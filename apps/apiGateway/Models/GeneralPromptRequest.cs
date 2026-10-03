namespace api.Models;

public sealed record GeneralPromptRequest(
    string Prompt,
    string? SystemPrompt = null,
    double? Temperature = 0.2,
    int? MaxTokens = 4096);