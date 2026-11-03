using NATS.Client.Core;
using SWCP.Contracts;

namespace coreApi.Services;

public sealed class LlmFeedbackStreamConsumer(
    ILogger<LlmFeedbackStreamConsumer> logger,
    NatsConnection nats,
    SubmissionEvents events) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await foreach (var message in nats.SubscribeAsync<LlmFeedbackStreamMessage>(
            Messaging.LlmFeedbackStreamSubject,
            cancellationToken: stoppingToken))
        {
            var update = message.Data;
            if (update is null)
            {
                continue;
            }

            try
            {
                await events.PublishFeedbackStreamAsync(update, stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                logger.LogError(
                    exception,
                    "Could not forward streamed feedback for submission {SubmissionId}, attempt {Attempt}, sequence {Sequence}",
                    update.SubmissionId,
                    update.Attempt,
                    update.Sequence);
            }
        }
    }
}
