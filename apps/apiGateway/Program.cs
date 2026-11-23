using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.IdentityModel.Tokens;
using System.Security.Cryptography;
using System.Text.Json.Serialization;

var builder = WebApplication.CreateBuilder(args);
builder.Logging.AddJsonConsole(options => options.TimestampFormat = "yyyy-MM-ddTHH:mm:ss.fffZ");
var publicKeyPath = builder.Configuration["Jwt:PublicKeyPath"]
    ?? Path.GetFullPath(Path.Combine(builder.Environment.ContentRootPath, "..", "coreApi", ".keys", "jwt-public.pem"));
using var publicRsa = RSA.Create();
if (File.Exists(publicKeyPath))
{
    publicRsa.ImportFromPem(await File.ReadAllTextAsync(publicKeyPath));
}
else
{
    publicRsa.KeySize = 2048;
}
var publicKeyId = Convert.ToHexString(SHA256.HashData(publicRsa.ExportSubjectPublicKeyInfo())).ToLowerInvariant();
var publicSigningKey = new RsaSecurityKey(publicRsa.ExportParameters(false)) { KeyId = publicKeyId };

// Add services to the container.

builder.Services.ConfigureHttpJsonOptions(options =>
    options.SerializerOptions.Converters.Add(new JsonStringEnumConverter()));

builder.Services
    .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(options =>
    {
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

app.MapGet("/health", () => Results.Ok());

app.UseCors();

app.UseAuthentication();
app.UseAuthorization();

app.MapReverseProxy();

app.Run();

namespace api
{
    public partial class Program;
}
