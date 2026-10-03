using cloudLLM.Agents;
using cloudLLM.Interfaces;
using cloudLLM.Models;
using NATS.Client.JetStream;
using NATS.Client.JetStream.Models;

namespace cloudLLM;

public sealed class Worker(
    ILogger<Worker> logger,
    AgentFactory agentFactory,
    INatsJSContext jetStream) : BackgroundService
{
    private const string RequestStream = "ANALYSIS_REQUESTS";
    private const string RequestSubject = "analysis.requests";
    private const string ResultStream = "ANALYSIS_RESULTS";
    private const string ResultSubject = "analysis.results";
    private const string ConsumerName = "cloudllm-worker";

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(RequestStream, [RequestSubject])
            {
                Retention = StreamConfigRetention.Workqueue
            },
            stoppingToken);
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(ResultStream, [ResultSubject])
            {
                Retention = StreamConfigRetention.Limits,
                MaxAge = TimeSpan.FromDays(1)
            },
            stoppingToken);

        var consumer = await jetStream.CreateOrUpdateConsumerAsync(
            RequestStream,
            new ConsumerConfig(ConsumerName)
            {
                AckWait = TimeSpan.FromMinutes(10)
            },
            stoppingToken);

        logger.LogInformation("Consuming JetStream subject '{Subject}'", RequestSubject);

        await foreach (var message in consumer.ConsumeAsync<AnalysisRequestMessage>(
            cancellationToken: stoppingToken))
        {
            AnalysisRequestMessage? request;
            try
            {
                message.EnsureSuccess();
                request = message.Data;
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Could not deserialize an analysis request; acknowledging the invalid message");
                await message.AckAsync(cancellationToken: stoppingToken);
                continue;
            }

            if (request is null)
            {
                await message.AckAsync(cancellationToken: stoppingToken);
                continue;
            }

            var result = await AnalyzeAsync(request, stoppingToken);
            try
            {
                await jetStream.PublishAsync(ResultSubject, result, cancellationToken: stoppingToken);
                await message.AckAsync(cancellationToken: stoppingToken);
                logger.LogInformation("Published result for job {JobId}", request.JobId);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Could not publish the result for job {JobId}; requesting redelivery", request.JobId);
                await message.NakAsync(cancellationToken: stoppingToken);
            }
        }
    }

    private async Task<AnalysisResultMessage> AnalyzeAsync(
        AnalysisRequestMessage request,
        CancellationToken cancellationToken)
    {
        logger.LogInformation("Processing job {JobId} for category {Category}", request.JobId, request.Category);
        try
        {
            if (!Enum.TryParse<ErrorCategory>(request.Category, ignoreCase: true, out var category))
            {
                throw new InvalidOperationException($"Unknown error category '{request.Category}'.");
            }

            ILlmAgent agent = agentFactory.GetAgent(category);
            var response = await agent.AnalyzeAsync(request.CCode, request.Logs, cancellationToken);
            return new AnalysisResultMessage
            {
                JobId = request.JobId,
                Success = true,
                Response = response
            };
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception exception)
        {
            logger.LogError(exception, "Failed to analyze job {JobId}", request.JobId);
            return new AnalysisResultMessage
            {
                JobId = request.JobId,
                Success = false,
                Error = exception.Message
            };
        }
    }
}
