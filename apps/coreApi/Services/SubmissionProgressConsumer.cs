using coreApi.Data;
using coreApi.Models;
using Microsoft.EntityFrameworkCore;
using NATS.Client.Core;
using SWCP.Contracts;

namespace coreApi.Services;

public sealed class SubmissionProgressConsumer(
    ILogger<SubmissionProgressConsumer> logger,
    NatsConnection nats,
    IServiceScopeFactory scopeFactory,
    SubmissionEvents events) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await foreach (var message in nats.SubscribeAsync<SubmissionWorkStarted>(
            Messaging.WorkStartedSubject,
            queueGroup: Messaging.WorkStartedQueueGroup,
            cancellationToken: stoppingToken))
        {
            var started = message.Data;
            if (started is null)
            {
                continue;
            }

            try
            {
                await using var scope = scopeFactory.CreateAsyncScope();
                var database = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
                var submission = await database.Submissions
                    .AsNoTracking()
                    .SingleOrDefaultAsync(item => item.Id == started.SubmissionId, stoppingToken);
                if (submission is null || submission.Status is SubmissionStatus.COMPLETED or SubmissionStatus.FAILED)
                {
                    continue;
                }

                var now = DateTimeOffset.UtcNow;
                var queueWaitMs = Math.Max(0, (now - submission.UpdatedAtUtc).TotalMilliseconds);
                var targetStatus = started.Stage switch
                {
                    "sandbox" => SubmissionStatus.SANDBOX_PROCESSING,
                    "llm" => SubmissionStatus.LLM_PROCESSING,
                    _ => submission.Status
                };

                var rowsUpdated = started.Stage switch
                {
                    "sandbox" => await database.Submissions
                        .Where(item => item.Id == started.SubmissionId
                            && item.Status != SubmissionStatus.COMPLETED
                            && item.Status != SubmissionStatus.FAILED
                            && item.Status != SubmissionStatus.LLM_QUEUED
                            && item.Status != SubmissionStatus.LLM_PROCESSING)
                        .ExecuteUpdateAsync(setters => setters
                            .SetProperty(item => item.Status, SubmissionStatus.SANDBOX_PROCESSING)
                            .SetProperty(item => item.UpdatedAtUtc, now), stoppingToken),
                    "llm" => await database.Submissions
                        .Where(item => item.Id == started.SubmissionId
                            && item.Status == SubmissionStatus.LLM_QUEUED)
                        .ExecuteUpdateAsync(setters => setters
                            .SetProperty(item => item.Status, SubmissionStatus.LLM_PROCESSING)
                            .SetProperty(item => item.UpdatedAtUtc, now), stoppingToken),
                    _ => 0
                };
                if (rowsUpdated == 0)
                {
                    continue;
                }

                submission.Status = targetStatus;
                submission.UpdatedAtUtc = now;
                var percent = submission.Status == SubmissionStatus.SANDBOX_PROCESSING ? 20 : 75;
                var traceParent = message.Headers?.TryGetLastValue(Messaging.TraceParentHeader, out var parent)
                    == true ? parent : null;
                logger.LogInformation(
                    "Pipeline stage started {event_name} {stage} {worker} {submission_id} {trace_id} {queue_wait_ms} {status}",
                    "pipeline.stage.started", started.Stage, started.Worker, submission.Id,
                    traceParent?.Split('-') is { Length: 4 } traceParts ? traceParts[1] : null,
                    (long)queueWaitMs, submission.Status.ToString());
                await events.PublishAsync(submission, "stage", $"{started.Worker} started", percent, stoppingToken, traceParent);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Could not record worker start for submission {SubmissionId}", started.SubmissionId);
            }
        }
    }
}