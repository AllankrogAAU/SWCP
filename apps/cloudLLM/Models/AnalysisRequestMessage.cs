namespace cloudLLM.Models
{
    public class AnalysisRequestMessage
    {
        public Guid JobId { get; set; }

        public string Category { get; set; } = string.Empty;

        public string CCode { get; set; } = string.Empty;

        public string Logs { get; set; } = string.Empty;
    }
}
