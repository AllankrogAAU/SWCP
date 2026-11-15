using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using coreApi.Data;
using coreApi.Models;
using coreApi.Services;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace SWCP.Tests.Component;

public sealed class AssignmentEndpointsTests : IClassFixture<CoreApiTestFixture>
{
    private readonly HttpClient _client;
    private readonly CoreApiTestFixture _fixture;

    public AssignmentEndpointsTests(CoreApiTestFixture fixture)
    {
        _fixture = fixture;
        _client = fixture.CreateClient();
    }

    [Fact]
    public async Task GetAssignments_Unauthenticated_Returns401Unauthorized()
    {
        var response = await _client.GetAsync("/api/assignments");
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task GetAssignments_AuthenticatedStudent_Returns200WithAssignments()
    {
        var student = await _fixture.SeedUserAsync("student_assign", "pass123", UserRole.Student);
        var assignment = await _fixture.SeedAssignmentAsync("Matrix Multiplication");
        var token = _fixture.GenerateJwt(student);

        using var request = new HttpRequestMessage(HttpMethod.Get, "/api/assignments");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);

        var response = await _client.SendAsync(request);

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        var assignments = await response.Content.ReadFromJsonAsync<List<AssignmentListResponse>>();
        Assert.NotNull(assignments);
        Assert.Contains(assignments, a => a.Id == assignment.Id);
    }

    [Fact]
    public async Task SeedSampleAssignments_SeedsAllIntroAssignmentsInFrontendList()
    {
        using (var seedScope = _fixture.Services.CreateScope())
        {
            await DevUserSeeder.SeedSampleAssignmentAsync(seedScope.ServiceProvider);
        }
        using (var editScope = _fixture.Services.CreateScope())
        {
            var database = editScope.ServiceProvider.GetRequiredService<CoreDbContext>();
            var triangle = await database.Assignments.SingleAsync(assignment =>
                assignment.Title == "09. Star triangle (L4)");
            triangle.HarnessSource = "stale harness";
            await database.SaveChangesAsync();
        }
        using (var reseedScope = _fixture.Services.CreateScope())
        {
            await DevUserSeeder.SeedSampleAssignmentAsync(reseedScope.ServiceProvider);
        }
        var student = await _fixture.SeedUserAsync("student_intro_seed", "pass123", UserRole.Student);
        var token = _fixture.GenerateJwt(student);

        using var request = new HttpRequestMessage(HttpMethod.Get, "/api/assignments");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        var response = await _client.SendAsync(request);

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        var assignments = await response.Content.ReadFromJsonAsync<List<AssignmentListResponse>>();
        Assert.NotNull(assignments);

        var introAssignments = assignments
            .Where(assignment => assignment.Title.Length >= 3 &&
                int.TryParse(assignment.Title[..2], out var number) &&
                number is >= 1 and <= 25)
            .OrderBy(assignment => assignment.Title)
            .ToArray();
        Assert.Equal(25, introAssignments.Length);
        for (var index = 0; index < introAssignments.Length; index++)
        {
            Assert.StartsWith($"{index + 1:D2}. ", introAssignments[index].Title);
        }

        using var scope = _fixture.Services.CreateScope();
        var database = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
        var seededRows = (await database.Assignments.ToListAsync())
            .Where(assignment => assignment.Title.Length >= 3 &&
                int.TryParse(assignment.Title[..2], out var number) &&
                number is >= 1 and <= 25)
            .ToArray();
        Assert.Equal(25, seededRows.Length);
        Assert.All(seededRows, assignment =>
        {
            using var testCases = JsonDocument.Parse(assignment.TestCasesJson);
            Assert.True(testCases.RootElement.GetArrayLength() > 0);
            Assert.NotEmpty(assignment.TaskFocus);
        });
        var standaloneTitles = new[]
        {
            "01. Hello, kursus (L1)",
            "03. Seconds to h/m/s (L2)",
            "09. Star triangle (L4)"
        };
        Assert.All(seededRows.Where(assignment => !standaloneTitles.Contains(assignment.Title)),
            assignment => Assert.False(string.IsNullOrWhiteSpace(assignment.HarnessSource)));
        Assert.All(seededRows.Where(assignment => standaloneTitles.Contains(assignment.Title)),
            assignment => Assert.True(string.IsNullOrWhiteSpace(assignment.HarnessSource)));
        Assert.Equal(
            "Write a complete C program that reads a positive integer n and prints n rows: row 1 has one '*', row 2 has two, and so on. Each row ends with a newline.",
            seededRows.Single(assignment => assignment.Title == "09. Star triangle (L4)").Description);
    }

    [Fact]
    public async Task GetAssignmentById_NonExistent_Returns404NotFound()
    {
        var student = await _fixture.SeedUserAsync("student_notfound", "pass123", UserRole.Student);
        var token = _fixture.GenerateJwt(student);

        using var request = new HttpRequestMessage(HttpMethod.Get, $"/api/assignments/{Guid.NewGuid()}");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);

        var response = await _client.SendAsync(request);
        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public async Task CreateAssignment_AsStudent_Returns403Forbidden()
    {
        var student = await _fixture.SeedUserAsync("student_forbidden", "pass123", UserRole.Student);
        var token = _fixture.GenerateJwt(student);

        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/assignments");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Content = JsonContent.Create(new CreateAssignmentRequest(
            "New Task", "Description", string.Empty, JsonDocument.Parse("[]").RootElement));

        var response = await _client.SendAsync(request);
        Assert.Equal(HttpStatusCode.Forbidden, response.StatusCode);
    }

    [Fact]
    public async Task CreateAssignment_AsTeacher_Returns201Created()
    {
        var teacher = await _fixture.SeedUserAsync("teacher_admin", "pass123", UserRole.Teacher);
        var token = _fixture.GenerateJwt(teacher);

        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/assignments");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Content = JsonContent.Create(new CreateAssignmentRequest(
            "Teacher Created Task", "Detailed problem description", "Focus on memory safety", JsonDocument.Parse("[]").RootElement));

        var response = await _client.SendAsync(request);
        Assert.Equal(HttpStatusCode.Created, response.StatusCode);

        var created = await response.Content.ReadFromJsonAsync<AssignmentResponse>();
        Assert.NotNull(created);
        Assert.Equal("Teacher Created Task", created.Title);
        Assert.Equal("Focus on memory safety", created.TaskFocus);
    }

    [Fact]
    public async Task CreateAssignment_MissingTitle_Returns400BadRequest()
    {
        var teacher = await _fixture.SeedUserAsync("teacher_badreq", "pass123", UserRole.Teacher);
        var token = _fixture.GenerateJwt(teacher);

        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/assignments");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Content = JsonContent.Create(new CreateAssignmentRequest(
            "", "Description only", string.Empty, JsonDocument.Parse("[]").RootElement));

        var response = await _client.SendAsync(request);
        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }
}
