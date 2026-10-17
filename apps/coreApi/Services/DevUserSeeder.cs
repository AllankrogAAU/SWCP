using System.Text.Json;
using coreApi.Data;
using coreApi.Models;
using Microsoft.AspNetCore.Identity;
using Microsoft.EntityFrameworkCore;
using SWCP.Contracts;

namespace coreApi.Services;

public static class DevUserSeeder
{
    private static readonly SampleAssignment[] SampleAssignments =
    [
        new(
            Guid.Parse("a1111111-1111-4111-8111-111111111111"),
            "C sandbox smoke test",
            "Write a C program that prints Hello, SWCP! followed by a newline.",
            "Check that the program prints exactly Hello, SWCP! followed by a newline.",
            []),
        new(
            Guid.Parse("a2222222-2222-4222-8222-222222222222"),
            "Print a Number",
            "Write a C program that prints the number 7 followed by a newline.",
            "Check that the program prints the number 7 followed by a newline.",
            [new SandboxTestCase("", "7\n", 2000, 64)]),
        new(
            Guid.Parse("a3333333-3333-4333-8333-333333333333"),
            "Print Two Lines",
            "Write a C program that prints First line and then Second line, each on its own line.",
            "Check that both required lines appear in the correct order, each followed by a newline.",
            [new SandboxTestCase("", "First line\nSecond line\n", 2000, 64)]),
        new(
            Guid.Parse("a4444444-4444-4444-8444-444444444444"),
            "Add Two Integers",
            "Read two integers from standard input and print their sum followed by a newline.",
            "Check that the program reads both integers and prints their sum followed by a newline.",
            [
                new SandboxTestCase("2 3\n", "5\n", 2000, 64),
                new SandboxTestCase("-4 9\n", "5\n", 2000, 64)
            ])
    ];

    public static async Task SeedSampleAssignmentAsync(IServiceProvider services)
    {
        var database = services.GetRequiredService<CoreDbContext>();
        var sampleIds = SampleAssignments.Select(assignment => assignment.Id).ToArray();
        var existingIds = await database.Assignments
            .Where(assignment => sampleIds.Contains(assignment.Id))
            .Select(assignment => assignment.Id)
            .ToHashSetAsync();
        var missingAssignments = SampleAssignments
            .Where(assignment => !existingIds.Contains(assignment.Id))
            .Select(assignment => new Assignment
            {
                Id = assignment.Id,
                Title = assignment.Title,
                Description = assignment.Description,
                TaskFocus = assignment.TaskFocus,
                TestCasesJson = JsonSerializer.Serialize(assignment.TestCases),
                CreatedAtUtc = DateTimeOffset.UtcNow
            })
            .ToArray();

        if (missingAssignments.Length == 0)
        {
            return;
        }

        database.Assignments.AddRange(missingAssignments);
        await database.SaveChangesAsync();
    }

    private sealed record SampleAssignment(
        Guid Id,
        string Title,
        string Description,
        string TaskFocus,
        IReadOnlyList<SandboxTestCase> TestCases);

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