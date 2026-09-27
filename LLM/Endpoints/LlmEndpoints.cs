using System.Text.Json.Nodes;
using LLM.Contracts;
using LLM.Services;
using Microsoft.AspNetCore.Http.HttpResults;
using Microsoft.AspNetCore.Mvc;

namespace LLM.Endpoints;

public static class LlmEndpoints
{
    public static WebApplication MapLlmEndpoints(this WebApplication app)
    {
        var llmApi = app.MapGroup("/api/llm")
            .WithTags("LLM");

        llmApi.MapPost("/prompt", HandlePromptAsync)
            .WithName("GeneratePromptResponse")
            .Accepts<GeneralPromptRequest>("application/json")
            .Produces<JsonNode>(StatusCodes.Status200OK);

        llmApi.MapPost("/code-analysis", HandleCodeAnalysisAsync)
            .WithName("AnalyzeCode")
            .Produces<JsonNode>(StatusCodes.Status200OK);

        return app;
    }

    private static async Task<Ok<JsonNode>> HandlePromptAsync(
        GeneralPromptRequest request,
        LlmClient llmClient)
    {
        var payload = new
        {
            model = "qwen2.5-coder",
            messages = new object[]
            {
                new
                {
                    role = "system",
                    content = request.SystemPrompt
                        ?? "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."
                },
                new { role = "user", content = request.Prompt }
            },
            temperature = request.Temperature ?? 0.2,
            max_tokens = request.MaxTokens ?? 4096
        };

        return TypedResults.Ok(
            await llmClient.SendChatCompletionAsync(payload));
    }

    private static async Task<Ok<JsonNode>> HandleCodeAnalysisAsync(
        [FromForm] CodeAnalysisRequest request,
        LlmClient llmClient,
        PromptBuilder promptBuilder)
    {
        var language = string.IsNullOrWhiteSpace(request.Language)
            ? "c"
            : request.Language.ToLowerInvariant();

        var formattedPrompt = promptBuilder.BuildCodeAnalysisPrompt(
            request.Code,
            request.Instruction,
            language);

        var payload = new
        {
            model = "qwen2.5-coder",
            messages = new[]
            {
                new
                {
                    role = "system",
                    content = request.SystemPrompt
                        ?? "You are an expert software developer and technical reviewer."
                },
                new { role = "user", content = formattedPrompt }
            },
            temperature = 0.1,
            max_tokens = 4096
        };

        return TypedResults.Ok(
            await llmClient.SendChatCompletionAsync(payload));
    }
}