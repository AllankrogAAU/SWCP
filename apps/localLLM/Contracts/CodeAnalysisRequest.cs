namespace LLM.Contracts;

public sealed record CodeAnalysisRequest(
    string Code,
    string Instruction,
    string? Language = null,
    string? SystemPrompt = null
);