using cloudLLM.Agents;

namespace cloudLLM.Interfaces
{
    public interface ILlmAgent
    {
        ErrorCategory Category { get; }

        Task<string> AnalyzeAsync(string cCode, string logs, CancellationToken cancellationToken = default);
    }
}
