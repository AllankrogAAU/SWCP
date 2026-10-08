using api.Endpoints;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.IdentityModel.Tokens;
using NATS.Client.Core;
using NATS.Client.Serializers.Json;
using System.Security.Cryptography;
using System.Text.Json.Serialization;

var builder = WebApplication.CreateBuilder(args);
builder.Logging.AddJsonConsole(options => options.TimestampFormat = "yyyy-MM-ddTHH:mm:ss.fffZ");
var publicKeyPath = builder.Configuration["Jwt:PublicKeyPath"]
    ?? Path.GetFullPath(Path.Combine(builder.Environment.ContentRootPath, "..", "coreApi", ".keys", "jwt-public.pem"));
using var publicRsa = RSA.Create();
publicRsa.ImportFromPem(await File.ReadAllTextAsync(publicKeyPath));
var publicKeyId = Convert.ToHexString(SHA256.HashData(publicRsa.ExportSubjectPublicKeyInfo())).ToLowerInvariant();
var publicSigningKey = new RsaSecurityKey(publicRsa.ExportParameters(false)) { KeyId = publicKeyId };

// Add services to the container.

builder.Services.ConfigureHttpJsonOptions(options =>
    options.SerializerOptions.Converters.Add(new JsonStringEnumConverter()));

builder.Services
    .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(options =>
    {
        options.Events = new JwtBearerEvents
        {
            OnMessageReceived = context =>
            {
                if (context.Request.Path.StartsWithSegments("/ws"))
                {
                    var bearerProtocol = context.Request.Headers.SecWebSocketProtocol
                        .ToString()
                        .Split(',', StringSplitOptions.TrimEntries)
                        .FirstOrDefault(protocol => protocol.StartsWith("bearer.", StringComparison.Ordinal));
                    if (bearerProtocol is not null)
                    {
                        context.Token = bearerProtocol["bearer.".Length..];
                    }
                }

                return Task.CompletedTask;
            }
        };
        options.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = builder.Configuration["Jwt:Issuer"] ?? "SWCP.CoreApi",
            ValidateAudience = true,
            ValidAudience = builder.Configuration["Jwt:Audience"] ?? "SWCP.Client",
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = publicSigningKey,
            ValidAlgorithms = [SecurityAlgorithms.RsaSha256],
            ValidateLifetime = true,
            ClockSkew = TimeSpan.FromMinutes(1)
        };
    });

builder.Services.AddAuthorization(options =>
{
    options.AddPolicy("TeacherOnly", policy => policy.RequireRole("Teacher"));
});

builder.Services.AddSingleton(_ => new NatsConnection(new NatsOpts
{
    Url = builder.Configuration["Nats:Url"] ?? "nats://localhost:4222",
    RetryOnInitialConnect = true,
    SerializerRegistry = NatsJsonSerializerRegistry.Default
}));
builder.Services.AddHttpClient("CoreApi", client =>
{
    client.BaseAddress = new Uri(builder.Configuration["CoreApi:Url"] ?? "http://localhost:8081");
    client.Timeout = TimeSpan.FromSeconds(10);
});
builder.Services.AddReverseProxy().LoadFromConfig(builder.Configuration.GetSection("ReverseProxy"));
builder.Services.AddCors(options => options.AddDefaultPolicy(policy =>
    policy.WithOrigins(builder.Configuration.GetSection("Cors:AllowedOrigins").Get<string[]>()
            ?? ["http://localhost:5173", "http://localhost:5174"])
        .AllowAnyHeader()
        .AllowAnyMethod()
        .AllowCredentials()));

var app = builder.Build();

// Configure the HTTP request pipeline.
if (app.Environment.IsDevelopment())
{
    app.MapGet("/", () => Results.Redirect("/scalar/v1"));
}

app.UseHttpsRedirection();
app.UseCors();
app.UseWebSockets();

app.UseAuthentication();
app.UseAuthorization();

app.MapSubmissionNotificationWebSocket();
app.MapReverseProxy();

app.Run();
