using System.Security.Cryptography;
using coreApi.Data;
using coreApi.Models;
using coreApi.Services;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Identity;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using SWCP.Contracts;

namespace SWCP.Tests.Component;

public class FakeNatsTaskPublisher : NatsTaskPublisher
{
    public List<SandboxTask> PublishedSandboxTasks { get; } = [];
    public List<LlmTask> PublishedLlmTasks { get; } = [];

    public override Task PublishSandboxTaskAsync(SandboxTask task, CancellationToken cancellationToken, int attempt = 0)
    {
        PublishedSandboxTasks.Add(task);
        return Task.CompletedTask;
    }

    public override Task PublishLlmTaskAsync(LlmTask task, CancellationToken cancellationToken)
    {
        PublishedLlmTasks.Add(task);
        return Task.CompletedTask;
    }
}

public sealed class CoreApiTestFixture : WebApplicationFactory<coreApi.Program>
{
    private readonly string _tempKeyDir;
    public FakeNatsTaskPublisher TaskPublisher { get; } = new();
    public string DatabaseName { get; } = $"TestDb_{Guid.NewGuid():N}";

    public CoreApiTestFixture()
    {
        _tempKeyDir = Path.Combine(Path.GetTempPath(), $"swcp-coreapi-test-{Guid.NewGuid():N}");
        Directory.CreateDirectory(_tempKeyDir);
        Directory.CreateDirectory(Path.Combine(_tempKeyDir, "public"));
    }

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseEnvironment("Testing");
        builder.ConfigureAppConfiguration((_, config) =>
        {
            config.AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["Jwt:KeyDirectory"] = _tempKeyDir,
                ["Jwt:PublicKeyDirectory"] = Path.Combine(_tempKeyDir, "public"),
                ["Jwt:Issuer"] = "SWCP.CoreApi",
                ["Jwt:Audience"] = "SWCP.Client",
                ["SeedDevelopmentUsers"] = "false",
                ["SeedSampleAssignments"] = "false"
            });
        });

        builder.ConfigureServices(services =>
        {
            services.AddDbContext<CoreDbContext>(options =>
            {
                options.UseInMemoryDatabase(DatabaseName);
                options.ConfigureWarnings(w => w.Ignore(Microsoft.EntityFrameworkCore.Diagnostics.InMemoryEventId.TransactionIgnoredWarning));
            });

            services.RemoveAll<NatsTaskPublisher>();
            services.AddSingleton<NatsTaskPublisher>(TaskPublisher);

            services.RemoveAll<SubmissionEvents>();
            services.AddSingleton<SubmissionEvents>(new SubmissionEvents());

            // Remove NATS background consumers during in-process component tests
            var hostedServices = services
                .Where(s => s.ServiceType == typeof(IHostedService) &&
                            (s.ImplementationType == typeof(SandboxResultConsumer) ||
                             s.ImplementationType == typeof(LlmResultConsumer) ||
                             s.ImplementationType == typeof(SubmissionProgressConsumer) ||
                             s.ImplementationType == typeof(SubmissionReconciler)))
                .ToList();

            foreach (var descriptor in hostedServices)
            {
                services.Remove(descriptor);
            }
        });
    }

    public async Task<User> SeedUserAsync(string username, string password, UserRole role)
    {
        using var scope = Services.CreateScope();
        var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
        var hasher = scope.ServiceProvider.GetRequiredService<IPasswordHasher<User>>();

        var user = new User
        {
            Id = Guid.NewGuid(),
            Username = username.ToLowerInvariant(),
            Role = role,
            CreatedAtUtc = DateTimeOffset.UtcNow
        };
        user.PasswordHash = hasher.HashPassword(user, password);
        db.Users.Add(user);
        await db.SaveChangesAsync();
        return user;
    }

    public async Task<Assignment> SeedAssignmentAsync(string title, string? taskFocus = null)
    {
        using var scope = Services.CreateScope();
        var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();

        var assignment = new Assignment
        {
            Id = Guid.NewGuid(),
            Title = title,
            Description = "Test Assignment Description",
            TaskFocus = taskFocus ?? string.Empty,
            TestCasesJson = "[]",
            CreatedAtUtc = DateTimeOffset.UtcNow
        };
        db.Assignments.Add(assignment);
        await db.SaveChangesAsync();
        return assignment;
    }

    public string GenerateJwt(User user)
    {
        using var scope = Services.CreateScope();
        var tokenService = scope.ServiceProvider.GetRequiredService<JwtTokenService>();
        return tokenService.Create(user).Token;
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        if (Directory.Exists(_tempKeyDir))
        {
            try { Directory.Delete(_tempKeyDir, true); } catch { }
        }
    }
}
