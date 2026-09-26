using Azure.AI.Inference;
using cloudLLM.Interfaces;
using Microsoft.Extensions.Configuration;

namespace cloudLLM.Agents
{
    public class CompilerAgent(ChatCompletionsClient client, IConfiguration configuration) : ILlmAgent
    {
        private const string SystemPrompt =
            "You are an expert C compiler analysis agent. Focus on identifying C syntax errors, " +
            "missing includes, type mismatches, and macro definition issues. For every issue found, " +
            "pinpoint the exact line number in the provided code and provide a clear, guided fix.";

        public ErrorCategory Category => ErrorCategory.CompilerError;

        public async Task<string> AnalyzeAsync(string cCode, string logs, CancellationToken cancellationToken = default)
        {
            var deployment = configuration["AzureAIFoundry:Deployments:Compiler"] ?? "gpt-4o";

            var requestOptions = new ChatCompletionsOptions
            {
                Model = deployment,
                Messages =
                {
                    new ChatRequestSystemMessage(SystemPrompt),
                    new ChatRequestUserMessage($"C Code:\n{cCode}\n\nCompiler Logs:\n{logs}")
                }
            };

            var response = await client.CompleteAsync(requestOptions, cancellationToken);
            return response.Value.Content ?? string.Empty;
        }
    }
}
