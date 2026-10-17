using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using coreApi.Models;
using Microsoft.IdentityModel.Tokens;

namespace coreApi.Services;

public sealed class JwtTokenService(IConfiguration configuration, RsaSigningKeys signingKeys)
{
    public (string Token, DateTimeOffset ExpiresAtUtc) Create(User user)
    {
        var jwt = configuration.GetSection("Jwt");
        var now = DateTime.UtcNow;
        var expires = now.AddMinutes(jwt.GetValue("ExpiryMinutes", 60));
        var claims = new[]
        {
            new Claim(JwtRegisteredClaimNames.Sub, user.Id.ToString()),
            new Claim(JwtRegisteredClaimNames.PreferredUsername, user.Username),
            new Claim(ClaimTypes.NameIdentifier, user.Id.ToString()),
            new Claim(ClaimTypes.Name, user.Username),
            new Claim(ClaimTypes.Role, user.Role.ToString())
        };
        var credentials = new SigningCredentials(
            new RsaSecurityKey(signingKeys.Rsa) { KeyId = signingKeys.KeyId },
            SecurityAlgorithms.RsaSha256);
        var token = new JwtSecurityToken(
            issuer: jwt["Issuer"],
            audience: jwt["Audience"],
            claims: claims,
            notBefore: now,
            expires: expires,
            signingCredentials: credentials);

        return (new JwtSecurityTokenHandler().WriteToken(token), expires);
    }
}