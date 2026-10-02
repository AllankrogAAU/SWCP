namespace cloudLLM.Models
{
    public class AnalysisResultMessage
    {
        public Guid JobId { get; set; }

        public bool Success { get; set; }

        public string? Response { get; set; }

        public string? Error { get; set; }
    }
}
