namespace api.Models
{
    public enum UserRole
    {
        Student,
        Teacher
    }

    public class LoginRequest
    {
        public string UserName { get; set; } = string.Empty;
        public UserRole Role { get; set; }
    }

    public class LoginResponse
    {
        public string Token { get; set; } = string.Empty;
        public DateTime ExpiresAtUtc { get; set; }
    }
}
