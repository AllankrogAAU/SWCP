using coreApi.Data;
using coreApi.Models;
using Microsoft.EntityFrameworkCore;
using NATS.Client.JetStream;
using NATS.Client.JetStream.Models;
using SWCP.Contracts;

namespace coreApi.Services;

public sealed class LlmResultConsumer(
    ILogger<LlmResultConsumer> logger,
    INatsJSContext jetStream,
    IServiceScopeFactory scopeFactory,
    SubmissionEvents events) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(Messaging.LlmResultsStream, [Messaging.LlmResultSubject])
            {
                Retention = StreamConfigRetention.Workqueue,
                DuplicateWindow = TimeSpan.FromMinutes(10)
            }, stoppingToken);
        var consumer = await jetStream.CreateOrUpdateConsumerAsync(
            Messaging.LlmResultsStream,
            new ConsumerConfig(Messaging.LlmResultConsumer) { AckWait = TimeSpan.FromSeconds(30) },
            stoppingToken);

        await foreach (var message in consumer.ConsumeAsync<LlmResult>(cancellationToken: stoppingToken))
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
                await using var scope = scopeFactory.CreateAsyncScope();
                var database = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
                var submission = await database.Submissions
                    .SingleOrDefaultAsync(item => item.Id == result.SubmissionId, stoppingToken);
                if (submission is not null)
                {
                    var traceParent = message.Headers?.TryGetLastValue(Messaging.TraceParentHeader, out var parent)
                        == true ? parent : null;
                    submission.Status = result.Status == "success"
                        ? SubmissionStatus.COMPLETED
                        : SubmissionStatus.FAILED;
                    submission.LlmFeedback = result.FeedbackMarkdown;
                    submission.TaskSolved = result.TaskSolved;
                    submission.ErrorMessage = result.Error;
                    submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
                    await database.SaveChangesAsync(stoppingToken);
                    logger.LogInformation(
                        "Pipeline completed {event_name} {submission_id} {trace_id} {duration_ms} {status} {backend} {outcome}",
                        "pipeline.completed", submission.Id,
                        traceParent?.Split('-') is { Length: 4 } traceParts ? traceParts[1] : null,
                        (long)(submission.UpdatedAtUtc - submission.CreatedAtUtc).TotalMilliseconds,
                        submission.Status.ToString().ToLowerInvariant(), submission.LlmBackend,
                        result.Status == "success" ? "feedback_ready" : "llm_failed");
                    await events.PublishAsync(submission, submission.Status.ToString().ToLowerInvariant(),
                        result.Status == "success" ? "Feedback is ready" : "LLM feedback failed", 100, stoppingToken, traceParent);
                }
                else
                {
                    logger.LogWarning("Ignoring LLM result for unknown submission {SubmissionId}", result.SubmissionId);
                }

                await message.AckAsync(cancellationToken: stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Failed to process LLM result for submission {SubmissionId}", result.SubmissionId);
                await message.NakAsync(cancellationToken: stoppingToken);
            }
        }
    }
}
