using System.Text.Json;
using api.Models;
using Confluent.Kafka;

namespace api.Services
{
    public class KafkaResultConsumerService(
        ILogger<KafkaResultConsumerService> logger,
        IConfiguration configuration,
        AnalysisResultStore resultStore) : BackgroundService
    {
        protected override Task ExecuteAsync(CancellationToken stoppingToken)
        {
            return Task.Factory.StartNew(() => ConsumeLoop(stoppingToken), stoppingToken,
                TaskCreationOptions.LongRunning, TaskScheduler.Default);
        }

        private void ConsumeLoop(CancellationToken stoppingToken)
        {
            var kafkaSection = configuration.GetSection("Kafka");
            var bootstrapServers = kafkaSection["BootstrapServers"] ?? "localhost:9092";
            var resultTopic = kafkaSection["ResultTopic"] ?? "code-analysis-results";
            var groupId = kafkaSection["ConsumerGroupId"] ?? "api-result-consumer";

            var consumerConfig = new ConsumerConfig
            {
                BootstrapServers = bootstrapServers,
                GroupId = groupId,
                AutoOffsetReset = AutoOffsetReset.Earliest
            };

            using var consumer = new ConsumerBuilder<string, string>(consumerConfig).Build();
            consumer.Subscribe(resultTopic);
            logger.LogInformation("Subscribed to Kafka topic '{Topic}' as group '{GroupId}'", resultTopic, groupId);

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

                    try
                    {
                        var result = JsonSerializer.Deserialize<AnalysisResultMessage>(consumeResult.Message.Value);
                        if (result is not null)
                        {
                            resultStore.SetResult(result);
                            logger.LogInformation("Stored result for job {JobId}", result.JobId);
                        }
                    }
                    catch (JsonException ex)
                    {
                        logger.LogError(ex, "Failed to deserialize analysis result message: {Raw}", consumeResult.Message.Value);
                    }
                }
            }
            finally
            {
                consumer.Close();
            }
        }
    }
}
