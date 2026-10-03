using OpenAI.Chat;
using cloudLLM.Interfaces;
using cloudLLM.Services;

namespace cloudLLM.Agents
{
    public class CompilerAgent(IChatClientProvider chatClientProvider) : ILlmAgent
    {
        private const string SystemPrompt =
            "You are an expert C compiler analysis agent. Focus on identifying C syntax errors, " +
            "missing includes, type mismatches, and macro definition issues. For every issue found, " +
            "pinpoint the exact line number in the provided code and provide a clear, guided fix.";

        public ErrorCategory Category => ErrorCategory.CompilerError;

        public async Task<string> AnalyzeAsync(string cCode, string logs, CancellationToken cancellationToken = default)
        {
            var (client, _) = chatClientProvider.GetClient("Gpt4o");

            var messages = new ChatMessage[]
            {
                new SystemChatMessage(SystemPrompt),
                new UserChatMessage($"C Code:\n{cCode}\n\nCompiler Logs:\n{logs}")
            };

            var response = await client.CompleteChatAsync(messages, cancellationToken: cancellationToken);
            return response.Value.Content.Count > 0 ? response.Value.Content[0].Text : string.Empty;
        }
    }
}