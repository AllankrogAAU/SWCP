using OpenAI.Chat;
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
            var (client, _) = chatClientProvider.GetClient("Gpt4o");

            var messages = new ChatMessage[]
            {
                new SystemChatMessage(SystemPrompt),
                new UserChatMessage($"C Code:\n{cCode}\n\nLint Logs:\n{logs}")
            };

            var response = await client.CompleteChatAsync(messages, cancellationToken: cancellationToken);
            return response.Value.Content.Count > 0 ? response.Value.Content[0].Text : string.Empty;
        }
    }
}