using coreApi.Data;
using coreApi.Models;
using coreApi.Services;
using Microsoft.AspNetCore.Identity;
using Microsoft.EntityFrameworkCore;

namespace coreApi.Endpoints;

public static class AuthEndpoints
{
    public static RouteGroupBuilder MapAuthEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/auth").WithTags("Authentication");
        group.MapPost("/register", Register).AllowAnonymous();
        group.MapPost("/login", Login).AllowAnonymous();
        return group;
    }

    private static async Task<IResult> Register(
        RegisterRequest request,
        CoreDbContext database,
        IPasswordHasher<User> passwordHasher,
        CancellationToken cancellationToken)
    {
        var username = request.Username?.Trim().ToLowerInvariant();
        if (!IsValidUsername(username) || request.Password is null || request.Password.EnumerateRunes().Count() < 5)
        {
            return Results.BadRequest(new { error = "Use a 3-32 character username (letters, numbers, '.', '_' or '-') and a password of at least 5 characters." });
        }
        var normalizedUsername = username!;

        if (await database.Users.AnyAsync(user => user.Username == normalizedUsername, cancellationToken))
        {
            return Results.Conflict(new { error = "An account with this username already exists." });
        }

        var user = new User
        {
            Id = Guid.NewGuid(),
            Username = normalizedUsername,
            Role = UserRole.Student,
            CreatedAtUtc = DateTimeOffset.UtcNow
        };
        user.PasswordHash = passwordHasher.HashPassword(user, request.Password);
        database.Users.Add(user);
        await database.SaveChangesAsync(cancellationToken);
        return Results.Created($"/api/users/{user.Id}", new { user.Id, user.Username, user.Role });
    }

    private static async Task<IResult> Login(
        LoginRequest request,
        CoreDbContext database,
        IPasswordHasher<User> passwordHasher,
        JwtTokenService tokenService,
        CancellationToken cancellationToken)
    {
        var username = request.Username?.Trim().ToLowerInvariant();
        if (string.IsNullOrWhiteSpace(username) || request.Password is null)
        {
            return Results.Unauthorized();
        }

        var user = await database.Users.SingleOrDefaultAsync(candidate => candidate.Username == username, cancellationToken);
        if (user is null || passwordHasher.VerifyHashedPassword(user, user.PasswordHash, request.Password)
            == PasswordVerificationResult.Failed)
        {
            return Results.Unauthorized();
        }

        var (token, expires) = tokenService.Create(user);
        return Results.Ok(new LoginResponse(token, "Bearer", expires));
    }

    private static bool IsValidUsername(string? username) =>
        username is { Length: >= 3 and <= 32 } &&
        username.All(character => char.IsAsciiLetterOrDigit(character) || character is '.' or '_' or '-');
}