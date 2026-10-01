using api.Models;
using api.Services;

namespace api.Endpoints
{
    public static class AuthEndpoints
    {
        public static RouteGroupBuilder MapAuthEndpoints(this IEndpointRouteBuilder app)
        {
            var group = app.MapGroup("api/auth").WithTags("Auth");

            group.MapPost("/login", Login)
                .WithName("Login")
                .WithSummary("THIS IS A DEMO/PLACEHOLDER LOGIN ENDPOINT. There is no user database yet.")
                .WithDescription("Issues a JWT for the requested role. Must be replaced with real credential validation once user storage exists.")
                .Produces<LoginResponse>()
                .AllowAnonymous();

            return group;
        }

        private static IResult Login(LoginRequest request, IJwtTokenService tokenService)
        {
            if (string.IsNullOrWhiteSpace(request.UserName))
            {
                return Results.BadRequest("UserName is required.");
            }

            var response = tokenService.GenerateToken(request.UserName, request.Role);
            return Results.Ok(response);
        }
    }
}
