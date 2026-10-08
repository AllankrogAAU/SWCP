using coreApi.Data;
using coreApi.Models;
using Microsoft.AspNetCore.Identity;
using Microsoft.EntityFrameworkCore;

namespace coreApi.Services;

public static class DevUserSeeder
{
    public static async Task SeedSampleAssignmentAsync(IServiceProvider services)
    {
        var database = services.GetRequiredService<CoreDbContext>();
        if (await database.Assignments.AnyAsync())
        {
            return;
        }

        database.Assignments.Add(new Assignment
        {
            Id = Guid.Parse("a1111111-1111-4111-8111-111111111111"),
            Title = "C sandbox smoke test",
            Description = "Write a C program that prints Hello, SWCP! followed by a newline.",
            SystemPromptTemplate = "You are a programming tutor. Explain compiler and runtime feedback clearly and concisely.",
            TestCasesJson = "[]",
            CreatedAtUtc = DateTimeOffset.UtcNow
        });
        await database.SaveChangesAsync();
    }

    public static async Task SeedAsync(IServiceProvider services, IConfiguration configuration)
    {
        if (!configuration.GetValue<bool>("SeedDevelopmentUsers"))
        {
            return;
        }

        var username = (configuration["DevelopmentUsers:TeacherUsername"] ?? configuration["DevelopmentUsers:TeacherEmail"])
            ?.Trim().ToLowerInvariant();
        var password = configuration["DevelopmentUsers:TeacherPassword"];
        if (string.IsNullOrWhiteSpace(username) || string.IsNullOrWhiteSpace(password))
        {
            throw new InvalidOperationException(
                "Development teacher username and password must be set when SeedDevelopmentUsers is enabled.");
        }

        var database = services.GetRequiredService<CoreDbContext>();
        if (await database.Users.AnyAsync(user => user.Username == username))
        {
            return;
        }

        var user = new User
        {
            Id = Guid.NewGuid(),
            Username = username,
            Role = UserRole.Teacher,
            CreatedAtUtc = DateTimeOffset.UtcNow
        };
        user.PasswordHash = services.GetRequiredService<IPasswordHasher<User>>().HashPassword(user, password);
        database.Users.Add(user);
        await database.SaveChangesAsync();
    }
}