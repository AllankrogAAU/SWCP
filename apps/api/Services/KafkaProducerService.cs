using System.Text.Json;
using api.Models;
using Confluent.Kafka;

namespace api.Services
{
    public interface IKafkaProducerService
    {
        Task PublishAnalysisRequestAsync(AnalysisRequestMessage message, CancellationToken cancellationToken = default);
    }

    public class KafkaProducerService : IKafkaProducerService, IDisposable
    {
        private readonly IProducer<string, string> _producer;
        private readonly string _requestTopic;

        public KafkaProducerService(IConfiguration configuration)
        {
            var kafkaSection = configuration.GetSection("Kafka");
            var bootstrapServers = kafkaSection["BootstrapServers"] ?? "localhost:9092";
            _requestTopic = kafkaSection["RequestTopic"] ?? "code-analysis-requests";

            var producerConfig = new ProducerConfig
            {
                BootstrapServers = bootstrapServers
            };

            _producer = new ProducerBuilder<string, string>(producerConfig).Build();
        }

        public async Task PublishAnalysisRequestAsync(AnalysisRequestMessage message, CancellationToken cancellationToken = default)
        {
            var payload = JsonSerializer.Serialize(message);

            await _producer.ProduceAsync(_requestTopic, new Message<string, string>
            {
                Key = message.JobId.ToString(),
                Value = payload
            }, cancellationToken);
        }

        public void Dispose()
        {
            _producer.Flush(TimeSpan.FromSeconds(5));
            _producer.Dispose();
        }
    }
}
