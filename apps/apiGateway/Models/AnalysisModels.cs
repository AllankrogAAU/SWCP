namespace api.Models
{
    public enum ErrorCategory
    {
        CompilerError,
        LintWarning,
        RuntimeCrash
    }

    public class AnalysisSubmitRequest
    {
        public ErrorCategory Category { get; set; }
        public string CCode { get; set; } = string.Empty;
        public string Logs { get; set; } = string.Empty;
    }

    public class AnalysisSubmitResponse
    {
        public Guid JobId { get; set; }
    }

    public enum AnalysisJobStatus
    {
        Pending,
        Completed,
        Failed
    }

    public class AnalysisStatusResponse
    {
        public Guid JobId { get; set; }
        public AnalysisJobStatus Status { get; set; }
        public string? Response { get; set; }
        public string? Error { get; set; }
    }
    public class AnalysisRequestMessage
    {
        public Guid JobId { get; set; }
        public string Category { get; set; } = string.Empty;
        public string CCode { get; set; } = string.Empty;
        public string Logs { get; set; } = string.Empty;
    }
    public class AnalysisResultMessage
    {
        public Guid JobId { get; set; }
        public bool Success { get; set; }
        public string? Response { get; set; }
        public string? Error { get; set; }
    }
}
