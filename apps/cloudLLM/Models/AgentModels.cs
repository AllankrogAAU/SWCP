using cloudLLM.Agents;

namespace cloudLLM.Models
{
    public class AgentAnalyzeRequest
    {
        public string CCode { get; set; } = string.Empty;
        public string Logs { get; set; } = string.Empty;
    }

    public class AgentAnalyzeResponse
    {
        public ErrorCategory Category { get; set; }
        public string Response { get; set; } = string.Empty;
    }
}
