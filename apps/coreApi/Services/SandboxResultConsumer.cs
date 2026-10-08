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

        submission.SandboxOutputJson = JsonSerializer.Serialize(result);
        submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
        if (result.ErrorClassification?.StartsWith("worker-error:", StringComparison.Ordinal) == true)
        {
            submission.Status = SubmissionStatus.FAILED;
            submission.ErrorMessage = result.ErrorClassification["worker-error:".Length..];
            await database.SaveChangesAsync(cancellationToken);
            await events.PublishAsync(submission, "failed", "Sandbox worker exhausted its retries", 100, cancellationToken, traceParent);
            return;
        }

        if (!result.Compilation.Success)
        {
            submission.Status = SubmissionStatus.COMPLETED;
            await database.SaveChangesAsync(cancellationToken);
            await events.PublishAsync(submission, "completed", "Compilation failed; LLM feedback skipped", 100, cancellationToken, traceParent);
            return;
        }

        submission.Status = SubmissionStatus.LLM_QUEUED;
        await database.SaveChangesAsync(cancellationToken);
        await events.PublishAsync(submission, "stage", "Queued for LLM feedback", 60, cancellationToken, traceParent);

        var assignment = submission.Assignment
            ?? throw new InvalidOperationException($"Assignment {submission.AssignmentId} was not found.");
        var systemPrompt = string.IsNullOrWhiteSpace(assignment.SystemPromptTemplate)
            ? "You are a programming tutor. Give concise, actionable feedback about the student's C submission."
            : assignment.SystemPromptTemplate;
        var userPrompt = $"Assignment:\n{assignment.Description}\n\nC source:\n```c\n{submission.SourceCode}\n```\n\nSandbox output:\n{JsonSerializer.Serialize(result)}";
        await publisher.PublishLlmTaskAsync(
            new LlmTask(submission.Id, submission.LlmBackend, systemPrompt, userPrompt,
                new LlmInferenceParameters(0.2, 2048, 1.0), traceParent),
            cancellationToken);
    }
}
