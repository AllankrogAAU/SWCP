using System.Diagnostics;
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
    private const ulong MaxDeliveryAttempts = 5;
    private static readonly ActivitySource ActivitySource = new("SWCP.CoreApi");
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

            var traceParent = message.Headers?.TryGetLastValue(Messaging.TraceParentHeader, out var parent)
                == true ? parent : null;
            try
            {
                await ProcessResultAsync(result, traceParent, stoppingToken);
                await message.AckAsync(cancellationToken: stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                var deliveryCount = message.Metadata?.NumDelivered ?? 1UL;
                logger.LogError(exception,
                    "Failed to process sandbox result for submission {SubmissionId} (attempt {Attempt}/{MaxAttempts})",
                    result.SubmissionId, deliveryCount, MaxDeliveryAttempts);
                if (deliveryCount >= MaxDeliveryAttempts)
                {
                    await MarkSubmissionFailedAsync(result.SubmissionId, exception.Message, traceParent, stoppingToken);
                    await message.AckAsync(cancellationToken: stoppingToken);
                }
                else
                {
                    await message.NakAsync(TimeSpan.FromSeconds(2), stoppingToken);
                }
            }
        }
    }

    private async Task MarkSubmissionFailedAsync(
        Guid submissionId,
        string reason,
        string? traceParent,
        CancellationToken cancellationToken)
    {
        try
        {
            await using var scope = scopeFactory.CreateAsyncScope();
            var database = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            var submission = await database.Submissions
                .SingleOrDefaultAsync(item => item.Id == submissionId, cancellationToken);
            if (submission is null || submission.Status is SubmissionStatus.COMPLETED or SubmissionStatus.FAILED)
            {
                return;
            }

            submission.Status = SubmissionStatus.FAILED;
            submission.ErrorMessage = $"Failed to enqueue AI feedback after sandbox evaluation: {reason}";
            submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
            await database.SaveChangesAsync(cancellationToken);
            await events.PublishAsync(submission, "failed", submission.ErrorMessage, 100, cancellationToken, traceParent);
        }
        catch (Exception markException)
        {
            logger.LogError(markException, "Could not mark submission {SubmissionId} as failed after exhausting sandbox result retries", submissionId);
        }
    }

    private async Task ProcessResultAsync(SandboxResult result, string? traceParent, CancellationToken cancellationToken)
    {
        using var activity = ActivitySource.StartActivity(
            "process sandbox.results",
            ActivityKind.Consumer,
            traceParent ?? string.Empty);

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

        if (submission.Status is SubmissionStatus.COMPLETED or SubmissionStatus.FAILED)
        {
            logger.LogInformation("Ignoring sandbox result for terminal submission {SubmissionId} ({Status})", submission.Id, submission.Status);
            return;
        }

        var assignment = submission.Assignment
            ?? throw new InvalidOperationException($"Assignment {submission.AssignmentId} was not found.");
        var prompt = SubmissionPromptBuilder.Build(assignment, submission, result);

        submission.Status = SubmissionStatus.LLM_QUEUED;
        await database.SaveChangesAsync(cancellationToken);
        await events.PublishAsync(submission, "stage", "Queued for LLM feedback", 60, cancellationToken, traceParent);

        var queueTimer = System.Diagnostics.Stopwatch.StartNew();
        await publisher.PublishLlmTaskAsync(
            new LlmTask(submission.Id, submission.LlmBackend, prompt.SystemPrompt, prompt.UserPrompt,
                prompt.Parameters, traceParent, prompt.EvaluationMode, prompt.SandboxFailed),
            cancellationToken);
        logger.LogInformation(
            "Pipeline stage queued {event_name} {stage} {submission_id} {trace_id} {queue_publish_ms} {status} {backend}",
            "pipeline.stage.queued", "llm", submission.Id,
            traceParent?.Split('-') is { Length: 4 } llmTraceParts ? llmTraceParts[1] : null,
            queueTimer.ElapsedMilliseconds, "queued", submission.LlmBackend);
    }
}
