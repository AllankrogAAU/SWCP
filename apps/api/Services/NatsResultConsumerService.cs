using api.Models;
using NATS.Client.JetStream;
using NATS.Client.JetStream.Models;

namespace api.Services;

public sealed class NatsResultConsumerService(
    ILogger<NatsResultConsumerService> logger,
    INatsJSContext jetStream,
    AnalysisResultStore resultStore) : BackgroundService
{
    private const string StreamName = "ANALYSIS_RESULTS";
    private const string Subject = "analysis.results";
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(StreamName, [Subject])
            {
                Retention = StreamConfigRetention.Limits,
                MaxAge = TimeSpan.FromDays(1)
            },
            stoppingToken);

        var consumer = await jetStream.CreateConsumerAsync(
            StreamName,
            new ConsumerConfig(),
            stoppingToken);

        logger.LogInformation("Consuming JetStream subject '{Subject}'", Subject);

        await foreach (var message in consumer.ConsumeAsync<AnalysisResultMessage>(
            cancellationToken: stoppingToken))
        {
            message.EnsureSuccess();
            if (message.Data is { } result)
            {
                resultStore.SetResult(result);
                logger.LogInformation("Stored result for job {JobId}", result.JobId);
            }

            await message.AckAsync(cancellationToken: stoppingToken);
        }
    }
}