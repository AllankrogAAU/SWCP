using System.Net;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using coreApi.Data;
using coreApi.Models;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace SWCP.Tests.Component;

public sealed class SubmissionEndpointsTests : IClassFixture<CoreApiTestFixture>
{
    private readonly HttpClient _client;
    private readonly CoreApiTestFixture _fixture;

    public SubmissionEndpointsTests(CoreApiTestFixture fixture)
    {
        _fixture = fixture;
        _client = fixture.CreateClient();
    }

    [Fact]
    public async Task CreateSubmission_ValidStudent_Returns202AcceptedAndPublishesTask()
    {
        var student = await _fixture.SeedUserAsync("student_submitter", "pass123", UserRole.Student);
        var assignment = await _fixture.SeedAssignmentAsync("Prime Factorization");
        var token = _fixture.GenerateJwt(student);

        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/submissions");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Content = JsonContent.Create(new CreateSubmissionRequest(
            assignment.Id, "int main() { return 0; }", "azure", "submit"));

        var response = await _client.SendAsync(request);

        Assert.Equal(HttpStatusCode.Accepted, response.StatusCode);
        var json = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.True(json.TryGetProperty("submissionId", out var submissionIdProp));
        Assert.Equal("SANDBOX_QUEUED", json.GetProperty("status").GetString());

        var publishedTask = _fixture.TaskPublisher.PublishedSandboxTasks
            .FirstOrDefault(t => t.SubmissionId == submissionIdProp.GetGuid());
        Assert.NotNull(publishedTask);
        Assert.Equal(assignment.Id, publishedTask.AssignmentId);
        Assert.Equal("int main() { return 0; }", publishedTask.Source);
    }

    [Fact]
    public async Task CreateSubmission_Unauthenticated_Returns401Unauthorized()
    {
        var response = await _client.PostAsJsonAsync("/api/submissions", new CreateSubmissionRequest(
            Guid.NewGuid(), "int main() {}", "azure", "submit"));
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task CreateSubmission_EmptySource_Returns400BadRequest()
    {
        var student = await _fixture.SeedUserAsync("student_empty", "pass123", UserRole.Student);
        var assignment = await _fixture.SeedAssignmentAsync("Empty Source Assignment");
        var token = _fixture.GenerateJwt(student);

        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/submissions");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Content = JsonContent.Create(new CreateSubmissionRequest(
            assignment.Id, "", "azure", "submit"));

        var response = await _client.SendAsync(request);
        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Fact]
    public async Task CreateSubmission_InvalidBackend_Returns400BadRequest()
    {
        var student = await _fixture.SeedUserAsync("student_invalid_backend", "pass123", UserRole.Student);
        var assignment = await _fixture.SeedAssignmentAsync("Backend Assignment");
        var token = _fixture.GenerateJwt(student);

        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/submissions");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Content = JsonContent.Create(new CreateSubmissionRequest(
            assignment.Id, "int main() {}", "invalid_backend", "submit"));

        var response = await _client.SendAsync(request);
        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Fact]
    public async Task CreateSubmission_NonExistentAssignment_Returns404NotFound()
    {
        var student = await _fixture.SeedUserAsync("student_bad_assign", "pass123", UserRole.Student);
        var token = _fixture.GenerateJwt(student);

        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/submissions");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Content = JsonContent.Create(new CreateSubmissionRequest(
            Guid.NewGuid(), "int main() {}", "azure", "submit"));

        var response = await _client.SendAsync(request);
        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public async Task GetSubmission_OtherUsersSubmission_Returns403ForbiddenForSecurityIsolation()
    {
        var owner = await _fixture.SeedUserAsync("student_owner", "pass123", UserRole.Student);
        var stranger = await _fixture.SeedUserAsync("student_stranger", "pass123", UserRole.Student);
        var assignment = await _fixture.SeedAssignmentAsync("Isolation Test Assignment");

        var ownerToken = _fixture.GenerateJwt(owner);
        using var submitReq = new HttpRequestMessage(HttpMethod.Post, "/api/submissions");
        submitReq.Headers.Authorization = new AuthenticationHeaderValue("Bearer", ownerToken);
        submitReq.Content = JsonContent.Create(new CreateSubmissionRequest(
            assignment.Id, "int main() {}", "azure", "submit"));

        var submitResp = await _client.SendAsync(submitReq);
        var submitJson = await submitResp.Content.ReadFromJsonAsync<JsonElement>();
        var submissionId = submitJson.GetProperty("submissionId").GetGuid();

        var strangerToken = _fixture.GenerateJwt(stranger);
        using var accessReq = new HttpRequestMessage(HttpMethod.Get, $"/api/submissions/{submissionId}");
        accessReq.Headers.Authorization = new AuthenticationHeaderValue("Bearer", strangerToken);

        var accessResp = await _client.SendAsync(accessReq);
        Assert.Equal(HttpStatusCode.Forbidden, accessResp.StatusCode);
    }

    [Fact]
    public async Task GetSubmission_ReturnsStoredConfidenceScore()
    {
        var student = await _fixture.SeedUserAsync("student_confidence", "pass123", UserRole.Student);
        var assignment = await _fixture.SeedAssignmentAsync("Confidence Score Assignment");
        var token = _fixture.GenerateJwt(student);

        using var submitRequest = new HttpRequestMessage(HttpMethod.Post, "/api/submissions");
        submitRequest.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        submitRequest.Content = JsonContent.Create(new CreateSubmissionRequest(
            assignment.Id, "int main() { return 0; }", "local", "submit"));

        var submitResponse = await _client.SendAsync(submitRequest);
        Assert.Equal(HttpStatusCode.Accepted, submitResponse.StatusCode);
        var submitJson = await submitResponse.Content.ReadFromJsonAsync<JsonElement>();
        var submissionId = submitJson.GetProperty("submissionId").GetGuid();

        await using (var scope = _fixture.Services.CreateAsyncScope())
        {
            var database = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            var submission = await database.Submissions.SingleAsync(item => item.Id == submissionId);
            submission.ConfidenceScorePercent = 87.5;
            await database.SaveChangesAsync();
        }

        using var getRequest = new HttpRequestMessage(HttpMethod.Get, $"/api/submissions/{submissionId}");
        getRequest.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        var getResponse = await _client.SendAsync(getRequest);

        Assert.Equal(HttpStatusCode.OK, getResponse.StatusCode);
        var getJson = await getResponse.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(87.5, getJson.GetProperty("confidenceScorePercent").GetDouble());
    }

    [Fact]
    public async Task CancelSubmission_OwnerMarksFailedAndUnblocksNextSubmission()
    {
        var owner = await _fixture.SeedUserAsync("student_cancel_owner", "pass123", UserRole.Student);
        var stranger = await _fixture.SeedUserAsync("student_cancel_stranger", "pass123", UserRole.Student);
        var assignment = await _fixture.SeedAssignmentAsync("Cancel Test Assignment");

        var ownerToken = _fixture.GenerateJwt(owner);
        using var submitReq = new HttpRequestMessage(HttpMethod.Post, "/api/submissions/hint");
        submitReq.Headers.Authorization = new AuthenticationHeaderValue("Bearer", ownerToken);
        submitReq.Content = JsonContent.Create(new CreateSubmissionRequest(
            assignment.Id, "int main() {}", "local", "hint"));

        var submitResp = await _client.SendAsync(submitReq);
        Assert.Equal(HttpStatusCode.Accepted, submitResp.StatusCode);
        var submitJson = await submitResp.Content.ReadFromJsonAsync<JsonElement>();
        var submissionId = submitJson.GetProperty("submissionId").GetGuid();

        var strangerToken = _fixture.GenerateJwt(stranger);
        using var forbiddenReq = new HttpRequestMessage(HttpMethod.Post, $"/api/submissions/{submissionId}/cancel");
        forbiddenReq.Headers.Authorization = new AuthenticationHeaderValue("Bearer", strangerToken);
        var forbiddenResp = await _client.SendAsync(forbiddenReq);
        Assert.Equal(HttpStatusCode.Forbidden, forbiddenResp.StatusCode);

        using var cancelReq = new HttpRequestMessage(HttpMethod.Post, $"/api/submissions/{submissionId}/cancel");
        cancelReq.Headers.Authorization = new AuthenticationHeaderValue("Bearer", ownerToken);
        var cancelResp = await _client.SendAsync(cancelReq);
        Assert.Equal(HttpStatusCode.OK, cancelResp.StatusCode);
        var cancelJson = await cancelResp.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal("FAILED", cancelJson.GetProperty("status").GetString());
        Assert.Equal("Canceled by user.", cancelJson.GetProperty("errorMessage").GetString());

        using var retryReq = new HttpRequestMessage(HttpMethod.Post, "/api/submissions/hint");
        retryReq.Headers.Authorization = new AuthenticationHeaderValue("Bearer", ownerToken);
        retryReq.Content = JsonContent.Create(new CreateSubmissionRequest(
            assignment.Id, "int main() {}", "local", "hint"));
        var retryResp = await _client.SendAsync(retryReq);
        Assert.Equal(HttpStatusCode.Accepted, retryResp.StatusCode);
    }

    [Fact]
    public async Task GetEvents_Unauthenticated_Returns401Unauthorized()
    {
        var response = await _client.GetAsync($"/api/submissions/{Guid.NewGuid()}/events");
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task GetEvents_Authenticated_ReturnsEventStream()
    {
        var student = await _fixture.SeedUserAsync("student_events", "pass123", UserRole.Student);
        var assignment = await _fixture.SeedAssignmentAsync("Events Assignment");
        var token = _fixture.GenerateJwt(student);

        using var submitReq = new HttpRequestMessage(HttpMethod.Post, "/api/submissions");
        submitReq.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        submitReq.Content = JsonContent.Create(new CreateSubmissionRequest(
            assignment.Id, "int main() { return 0; }", "azure", "submit"));
        var submitResp = await _client.SendAsync(submitReq);
        Assert.Equal(HttpStatusCode.Accepted, submitResp.StatusCode);
        var submitJson = await submitResp.Content.ReadFromJsonAsync<JsonElement>();
        var submissionId = submitJson.GetProperty("submissionId").GetGuid();

        using var request = new HttpRequestMessage(HttpMethod.Get, $"/api/submissions/{submissionId}/events");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("text/event-stream"));

        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        var response = await _client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cts.Token);

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("text/event-stream", response.Content.Headers.ContentType?.MediaType);
    }
}
