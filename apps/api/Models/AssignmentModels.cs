namespace api.Models
{
    public class Assignment
    {
        public Guid Id { get; set; }
        public string Title { get; set; } = string.Empty;
        public string AssignmentText { get; set; } = string.Empty;
        public string Focus { get; set; } = string.Empty;
        public DateTime CreatedAtUtc { get; set; }
        public DateTime? UpdatedAtUtc { get; set; }
    }

    public class CreateAssignmentRequest
    {
        public string Title { get; set; } = string.Empty;
        public string AssignmentText { get; set; } = string.Empty;
        public string Focus { get; set; } = string.Empty;
    }

    public class UpdateAssignmentRequest
    {
        public string Title { get; set; } = string.Empty;
        public string AssignmentText { get; set; } = string.Empty;
        public string Focus { get; set; } = string.Empty;
    }

    public class AssignmentResponse
    {
        public Guid Id { get; set; }
        public string Title { get; set; } = string.Empty;
        public string AssignmentText { get; set; } = string.Empty;
        public string? Focus { get; set; }
        public DateTime CreatedAtUtc { get; set; }
        public DateTime? UpdatedAtUtc { get; set; }
    }

    public class AssignmentTitleResponse
    {
        public Guid Id { get; set; }
        public string Title { get; set; } = string.Empty;
    }
}
