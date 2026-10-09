using System.Collections.Concurrent;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using Azure;
using Azure.AI.OpenAI;
using Azure.AI.OpenAI.Chat;
using Microsoft.Extensions.Http;
using OpenAI.Chat;
using System.ClientModel;
using SWCP.Contracts;

namespace llmWorker.Services;

public sealed record LlmCompletion(string FeedbackMarkdown, LlmTokenUsage? TokenUsage, bool? TaskSolved);

public sealed class RetryableLlmException(TimeSpan retryDelay, Exception innerException)
    : Exception(innerException.Message, innerException)
{
    public TimeSpan RetryDelay { get; } = retryDelay;
}

public sealed class LlmTaskProcessor(IConfiguration configuration, IHttpClientFactory httpClientFactory)
{
        private const string StructuredResponseFormatName = "submission_feedback";
        private const string StructuredResponseSchema = """
                {
                    "type": "object",
                    "properties": {
                        "feedback": { "type": "string" },
                        "taskSolved": { "type": ["boolean", "null"] }
                    },
                    "required": ["feedback", "taskSolved"],
                    "additionalProperties": false
                }
                """;
        private const string HintStructuredResponseSchema = """
                {
                    "type": "object",
                    "properties": {
                        "feedback": { "type": "string", "maxLength": 300 },
                        "taskSolved": { "type": ["boolean", "null"] }
                    },
                    "required": ["feedback", "taskSolved"],
                    "additionalProperties": false
                }
                """;
    private readonly ConcurrentDictionary<string, (ChatClient Client, string Deployment)> _azureClients = new();

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
            var configuredDeployment = settings["Deployment"];
            if (string.IsNullOrWhiteSpace(endpoint) || string.IsNullOrWhiteSpace(apiKey) || string.IsNullOrWhiteSpace(configuredDeployment))
            {
                throw new InvalidOperationException(
                    $"Azure AI Foundry configuration for '{key}' must include Endpoint, ApiKey, and Deployment.");
            }

            var azureClient = new AzureOpenAIClient(new Uri(endpoint), new AzureKeyCredential(apiKey));
            return (azureClient.GetChatClient(configuredDeployment), configuredDeployment);
        });

        var options = new ChatCompletionOptions
        {
            MaxOutputTokenCount = task.Parameters.MaxTokens,
            ResponseFormat = ChatResponseFormat.CreateJsonSchemaFormat(
                StructuredResponseFormatName,
                BinaryData.FromString(GetStructuredResponseSchema(task, constrainHintFeedback: false)),
                jsonSchemaIsStrict: true)
        };
    #pragma warning disable AOAI001
        options.SetNewMaxCompletionTokensPropertyEnabled();
    #pragma warning restore AOAI001
        ChatMessage[] messages =
        [
            new SystemChatMessage(task.SystemPrompt),
            new UserChatMessage(task.UserPrompt)
        ];

        try
        {
            var response = await client.CompleteChatAsync(messages, options, cancellationToken);
            var result = response.Value;
            var usage = result.Usage is null
                ? null
                : new LlmTokenUsage(result.Usage.InputTokenCount, result.Usage.OutputTokenCount, result.Usage.TotalTokenCount);
            var content = result.Content.Count > 0 ? result.Content[0].Text : string.Empty;
            return ParseCompletion(content, usage, task);
        }
        catch (ClientResultException exception) when (exception.Status == (int)HttpStatusCode.TooManyRequests)
        {
            throw new RetryableLlmException(TimeSpan.FromSeconds(30), exception);
        }
        catch (ClientResultException exception) when (exception.Status >= 500)
        {
            throw new RetryableLlmException(TimeSpan.FromSeconds(5), exception);
        }
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
            top_p = task.Parameters.TopP,
            response_format = new
            {
                type = "json_schema",
                json_schema = new
                {
                    name = StructuredResponseFormatName,
                    schema = ParseStructuredResponseSchema(task)
                }
            }
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
        var choice = root.GetProperty("choices")[0];
        if (choice.TryGetProperty("finish_reason", out var finishReason) && finishReason.GetString() == "length")
        {
            throw new JsonException("The local LLM reached its output token limit before completing the structured response.");
        }

        var content = choice.GetProperty("message").GetProperty("content").GetString() ?? string.Empty;
        LlmTokenUsage? usage = null;
        if (root.TryGetProperty("usage", out var tokenUsage))
        {
            usage = new LlmTokenUsage(
                tokenUsage.GetProperty("prompt_tokens").GetInt32(),
                tokenUsage.GetProperty("completion_tokens").GetInt32(),
                tokenUsage.GetProperty("total_tokens").GetInt32());
        }

        return ParseCompletion(content, usage, task);
    }

    private static LlmCompletion ParseCompletion(string content, LlmTokenUsage? usage, LlmTask task)
    {
        using var document = JsonDocument.Parse(content);
        var root = document.RootElement;
        if (root.ValueKind != JsonValueKind.Object ||
            !root.TryGetProperty("feedback", out var feedbackElement) ||
            feedbackElement.ValueKind != JsonValueKind.String ||
            !root.TryGetProperty("taskSolved", out var taskSolvedElement) ||
            taskSolvedElement.ValueKind is not (JsonValueKind.True or JsonValueKind.False or JsonValueKind.Null))
        {
            throw new JsonException("The LLM response must contain string feedback and a boolean-or-null taskSolved field.");
        }

        bool? taskSolved = taskSolvedElement.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => null
        };
        if (task.EvaluationMode != "submit" || task.SandboxFailed)
        {
            taskSolved = null;
        }

        return new LlmCompletion(feedbackElement.GetString()!, usage, taskSolved);
    }

    private static string GetStructuredResponseSchema(LlmTask task, bool constrainHintFeedback) =>
        constrainHintFeedback && task.EvaluationMode == "hint"
            ? HintStructuredResponseSchema
            : StructuredResponseSchema;

    private static JsonElement ParseStructuredResponseSchema(LlmTask task)
    {
        using var document = JsonDocument.Parse(GetStructuredResponseSchema(task, constrainHintFeedback: true));
        return document.RootElement.Clone();
    }
}