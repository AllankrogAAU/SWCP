using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using coreApi.Models;
using Xunit;

namespace SWCP.Tests.Component;

public sealed class AuthEndpointsTests : IClassFixture<CoreApiTestFixture>
{
    private readonly HttpClient _client;
    private readonly CoreApiTestFixture _fixture;

    public AuthEndpointsTests(CoreApiTestFixture fixture)
    {
        _fixture = fixture;
        _client = fixture.CreateClient();
    }

    [Fact]
    public async Task Register_ValidStudent_Returns201Created()
    {
        var username = $"student_{Guid.NewGuid():N}"[..12];
        var response = await _client.PostAsJsonAsync("/api/auth/register", new RegisterRequest(username, "Password123!"));

        Assert.Equal(HttpStatusCode.Created, response.StatusCode);
        var json = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(username, json.GetProperty("username").GetString());
        Assert.Equal("Student", json.GetProperty("role").GetString());
    }

    [Fact]
    public async Task Register_DuplicateUsername_Returns409Conflict()
    {
        var username = $"dup_{Guid.NewGuid():N}"[..12];
        await _fixture.SeedUserAsync(username, "Password123!", UserRole.Student);

        var response = await _client.PostAsJsonAsync("/api/auth/register", new RegisterRequest(username, "Password123!"));

        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        var body = await response.Content.ReadAsStringAsync();
        Assert.Contains("already exists", body);
    }

    [Theory]
    [InlineData("a", "validpass")]
    [InlineData("user with spaces", "validpass")]
    [InlineData("validuser", "123")]
    public async Task Register_InvalidUsernameOrShortPassword_Returns400BadRequest(string username, string password)
    {
        var response = await _client.PostAsJsonAsync("/api/auth/register", new RegisterRequest(username, password));
        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Fact]
    public async Task Login_ValidCredentials_Returns200WithAccessToken()
    {
        var username = $"login_{Guid.NewGuid():N}"[..12];
        await _fixture.SeedUserAsync(username, "SecretPass123", UserRole.Student);

        var response = await _client.PostAsJsonAsync("/api/auth/login", new LoginRequest(username, "SecretPass123"));

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        var json = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.True(json.TryGetProperty("accessToken", out var token));
        Assert.False(string.IsNullOrWhiteSpace(token.GetString()));
    }

    [Fact]
    public async Task Login_InvalidPassword_Returns401Unauthorized()
    {
        var username = $"wrongpass_{Guid.NewGuid():N}"[..12];
        await _fixture.SeedUserAsync(username, "CorrectPassword", UserRole.Student);

        var response = await _client.PostAsJsonAsync("/api/auth/login", new LoginRequest(username, "WrongPassword"));
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Fact]
    public async Task Login_NonExistentUser_Returns401Unauthorized()
    {
        var response = await _client.PostAsJsonAsync("/api/auth/login", new LoginRequest("ghostuser_999", "AnyPassword"));
        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }
}

