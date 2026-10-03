namespace api.Models;

public sealed class AgentAnalyzeRequest
{
    public string CCode { get; set; } = string.Empty;
    public string Logs { get; set; } = string.Empty;
}