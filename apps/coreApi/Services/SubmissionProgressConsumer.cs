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
                    .SingleOrDefaultAsync(item => item.Id == started.SubmissionId, stoppingToken);
                if (submission is null || submission.Status is SubmissionStatus.COMPLETED or SubmissionStatus.FAILED)
                {
                    continue;
                }

                submission.Status = started.Stage switch
                {
                    "sandbox" => SubmissionStatus.SANDBOX_PROCESSING,
                    "llm" => SubmissionStatus.LLM_PROCESSING,
                    _ => submission.Status
                };
                submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
                await database.SaveChangesAsync(stoppingToken);
                var percent = submission.Status == SubmissionStatus.SANDBOX_PROCESSING ? 20 : 75;
                var traceParent = message.Headers?.TryGetLastValue(Messaging.TraceParentHeader, out var parent)
                    == true ? parent : null;
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