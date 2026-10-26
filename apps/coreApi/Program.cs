using System.Security.Cryptography;
using System.Text.Json.Serialization;
using coreApi.Data;
using coreApi.Endpoints;
using coreApi.Models;
using coreApi.Services;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Identity;
using Microsoft.EntityFrameworkCore;
using Microsoft.IdentityModel.Tokens;
using NATS.Client.Core;
using NATS.Client.JetStream;
using NATS.Client.Serializers.Json;
using System.Diagnostics;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using Scalar.AspNetCore;

var builder = WebApplication.CreateBuilder(args);
builder.Logging.AddJsonConsole(options => options.TimestampFormat = "yyyy-MM-ddTHH:mm:ss.fffZ");
var otelEndpoint = builder.Configuration["OTEL_EXPORTER_OTLP_ENDPOINT"] ?? builder.Configuration["Otel:Endpoint"];
if (!string.IsNullOrWhiteSpace(otelEndpoint))
{
    builder.Services.AddOpenTelemetry()
        .ConfigureResource(resource => resource.AddService(
            serviceName: builder.Configuration["OTEL_SERVICE_NAME"] ?? "core-api"))
        .WithTracing(tracing =>
        {
            tracing
                .SetSampler(new AlwaysOnSampler())
                .AddAspNetCoreInstrumentation(options =>
                {
                    options.RecordException = true;
                    options.Filter = httpContext => !httpContext.Request.Path.StartsWithSegments("/healthz");
                })
                .AddHttpClientInstrumentation()
                .AddSource("SWCP.CoreApi")
                .AddOtlpExporter(options =>
                {
                    options.Endpoint = new Uri(otelEndpoint);
                    options.Protocol = otelEndpoint.Contains(":4318") ? OtlpExportProtocol.HttpProtobuf : OtlpExportProtocol.Grpc;
                    options.BatchExportProcessorOptions = new BatchExportProcessorOptions<Activity>
                    {
                        ScheduledDelayMilliseconds = 500,
                        MaxExportBatchSize = 32
                    };
                });
        });
}
builder.Services.ConfigureHttpJsonOptions(options =>
    options.SerializerOptions.Converters.Add(new JsonStringEnumConverter()));
var keyDirectory = builder.Configuration["Jwt:KeyDirectory"] ?? Path.Combine(builder.Environment.ContentRootPath, ".keys");
var publicKeyDirectory = builder.Configuration["Jwt:PublicKeyDirectory"] ?? Path.Combine(keyDirectory, "public");
var signingKeys = RsaSigningKeys.LoadOrCreate(keyDirectory, publicKeyDirectory);

builder.Services.AddSingleton(signingKeys);
var defaultConnection = builder.Configuration.GetConnectionString("Default");
if (!string.IsNullOrEmpty(defaultConnection))
{
    builder.Services.AddDbContext<CoreDbContext>(options =>
        options.UseNpgsql(defaultConnection));
}
builder.Services.AddSingleton<IPasswordHasher<User>, PasswordHasher<User>>();
builder.Services.AddSingleton<JwtTokenService>();
builder.Services.AddSingleton(_ => new NatsConnection(new NatsOpts
{
    Url = builder.Configuration["Nats:Url"] ?? "nats://localhost:4222",
    RetryOnInitialConnect = true,
    SerializerRegistry = NatsJsonSerializerRegistry.Default
}));
builder.Services.AddSingleton<INatsJSContext>(services =>
    new NatsJSContext(services.GetRequiredService<NatsConnection>()));
builder.Services.AddSingleton<NatsTaskPublisher>();
builder.Services.AddSingleton<SubmissionEvents>();
builder.Services.AddHostedService<SandboxResultConsumer>();
builder.Services.AddHostedService<LlmResultConsumer>();
builder.Services.AddHostedService<SubmissionProgressConsumer>();
builder.Services.AddHostedService<SubmissionReconciler>();
builder.Services.AddOpenApi();

builder.Services
    .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(options =>
    {
        options.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = builder.Configuration["Jwt:Issuer"],
            ValidateAudience = true,
            ValidAudience = builder.Configuration["Jwt:Audience"],
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = new RsaSecurityKey(signingKeys.Rsa) { KeyId = signingKeys.KeyId },
            ValidateLifetime = true,
            ClockSkew = TimeSpan.FromMinutes(1)
        };
    });
builder.Services.AddAuthorization(options =>
    options.AddPolicy("TeacherOnly", policy => policy.RequireRole(UserRole.Teacher.ToString())));

var app = builder.Build();

await using (var scope = app.Services.CreateAsyncScope())
{
    var database = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
    if (database.Database.IsRelational())
    {
        await database.Database.MigrateAsync();
    }
    else
    {
        await database.Database.EnsureCreatedAsync();
    }
    if (builder.Environment.IsDevelopment() || builder.Configuration.GetValue<bool>("SeedSampleAssignments"))
    {
        await DevUserSeeder.SeedSampleAssignmentAsync(scope.ServiceProvider);
    }
    await DevUserSeeder.SeedAsync(scope.ServiceProvider, builder.Configuration);
}

if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
    app.MapScalarApiReference();
}

app.UseAuthentication();
app.UseAuthorization();
app.MapGet("/healthz", () => Results.Ok(new { status = "healthy" })).AllowAnonymous();
app.MapAuthEndpoints();
app.MapAssignmentEndpoints();
app.MapSubmissionEndpoints();
app.MapGet("/.well-known/swcp-signing-key", (RsaSigningKeys keys) =>
    Results.Text(keys.Rsa.ExportSubjectPublicKeyInfoPem(), "application/x-pem-file")).AllowAnonymous();

await app.RunAsync();

namespace coreApi
{
    public partial class Program;
}
