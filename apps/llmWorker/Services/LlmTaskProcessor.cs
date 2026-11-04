using System.Collections.Concurrent;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Net.Http.Json;
using System.Text;
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
                        "feedback": { "type": "string", "maxLength": 1500 },
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
    private readonly ConcurrentDictionary<string, bool> _azureTemperatureUnsupported = new();

    private float AzureTemperature =>
        configuration.GetValue<float?>("AzureAIFoundry:Temperature") ?? 0.0f;

    private ChatCompletionOptions CreateAzureOptions(LlmTask task, bool includeTemperature)
    {
        var options = new ChatCompletionOptions
        {
            MaxOutputTokenCount = task.Parameters.MaxTokens,
            ResponseFormat = ChatResponseFormat.CreateJsonSchemaFormat(
                StructuredResponseFormatName,
                BinaryData.FromString(GetStructuredResponseSchema(task, constrainHintFeedback: false)),
                jsonSchemaIsStrict: true)
        };
        if (includeTemperature)
        {
            options.Temperature = AzureTemperature;
        }
#pragma warning disable AOAI001
        options.SetNewMaxCompletionTokensPropertyEnabled();
#pragma warning restore AOAI001
        return options;
    }

    // Some reasoning deployments only accept the default temperature; remember that per model and omit it.
    private static bool IsUnsupportedTemperature(ClientResultException exception) =>
        exception.Status == (int)HttpStatusCode.BadRequest &&
        exception.Message.Contains("temperature", StringComparison.OrdinalIgnoreCase);

    private int LocalMaxTokens(LlmTask task)
    {
        var cap = configuration.GetValue<int?>("LocalLlm:MaxTokens");
        return cap is > 0 ? Math.Min(task.Parameters.MaxTokens, cap.Value) : task.Parameters.MaxTokens;
    }

    private object[] LocalMessages(LlmTask task) =>
        configuration.GetValue<bool>("LocalLlm:MergeSystemPrompt")
            ? [new { role = "user", content = $"{task.SystemPrompt}\n\n{task.UserPrompt}" }]
            : [new { role = "system", content = task.SystemPrompt }, new { role = "user", content = task.UserPrompt }];

    public Task<LlmCompletion> CompleteAsync(LlmTask task, CancellationToken cancellationToken) =>
        task.Backend switch
        {
            "azure" => CompleteAzureAsync(task, cancellationToken),
            "local" => CompleteLocalAsync(task, cancellationToken),
            _ => throw new InvalidOperationException($"Unsupported LLM backend '{task.Backend}'.")
        };

    public Task<LlmCompletion> CompleteStreamingAsync(
        LlmTask task,
        Func<string, CancellationToken, ValueTask>? onFeedbackDelta,
        CancellationToken cancellationToken) =>
        task.Backend switch
        {
            "azure" => CompleteAzureStreamingAsync(task, onFeedbackDelta, cancellationToken),
            "local" => CompleteLocalStreamingAsync(task, onFeedbackDelta, cancellationToken),
            _ => throw new InvalidOperationException($"Unsupported LLM backend '{task.Backend}'.")
        };

    private async Task<LlmCompletion> CompleteAzureAsync(LlmTask task, CancellationToken cancellationToken)
    {
        var modelKey = task.AzureModelKey ?? configuration["AzureAIFoundry:DefaultModel"] ?? "Gpt6Sol";
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

        var useTemperature = !_azureTemperatureUnsupported.ContainsKey(modelKey);
        ChatMessage[] messages =
        [
            new SystemChatMessage(task.SystemPrompt),
            new UserChatMessage(task.UserPrompt)
        ];

        try
        {
            ClientResult<ChatCompletion> response;
            try
            {
                response = await client.CompleteChatAsync(
                    messages, CreateAzureOptions(task, useTemperature), cancellationToken);
            }
            catch (ClientResultException exception) when (useTemperature && IsUnsupportedTemperature(exception))
            {
                _azureTemperatureUnsupported[modelKey] = true;
                response = await client.CompleteChatAsync(
                    messages, CreateAzureOptions(task, includeTemperature: false), cancellationToken);
            }
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
        if (configuration.GetValue<bool>("LocalLlm:MockEnabled"))
        {
            var delayMs = Math.Max(0, configuration.GetValue<int?>("LocalLlm:MockLatencyMs") ?? 5);
            if (delayMs > 0)
            {
                await Task.Delay(delayMs, cancellationToken);
            }

            var solved = task.EvaluationMode == "submit" &&
                !task.SandboxFailed &&
                task.ContextMode != "raw_code_only"
                    ? true
                    : (bool?)null;
            return new LlmCompletion(
                "Mock LLM feedback for infinite-GPU bottleneck benchmarking.",
                new LlmTokenUsage(250, 80, 330),
                solved);
        }

        var modelName = configuration["LocalLlm:ModelName"] ?? configuration["LLM_MODEL_NAME"];
        if (string.IsNullOrWhiteSpace(modelName))
        {
            throw new InvalidOperationException("Local LLM model name is not configured.");
        }

        var client = httpClientFactory.CreateClient("LocalLlm");
        using var response = await client.PostAsJsonAsync("/v1/chat/completions", new
        {
            model = modelName,
            messages = LocalMessages(task),
            temperature = task.Parameters.Temperature,
            max_tokens = LocalMaxTokens(task),
            top_p = task.Parameters.TopP,
            presence_penalty = 0.3,
            frequency_penalty = 0.3,
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
        var isLengthFinish = choice.TryGetProperty("finish_reason", out var finishReason) && finishReason.GetString() == "length";
        var content = choice.GetProperty("message").GetProperty("content").GetString() ?? string.Empty;
        if (isLengthFinish)
        {
            content = RecoverTruncatedJson(content, task);
        }

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

    private async Task<LlmCompletion> CompleteAzureStreamingAsync(
        LlmTask task,
        Func<string, CancellationToken, ValueTask>? onFeedbackDelta,
        CancellationToken cancellationToken)
    {
        var modelKey = task.AzureModelKey ?? configuration["AzureAIFoundry:DefaultModel"] ?? "Gpt6Sol";
        var (client, deployment) = _azureClients.GetOrAdd(modelKey, key =>
        {
            var settings = configuration.GetSection($"AzureAIFoundry:Models:{key}");
            var endpoint = settings["Endpoint"];
            var apiKey = settings["ApiKey"];
            var configuredDeployment = settings["Deployment"];
            if (string.IsNullOrWhiteSpace(endpoint) || string.IsNullOrWhiteSpace(apiKey) ||
                string.IsNullOrWhiteSpace(configuredDeployment))
            {
                throw new InvalidOperationException(
                    $"Azure AI Foundry configuration for '{key}' must include Endpoint, ApiKey, and Deployment.");
            }

            var azureClient = new AzureOpenAIClient(new Uri(endpoint), new AzureKeyCredential(apiKey));
            return (azureClient.GetChatClient(configuredDeployment), configuredDeployment);
        });

        var useTemperature = !_azureTemperatureUnsupported.ContainsKey(modelKey);
        ChatMessage[] messages =
        [
            new SystemChatMessage(task.SystemPrompt),
            new UserChatMessage(task.UserPrompt)
        ];

        var rawContent = new StringBuilder();
        var feedbackParser = onFeedbackDelta is null ? null : new FeedbackJsonStreamParser();
        LlmTokenUsage? usage = null;
        try
        {
            while (true)
            {
                try
                {
                    await foreach (var update in client.CompleteChatStreamingAsync(
                        messages, CreateAzureOptions(task, useTemperature), cancellationToken))
                    {
                        foreach (var contentPart in update.ContentUpdate)
                        {
                            var content = contentPart.Text;
                            if (!string.IsNullOrEmpty(content))
                            {
                                rawContent.Append(content);
                                var feedbackDelta = feedbackParser?.Append(content);
                                if (!string.IsNullOrEmpty(feedbackDelta))
                                {
                                    await onFeedbackDelta!(feedbackDelta, cancellationToken);
                                }
                            }
                        }

                        if (update.Usage is { } updateUsage)
                        {
                            usage = new LlmTokenUsage(
                                updateUsage.InputTokenCount,
                                updateUsage.OutputTokenCount,
                                updateUsage.TotalTokenCount);
                        }
                    }
                    break;
                }
                catch (ClientResultException exception) when (
                    useTemperature && rawContent.Length == 0 && IsUnsupportedTemperature(exception))
                {
                    _azureTemperatureUnsupported[modelKey] = true;
                    useTemperature = false;
                }
            }
        }
        catch (ClientResultException exception) when (exception.Status == (int)HttpStatusCode.TooManyRequests)
        {
            throw new RetryableLlmException(TimeSpan.FromSeconds(30), exception);
        }
        catch (ClientResultException exception) when (exception.Status >= 500)
        {
            throw new RetryableLlmException(TimeSpan.FromSeconds(5), exception);
        }

        feedbackParser?.Complete();
        return ParseCompletion(rawContent.ToString(), usage, task);
    }

    private async Task<LlmCompletion> CompleteLocalStreamingAsync(
        LlmTask task,
        Func<string, CancellationToken, ValueTask>? onFeedbackDelta,
        CancellationToken cancellationToken)
    {
        if (configuration.GetValue<bool>("LocalLlm:MockEnabled"))
        {
            var delayMs = Math.Max(0, configuration.GetValue<int?>("LocalLlm:MockLatencyMs") ?? 5);
            if (delayMs > 0)
            {
                await Task.Delay(delayMs, cancellationToken);
            }

            const string mockFeedback = "Mock LLM feedback for infinite-GPU bottleneck benchmarking.";
            if (onFeedbackDelta is not null)
            {
                for (var offset = 0; offset < mockFeedback.Length; offset += 16)
                {
                    await onFeedbackDelta(
                        mockFeedback.Substring(offset, Math.Min(16, mockFeedback.Length - offset)),
                        cancellationToken);
                }
            }

            var solved = task.EvaluationMode == "submit" &&
                !task.SandboxFailed &&
                task.ContextMode != "raw_code_only"
                    ? true
                    : (bool?)null;
            return new LlmCompletion(mockFeedback, new LlmTokenUsage(250, 80, 330), solved);
        }

        var modelName = configuration["LocalLlm:ModelName"] ?? configuration["LLM_MODEL_NAME"];
        if (string.IsNullOrWhiteSpace(modelName))
        {
            throw new InvalidOperationException("Local LLM model name is not configured.");
        }

        var client = httpClientFactory.CreateClient("LocalLlm");
        using var request = new HttpRequestMessage(HttpMethod.Post, "/v1/chat/completions")
        {
            Content = JsonContent.Create(new
            {
                model = modelName,
                messages = LocalMessages(task),
                temperature = task.Parameters.Temperature,
                max_tokens = LocalMaxTokens(task),
                top_p = task.Parameters.TopP,
                presence_penalty = 0.3,
                frequency_penalty = 0.3,
                stream = true,
                stream_options = new { include_usage = true },
                response_format = new
                {
                    type = "json_schema",
                    json_schema = new
                    {
                        name = StructuredResponseFormatName,
                        schema = ParseStructuredResponseSchema(task)
                    }
                }
            })
        };

        using var response = await client.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            cancellationToken);
        if ((int)response.StatusCode == 429)
        {
            var delay = response.Headers.RetryAfter?.Delta ?? TimeSpan.FromSeconds(10);
            throw new RetryableLlmException(delay, new HttpRequestException("Local LLM returned 429."));
        }

        if ((int)response.StatusCode >= 500)
        {
            throw new RetryableLlmException(
                TimeSpan.FromSeconds(5),
                new HttpRequestException($"Local LLM returned {(int)response.StatusCode}."));
        }

        response.EnsureSuccessStatusCode();
        var rawContent = new StringBuilder();
        var feedbackParser = onFeedbackDelta is null ? null : new FeedbackJsonStreamParser();
        LlmTokenUsage? usage = null;
        var truncated = false;
        var streamEnded = false;
        var eventData = new StringBuilder();
        await using var responseStream = await response.Content.ReadAsStreamAsync(cancellationToken);
        using var reader = new StreamReader(responseStream);
        while (await reader.ReadLineAsync(cancellationToken) is { } line)
        {
            if (line.Length == 0)
            {
                if (eventData.Length == 0) continue;
                if (await ProcessLocalStreamEventAsync(
                    eventData.ToString(),
                    rawContent,
                    feedbackParser,
                    onFeedbackDelta,
                    cancellationToken,
                    value => usage = value,
                    value => truncated = value))
                {
                    streamEnded = true;
                    break;
                }

                eventData.Clear();
                continue;
            }

            if (line.StartsWith("data:", StringComparison.Ordinal))
            {
                if (eventData.Length > 0) eventData.Append('\n');
                eventData.Append(line.AsSpan(5).TrimStart());
            }
        }

        if (!streamEnded && eventData.Length > 0)
        {
            streamEnded = await ProcessLocalStreamEventAsync(
                eventData.ToString(),
                rawContent,
                feedbackParser,
                onFeedbackDelta,
                cancellationToken,
                value => usage = value,
                value => truncated = value);
        }

        if (!streamEnded)
        {
            throw new InvalidOperationException("Local LLM stream ended without a [DONE] marker.");
        }

        var content = rawContent.ToString();
        if (truncated)
        {
            content = RecoverTruncatedJson(content, task);
        }
        else
        {
            feedbackParser?.Complete();
        }

        return ParseCompletion(content, usage, task);
    }

    private static async Task<bool> ProcessLocalStreamEventAsync(
        string eventData,
        StringBuilder rawContent,
        FeedbackJsonStreamParser? feedbackParser,
        Func<string, CancellationToken, ValueTask>? onFeedbackDelta,
        CancellationToken cancellationToken,
        Action<LlmTokenUsage?> setUsage,
        Action<bool> setTruncated)
    {
        if (eventData == "[DONE]") return true;

        using var document = JsonDocument.Parse(eventData);
        var root = document.RootElement;
        if (root.TryGetProperty("usage", out var usage) && usage.ValueKind == JsonValueKind.Object)
        {
            setUsage(new LlmTokenUsage(
                usage.GetProperty("prompt_tokens").GetInt32(),
                usage.GetProperty("completion_tokens").GetInt32(),
                usage.GetProperty("total_tokens").GetInt32()));
        }

        if (!root.TryGetProperty("choices", out var choices) || choices.GetArrayLength() == 0)
        {
            return false;
        }

        var choice = choices[0];
        if (choice.TryGetProperty("finish_reason", out var finishReason) &&
            finishReason.ValueKind == JsonValueKind.String &&
            finishReason.GetString() == "length")
        {
            setTruncated(true);
        }

        if (choice.TryGetProperty("delta", out var delta) &&
            delta.TryGetProperty("content", out var contentElement) &&
            contentElement.ValueKind == JsonValueKind.String)
        {
            var content = contentElement.GetString() ?? string.Empty;
            rawContent.Append(content);
            var feedbackDelta = feedbackParser?.Append(content);
            if (!string.IsNullOrEmpty(feedbackDelta))
            {
                await onFeedbackDelta!(feedbackDelta, cancellationToken);
            }
        }

        return false;
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
        if (task.EvaluationMode != "submit" ||
            task.SandboxFailed ||
            task.ContextMode == "raw_code_only")
        {
            taskSolved = null;
        }

        return new LlmCompletion(feedbackElement.GetString()!, usage, taskSolved);
    }

    private static string RecoverTruncatedJson(string content, LlmTask task)
    {
        try
        {
            using var testDoc = JsonDocument.Parse(content);
            return content;
        }
        catch (JsonException)
        {
            var feedback = content;
            var keyIdx = content.IndexOf("\"feedback\":", StringComparison.OrdinalIgnoreCase);
            if (keyIdx >= 0)
            {
                var valStart = content.IndexOf('\"', keyIdx + 11);
                if (valStart >= 0)
                {
                    feedback = content[(valStart + 1)..];
                }
            }
            feedback = feedback.TrimEnd('\\', '\"', ',', ' ', '\r', '\n', '\t') + "... [response truncated for length]";
            return JsonSerializer.Serialize(new
            {
                feedback,
                taskSolved = (bool?)null
            });
        }
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