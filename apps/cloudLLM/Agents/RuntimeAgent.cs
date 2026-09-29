using Azure.AI.Inference;
using cloudLLM.Interfaces;
using cloudLLM.Services;

namespace cloudLLM.Agents
{
    public class RuntimeAgent(IChatClientProvider chatClientProvider) : ILlmAgent
    {
        private const string SystemPrompt =
            "You are a deep execution-tracing agent specializing in C runtime failures. Focus on " +
            "memory leaks (malloc/free mismatches), Segmentation Faults (SIGSEGV), dangling pointers, " +
            "and buffer overflows. Explain the stack and heap memory layout conceptually to help the " +
            "user understand why the crash occurred, and provide a guided fix with exact line numbers.";

        public ErrorCategory Category => ErrorCategory.RuntimeCrash;

        public async Task<string> AnalyzeAsync(string cCode, string logs, CancellationToken cancellationToken = default)
        {
            var (client, deployment) = chatClientProvider.GetClient("Gpt6Sol");

            var requestOptions = new ChatCompletionsOptions
            {
                Model = deployment,
                Messages =
                {
                    new ChatRequestSystemMessage(SystemPrompt),
                    new ChatRequestUserMessage($"C Code:\n{cCode}\n\nRuntime Logs:\n{logs}")
                }
            };

            var response = await client.CompleteAsync(requestOptions, cancellationToken);
            return response.Value.Content ?? string.Empty;
        }
    }
}
