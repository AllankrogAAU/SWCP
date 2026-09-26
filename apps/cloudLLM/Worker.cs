using System.Text.Json;
using cloudLLM.Agents;
using cloudLLM.Interfaces;
using cloudLLM.Models;
using Confluent.Kafka;

namespace cloudLLM
{
    public class Worker(ILogger<Worker> logger, AgentFactory agentFactory, IConfiguration configuration) : BackgroundService
    {
        protected override async Task ExecuteAsync(CancellationToken stoppingToken)
        {
            var kafkaSection = configuration.GetSection("Kafka");
            var bootstrapServers = kafkaSection["BootstrapServers"] ?? "localhost:9092";
            var requestTopic = kafkaSection["RequestTopic"] ?? "code-analysis-requests";
            var resultTopic = kafkaSection["ResultTopic"] ?? "code-analysis-results";
            var groupId = kafkaSection["ConsumerGroupId"] ?? "cloudllm-worker";

            var consumerConfig = new ConsumerConfig
            {
                BootstrapServers = bootstrapServers,
                GroupId = groupId,
                AutoOffsetReset = AutoOffsetReset.Earliest
            };

            var producerConfig = new ProducerConfig
            {
                BootstrapServers = bootstrapServers
            };

            using var consumer = new ConsumerBuilder<string, string>(consumerConfig).Build();
            using var producer = new ProducerBuilder<string, string>(producerConfig).Build();

            consumer.Subscribe(requestTopic);
            logger.LogInformation("Subscribed to Kafka topic '{Topic}' as group '{GroupId}'", requestTopic, groupId);

            try
            {
                while (!stoppingToken.IsCancellationRequested)
                {
                    ConsumeResult<string, string>? consumeResult;
                    try
                    {
                        consumeResult = consumer.Consume(stoppingToken);
                    }
                    catch (OperationCanceledException)
                    {
                        break;
                    }
                    catch (ConsumeException ex)
                    {
                        logger.LogError(ex, "Error consuming message from Kafka");
                        continue;
                    }

                    if (consumeResult?.Message is null)
                    {
                        continue;
                    }

                    await HandleRequestAsync(consumeResult.Message.Value, producer, resultTopic, stoppingToken);
                }
            }
            finally
            {
                consumer.Close();
            }
        }

        private async Task HandleRequestAsync(string rawMessage, IProducer<string, string> producer, string resultTopic, CancellationToken stoppingToken)
        {
            AnalysisRequestMessage? request;
            try
            {
                request = JsonSerializer.Deserialize<AnalysisRequestMessage>(rawMessage);
            }
            catch (JsonException ex)
            {
                logger.LogError(ex, "Failed to deserialize analysis request message: {Raw}", rawMessage);
                return;
            }

            if (request is null)
            {
                logger.LogWarning("Received empty analysis request message");
                return;
            }

            logger.LogInformation("Processing job {JobId} for category {Category}", request.JobId, request.Category);

            AnalysisResultMessage result;
            try
            {
                if (!Enum.TryParse<ErrorCategory>(request.Category, ignoreCase: true, out var category))
                {
                    throw new InvalidOperationException($"Unknown error category '{request.Category}'.");
                }

                ILlmAgent agent = agentFactory.GetAgent(category);
                var response = await agent.AnalyzeAsync(request.CCode, request.Logs, stoppingToken);

                result = new AnalysisResultMessage
                {
                    JobId = request.JobId,
                    Success = true,
                    Response = response
                };
            }
            catch (Exception ex)
            {
                logger.LogError(ex, "Failed to analyze job {JobId}", request.JobId);
                result = new AnalysisResultMessage
                {
                    JobId = request.JobId,
                    Success = false,
                    Error = ex.Message
                };
            }

            var payload = JsonSerializer.Serialize(result);
            await producer.ProduceAsync(resultTopic, new Message<string, string>
            {
                Key = request.JobId.ToString(),
                Value = payload
            }, stoppingToken);

            logger.LogInformation("Published result for job {JobId} to '{Topic}'", request.JobId, resultTopic);
        }
    }
}
