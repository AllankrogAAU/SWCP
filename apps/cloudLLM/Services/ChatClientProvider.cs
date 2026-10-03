using System.Collections.Concurrent;
using Azure;
using Azure.AI.OpenAI;
using OpenAI.Chat;
using Microsoft.Extensions.Configuration;

namespace cloudLLM.Services
{
    public interface IChatClientProvider
    {
        (ChatClient Client, string Deployment) GetClient(string modelKey);
    }

    public class ChatClientProvider(IConfiguration configuration) : IChatClientProvider
    {
        private readonly ConcurrentDictionary<string, (ChatClient Client, string Deployment)> _clients = new();

        public (ChatClient Client, string Deployment) GetClient(string modelKey)
        {
            return _clients.GetOrAdd(modelKey, key =>
            {
                var section = configuration.GetSection($"AzureAIFoundry:Models:{key}");

                var endpoint = section["Endpoint"];
                var apiKey = section["ApiKey"];
                var deployment = section["Deployment"];

                if (string.IsNullOrWhiteSpace(endpoint) || string.IsNullOrWhiteSpace(apiKey) || string.IsNullOrWhiteSpace(deployment))
                {
                    throw new InvalidOperationException(
                        $"Missing Azure AI Foundry configuration for model '{key}'. " +
                        $"Expected 'AzureAIFoundry:Models:{key}:Endpoint', ':ApiKey' and ':Deployment' to be set.");
                }

                var azureClient = new AzureOpenAIClient(new Uri(endpoint), new AzureKeyCredential(apiKey));
                var client = azureClient.GetChatClient(deployment);
                return (client, deployment);
            });
        }
    }
}
