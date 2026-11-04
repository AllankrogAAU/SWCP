using System.IdentityModel.Tokens.Jwt;
using System.Security.Cryptography;
using coreApi.Models;
using coreApi.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.IdentityModel.Tokens;
using Xunit;

namespace SWCP.Tests.Unit;

public sealed class JwtTokenServiceTests : IDisposable
{
    private readonly string _tempDir;
    private readonly RsaSigningKeys _signingKeys;
    private readonly IConfiguration _configuration;

    public JwtTokenServiceTests()
    {
        _tempDir = Path.Combine(Path.GetTempPath(), $"swcp-test-keys-{Guid.NewGuid():N}");
        _signingKeys = RsaSigningKeys.LoadOrCreate(_tempDir, Path.Combine(_tempDir, "public"));
        _configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["Jwt:Issuer"] = "SWCP.CoreApi",
                ["Jwt:Audience"] = "SWCP.Client",
                ["Jwt:ExpiryMinutes"] = "60"
            })
            .Build();
    }

    [Fact]
    public void Create_ValidUser_GeneratesVerifiableRs256Jwt()
    {
        var service = new JwtTokenService(_configuration, _signingKeys);
        var user = new User
        {
            Id = Guid.NewGuid(),
            Username = "teststudent",
            Role = UserRole.Student,
            CreatedAtUtc = DateTimeOffset.UtcNow
        };

        var (token, expires) = service.Create(user);

        Assert.NotNull(token);
        Assert.True(expires > DateTimeOffset.UtcNow);

        var handler = new JwtSecurityTokenHandler();
        var principal = handler.ValidateToken(token, new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = "SWCP.CoreApi",
            ValidateAudience = true,
            ValidAudience = "SWCP.Client",
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = new RsaSecurityKey(_signingKeys.Rsa),
            ValidateLifetime = true
        }, out var validatedToken);

        Assert.NotNull(principal);
        Assert.Equal(user.Id.ToString(), principal.FindFirst(System.Security.Claims.ClaimTypes.NameIdentifier)?.Value);
        Assert.Equal("Student", principal.FindFirst(System.Security.Claims.ClaimTypes.Role)?.Value);
    }

    [Fact]
    public void ValidateToken_WithTamperedPayload_FailsVerification()
    {
        var service = new JwtTokenService(_configuration, _signingKeys);
        var user = new User
        {
            Id = Guid.NewGuid(),
            Username = "teststudent",
            Role = UserRole.Student,
            CreatedAtUtc = DateTimeOffset.UtcNow
        };

        var (token, _) = service.Create(user);
        var tamperedToken = token[..^5] + "XXXXX";

        var handler = new JwtSecurityTokenHandler();
        Assert.ThrowsAny<SecurityTokenException>(() =>
            handler.ValidateToken(tamperedToken, new TokenValidationParameters
            {
                ValidateIssuer = true,
                ValidIssuer = "SWCP.CoreApi",
                ValidateAudience = true,
                ValidAudience = "SWCP.Client",
                ValidateIssuerSigningKey = true,
                IssuerSigningKey = new RsaSecurityKey(_signingKeys.Rsa),
                ValidateLifetime = true
            }, out _));
    }

    [Fact]
    public void ValidateToken_WithDifferentKey_FailsSignature()
    {
        var service = new JwtTokenService(_configuration, _signingKeys);
        var user = new User
        {
            Id = Guid.NewGuid(),
            Username = "teststudent",
            Role = UserRole.Student,
            CreatedAtUtc = DateTimeOffset.UtcNow
        };

        var (token, _) = service.Create(user);

        using var differentRsa = RSA.Create(2048);
        var handler = new JwtSecurityTokenHandler();

        Assert.ThrowsAny<SecurityTokenException>(() =>
            handler.ValidateToken(token, new TokenValidationParameters
            {
                ValidateIssuer = true,
                ValidIssuer = "SWCP.CoreApi",
                ValidateAudience = true,
                ValidAudience = "SWCP.Client",
                ValidateIssuerSigningKey = true,
                IssuerSigningKey = new RsaSecurityKey(differentRsa),
                ValidateLifetime = true
            }, out _));
    }

    public void Dispose()
    {
        _signingKeys.Dispose();
        if (Directory.Exists(_tempDir))
        {
            Directory.Delete(_tempDir, true);
        }
    }
}
