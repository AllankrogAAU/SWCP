using System.Net;
using System.Security.Cryptography;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;
using Xunit;

namespace SWCP.Tests.Component;

public sealed class ApiGatewayTestFixture : WebApplicationFactory<api.Program>
{
    private readonly string _tempKeyDir;
    public RSA PublicRsa { get; } = RSA.Create(2048);

    public ApiGatewayTestFixture()
    {
        _tempKeyDir = Path.Combine(Path.GetTempPath(), $"swcp-gateway-test-{Guid.NewGuid():N}");
        Directory.CreateDirectory(_tempKeyDir);
        var pubKeyPath = Path.Combine(_tempKeyDir, "jwt-public.pem");
        File.WriteAllText(pubKeyPath, PublicRsa.ExportSubjectPublicKeyInfoPem());
    }

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseEnvironment("Testing");
        builder.ConfigureAppConfiguration((_, config) =>
        {
            config.AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["Jwt:PublicKeyPath"] = Path.Combine(_tempKeyDir, "jwt-public.pem"),
                ["Jwt:Issuer"] = "SWCP.CoreApi",
                ["Jwt:Audience"] = "SWCP.Client",
                ["Nats:Url"] = "nats://localhost:4222",
                ["CoreApi:Url"] = "http://localhost:8081"
            });
        });
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        PublicRsa.Dispose();
        if (Directory.Exists(_tempKeyDir))
        {
            try { Directory.Delete(_tempKeyDir, true); } catch { }
        }
    }
}

public sealed class ApiGatewayTests : IClassFixture<ApiGatewayTestFixture>
{
    private readonly HttpClient _client;

    public ApiGatewayTests(ApiGatewayTestFixture fixture)
    {
        _client = fixture.CreateClient();
    }

    [Fact]
    public async Task Health_Returns200Ok()
    {
        var response = await _client.GetAsync("/health");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
    }

    [Fact]
    public async Task ReverseProxy_ForwardsApiRequestToCoreApi()
    {
        // YARP reverse proxies /api requests to CoreApi; when CoreApi is offline in this isolated gateway test, returns 502 Bad Gateway
        var response = await _client.GetAsync($"/api/submissions/{Guid.NewGuid()}/events");
        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
    }
}

