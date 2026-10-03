using api.Models;
using NATS.Client.JetStream;
using NATS.Client.JetStream.Models;

namespace api.Services;

public interface IAnalysisRequestPublisher
{
    Task PublishAnalysisRequestAsync(
        AnalysisRequestMessage message,
        CancellationToken cancellationToken = default);
}

public sealed class NatsAnalysisRequestPublisher(INatsJSContext jetStream) : IAnalysisRequestPublisher
{
    private const string StreamName = "ANALYSIS_REQUESTS";
    private const string Subject = "analysis.requests";

    public async Task PublishAnalysisRequestAsync(
        AnalysisRequestMessage message,
        CancellationToken cancellationToken = default)
    {
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(StreamName, [Subject])
            {
                Retention = StreamConfigRetention.Workqueue
            },
            cancellationToken);

        await jetStream.PublishAsync(Subject, message, cancellationToken: cancellationToken);
    }
}