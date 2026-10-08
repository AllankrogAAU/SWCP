using System.Collections.Concurrent;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using Azure;
using Azure.AI.Inference;
using Microsoft.Extensions.Http;
using SWCP.Contracts;

namespace llmWorker.Services;

public sealed record LlmCompletion(string FeedbackMarkdown, LlmTokenUsage? TokenUsage);

public sealed class RetryableLlmException(TimeSpan retryDelay, Exception innerException)
    : Exception(innerException.Message, innerException)
{
    public TimeSpan RetryDelay { get; } = retryDelay;
}

public sealed class LlmTaskProcessor(IConfiguration configuration, IHttpClientFactory httpClientFactory)
{
    private readonly ConcurrentDictionary<string, (ChatCompletionsClient Client, string Deployment)> _azureClients = new();

    public Task<LlmCompletion> CompleteAsync(LlmTask task, CancellationToken cancellationToken) =>
        task.Backend switch
        {
            "azure" => CompleteAzureAsync(task, cancellationToken),
            "local" => CompleteLocalAsync(task, cancellationToken),
            _ => throw new InvalidOperationException($"Unsupported LLM backend '{task.Backend}'.")
        };

    private async Task<LlmCompletion> CompleteAzureAsync(LlmTask task, CancellationToken cancellationToken)
    {
        var modelKey = configuration["AzureAIFoundry:DefaultModel"] ?? "Gpt6Sol";
        var (client, deployment) = _azureClients.GetOrAdd(modelKey, key =>
        {
            var settings = configuration.GetSection($"AzureAIFoundry:Models:{key}");
            var endpoint = settings["Endpoint"];
            var apiKey = settings["ApiKey"];
            var configuredDeployment = settings["Deployment"] ?? key;
            if (string.IsNullOrWhiteSpace(endpoint) || string.IsNullOrWhiteSpace(apiKey))
            {
                throw new InvalidOperationException($"Azure AI Foundry configuration for '{key}' is incomplete.");
            }

            return (new ChatCompletionsClient(new Uri(endpoint), new AzureKeyCredential(apiKey)), configuredDeployment);
        });

        var options = new ChatCompletionsOptions
        {
            Model = deployment,
            Temperature = (float)task.Parameters.Temperature,
            NucleusSamplingFactor = (float)task.Parameters.TopP,
            MaxTokens = task.Parameters.MaxTokens,
            Messages =
            {
                new ChatRequestSystemMessage(task.SystemPrompt),
                new ChatRequestUserMessage(task.UserPrompt)
            }
        };

        try
        {
            var response = await client.CompleteAsync(options, cancellationToken);
            var result = response.Value;
            var usage = result.Usage is null
                ? null
                : new LlmTokenUsage(result.Usage.PromptTokens, result.Usage.CompletionTokens, result.Usage.TotalTokens);
            return new LlmCompletion(result.Content ?? string.Empty, usage);
        }
        catch (RequestFailedException exception) when (exception.Status == (int)HttpStatusCode.TooManyRequests)
        {
            throw new RetryableLlmException(GetAzureRetryDelay(exception), exception);
        }
        catch (RequestFailedException exception) when (exception.Status >= 500)
        {
            throw new RetryableLlmException(TimeSpan.FromSeconds(5), exception);
        }
    }

    private static TimeSpan GetAzureRetryDelay(RequestFailedException exception)
    {
        if (exception.GetRawResponse()?.Headers.TryGetValue("Retry-After", out var retryAfter) == true)
        {
            if (double.TryParse(retryAfter, NumberStyles.Float, CultureInfo.InvariantCulture, out var seconds))
            {
                return TimeSpan.FromSeconds(Math.Clamp(seconds, 1, 300));
            }

            if (DateTimeOffset.TryParse(retryAfter, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var retryAt))
            {
                return TimeSpan.FromSeconds(Math.Clamp((retryAt - DateTimeOffset.UtcNow).TotalSeconds, 1, 300));
            }
        }

        return TimeSpan.FromSeconds(30);
    }

    private async Task<LlmCompletion> CompleteLocalAsync(LlmTask task, CancellationToken cancellationToken)
    {
        var modelName = configuration["LocalLlm:ModelName"] ?? configuration["LLM_MODEL_NAME"];
        if (string.IsNullOrWhiteSpace(modelName))
        {
            throw new InvalidOperationException("Local LLM model name is not configured.");
        }

        var client = httpClientFactory.CreateClient("LocalLlm");
        using var response = await client.PostAsJsonAsync("/v1/chat/completions", new
        {
            model = modelName,
            messages = new[]
            {
                new { role = "system", content = task.SystemPrompt },
                new { role = "user", content = task.UserPrompt }
            },
            temperature = task.Parameters.Temperature,
            max_tokens = task.Parameters.MaxTokens,
            top_p = task.Parameters.TopP
        }, cancellationToken);

        if ((int)response.StatusCode == 429)
        {
            var delay = response.Headers.RetryAfter?.Delta ?? TimeSpan.FromSeconds(10);
            throw new RetryableLlmException(delay, new HttpRequestException("Local LLM returned 429."));
        }

        if ((int)response.StatusCode >= 500)
        {
            throw new RetryableLlmException(TimeSpan.FromSeconds(5), new HttpRequestException($"Local LLM returned {(int)response.StatusCode}."));
        }

        response.EnsureSuccessStatusCode();
        using var document = await response.Content.ReadFromJsonAsync<JsonDocument>(cancellationToken: cancellationToken)
            ?? throw new InvalidOperationException("Local LLM returned an empty response.");
        var root = document.RootElement;
        var content = root.GetProperty("choices")[0].GetProperty("message").GetProperty("content").GetString() ?? string.Empty;
        LlmTokenUsage? usage = null;
        if (root.TryGetProperty("usage", out var tokenUsage))
        {
            usage = new LlmTokenUsage(
                tokenUsage.GetProperty("prompt_tokens").GetInt32(),
                tokenUsage.GetProperty("completion_tokens").GetInt32(),
                tokenUsage.GetProperty("total_tokens").GetInt32());
        }

        return new LlmCompletion(content, usage);
    }
}