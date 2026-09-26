using Azure.AI.Inference;
using cloudLLM.Interfaces;
using cloudLLM.Services;

namespace cloudLLM.Agents
{
    public class LintAgent(IChatClientProvider chatClientProvider) : ILlmAgent
    {
        private const string SystemPrompt =
            "You are a meticulous C linting agent. Focus on code formatting, naming conventions, " +
            "unused variables, and C idiomatic best practices. Provide actionable suggestions " +
            "referencing exact line numbers where possible.";

        public ErrorCategory Category => ErrorCategory.LintWarning;

        public async Task<string> AnalyzeAsync(string cCode, string logs, CancellationToken cancellationToken = default)
        {
            var (client, deployment) = chatClientProvider.GetClient("Gpt4o");

            var requestOptions = new ChatCompletionsOptions
            {
                Model = deployment,
                Messages =
                {
                    new ChatRequestSystemMessage(SystemPrompt),
                    new ChatRequestUserMessage($"C Code:\n{cCode}\n\nLint Logs:\n{logs}")
                }
            };

            var response = await client.CompleteAsync(requestOptions, cancellationToken);
            return response.Value.Content ?? string.Empty;
        }
    }
}
