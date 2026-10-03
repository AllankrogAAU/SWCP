using System.Text.Json.Nodes;

namespace api.Services;

public sealed class LlmClient(HttpClient httpClient, ILogger<LlmClient> logger)
{
    public async Task<JsonNode> SendChatCompletionAsync(
        object payload,
        CancellationToken cancellationToken = default)
    {
        using var response = await httpClient.PostAsJsonAsync(
            "/v1/chat/completions",
            payload,
            cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            var details = await response.Content.ReadAsStringAsync(cancellationToken);
            logger.LogError("LLM server error ({Status}): {Details}", response.StatusCode, details);
            throw new HttpRequestException($"LLM server returned {(int)response.StatusCode}: {details}");
        }

        var node = await response.Content.ReadFromJsonAsync<JsonNode>(cancellationToken: cancellationToken);
        return node ?? throw new InvalidOperationException("LLM server returned an empty response body.");
    }
}