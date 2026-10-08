using System.Text.Json;
using coreApi.Data;
using coreApi.Models;
using Microsoft.EntityFrameworkCore;
using NATS.Client.JetStream;
using NATS.Client.JetStream.Models;
using SWCP.Contracts;

namespace coreApi.Services;

public sealed class SandboxResultConsumer(
    ILogger<SandboxResultConsumer> logger,
    INatsJSContext jetStream,
    IServiceScopeFactory scopeFactory,
    NatsTaskPublisher publisher,
    SubmissionEvents events) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(Messaging.SandboxResultsStream, [Messaging.SandboxResultSubject])
            {
                Retention = StreamConfigRetention.Workqueue
            }, stoppingToken);
        var consumer = await jetStream.CreateOrUpdateConsumerAsync(
            Messaging.SandboxResultsStream,
            new ConsumerConfig(Messaging.SandboxResultConsumer) { AckWait = TimeSpan.FromSeconds(30) },
            stoppingToken);

        await foreach (var message in consumer.ConsumeAsync<SandboxResult>(cancellationToken: stoppingToken))
        {
            message.EnsureSuccess();
            var result = message.Data;
            if (result is null)
            {
                await message.AckAsync(cancellationToken: stoppingToken);
                continue;
            }

            try
            {
                var traceParent = message.Headers?.TryGetLastValue(Messaging.TraceParentHeader, out var parent)
                    == true ? parent : null;
                await ProcessResultAsync(result, traceParent, stoppingToken);
                await message.AckAsync(cancellationToken: stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Failed to process sandbox result for submission {SubmissionId}", result.SubmissionId);
                await message.NakAsync(cancellationToken: stoppingToken);
            }
        }
    }

    private async Task ProcessResultAsync(SandboxResult result, string? traceParent, CancellationToken cancellationToken)
    {
        await using var scope = scopeFactory.CreateAsyncScope();
        var database = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
        var submission = await database.Submissions
            .Include(item => item.Assignment)
            .SingleOrDefaultAsync(item => item.Id == result.SubmissionId, cancellationToken);
        if (submission is null)
        {
            logger.LogWarning("Ignoring sandbox result for unknown submission {SubmissionId}", result.SubmissionId);
            return;
        }

        logger.LogInformation(
            "Pipeline stage result received {event_name} {stage} {submission_id} {trace_id} {status} {outcome}",
            "pipeline.stage.result_received", "sandbox", submission.Id,
            traceParent?.Split('-') is { Length: 4 } traceParts ? traceParts[1] : null,
            result.Compilation.Success ? "completed" : "compile_failed", result.ErrorClassification);

        submission.SandboxOutputJson = JsonSerializer.Serialize(result);
        submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
        if (result.ErrorClassification?.StartsWith("worker-error:", StringComparison.Ordinal) == true)
        {
            submission.Status = SubmissionStatus.FAILED;
            submission.ErrorMessage = result.ErrorClassification["worker-error:".Length..];
            await database.SaveChangesAsync(cancellationToken);
            logger.LogError(
                "Pipeline completed {event_name} {submission_id} {trace_id} {duration_ms} {status} {backend} {outcome}",
                "pipeline.completed", submission.Id,
                traceParent?.Split('-') is { Length: 4 } workerTraceParts ? workerTraceParts[1] : null,
                (long)(DateTimeOffset.UtcNow - submission.CreatedAtUtc).TotalMilliseconds,
                "failed", submission.LlmBackend, "sandbox_worker_error");
            await events.PublishAsync(submission, "failed", "Sandbox worker exhausted its retries", 100, cancellationToken, traceParent);
            return;
        }

        if (submission.Action == "run")
        {
            submission.Status = SubmissionStatus.COMPLETED;
            await database.SaveChangesAsync(cancellationToken);
            logger.LogInformation(
                "Pipeline completed {event_name} {submission_id} {trace_id} {duration_ms} {status} {backend} {outcome}",
                "pipeline.completed", submission.Id,
                traceParent?.Split('-') is { Length: 4 } compileTraceParts ? compileTraceParts[1] : null,
                (long)(DateTimeOffset.UtcNow - submission.CreatedAtUtc).TotalMilliseconds,
                "completed", submission.LlmBackend,
                result.Compilation.Success ? "sandbox_run_completed" : "sandbox_compile_failed");
            await events.PublishAsync(submission, "completed", "Sandbox run completed", 100, cancellationToken, traceParent);
            return;
        }

        submission.Status = SubmissionStatus.LLM_QUEUED;
        await database.SaveChangesAsync(cancellationToken);
        await events.PublishAsync(submission, "stage", "Queued for LLM feedback", 60, cancellationToken, traceParent);

        var assignment = submission.Assignment
            ?? throw new InvalidOperationException($"Assignment {submission.AssignmentId} was not found.");
        var baseSystemPrompt = string.IsNullOrWhiteSpace(assignment.SystemPromptTemplate)
            ? "You are a programming tutor. Give concise, actionable feedback about the student's C submission."
            : assignment.SystemPromptTemplate;
        var sandboxFailed = !result.Compilation.Success || result.TestResults.Any(test => !test.Passed);
        var evaluationMode = submission.Action == "hint" ? "hint" : "submit";
        var actionInstructions = evaluationMode == "hint"
            ? "The student requested a hint. Explain the most useful next step without giving away a complete solution. Do not judge whether the assignment is solved. End with exactly this metadata line: SWCP_TASK_SOLVED: not_evaluated"
            : sandboxFailed
                ? "The sandbox reported a compilation or runtime failure. Focus on explaining the failure and how the student can debug it educationally. Do not judge whether the assignment is solved. End with exactly this metadata line: SWCP_TASK_SOLVED: not_evaluated"
                : "Review the code for significant non-crashing quality issues first. If significant issues exist, explain them and do not judge task completion. If no significant issues exist, compare the program with the assignment description and decide whether it is correctly solved. End with exactly one metadata line: SWCP_TASK_SOLVED: true or SWCP_TASK_SOLVED: false. Do not include that line in the user-facing explanation.";
        var systemPrompt = $"{baseSystemPrompt}\n\n{actionInstructions}";
        var userPrompt = $"Assignment:\n{assignment.Description}\n\nC source:\n```c\n{submission.SourceCode}\n```\n\nSandbox output:\n{JsonSerializer.Serialize(result)}";
        var queueTimer = System.Diagnostics.Stopwatch.StartNew();
        await publisher.PublishLlmTaskAsync(
            new LlmTask(submission.Id, submission.LlmBackend, systemPrompt, userPrompt,
                new LlmInferenceParameters(0.2, 2048, 1.0), traceParent, evaluationMode, sandboxFailed),
            cancellationToken);
        logger.LogInformation(
            "Pipeline stage queued {event_name} {stage} {submission_id} {trace_id} {queue_publish_ms} {status} {backend}",
            "pipeline.stage.queued", "llm", submission.Id,
            traceParent?.Split('-') is { Length: 4 } llmTraceParts ? llmTraceParts[1] : null,
            queueTimer.ElapsedMilliseconds, "queued", submission.LlmBackend);
    }
}
