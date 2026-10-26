using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using coreApi.Models;
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
