using api.Endpoints;
using api.Services;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.IdentityModel.Tokens;
using NATS.Client.Core;
using NATS.Client.Serializers.Json;
using NATS.Client.JetStream;
using Scalar.AspNetCore;
using System.Text;
using System.Text.Json.Serialization;

var builder = WebApplication.CreateBuilder(args);

// Add services to the container.

builder.Services.ConfigureHttpJsonOptions(options =>
    options.SerializerOptions.Converters.Add(new JsonStringEnumConverter()));
builder.Services.AddOpenApi();
builder.Services.AddHealthChecks();

var jwtSection = builder.Configuration.GetSection("Jwt");
var jwtKey = jwtSection["Key"] ?? throw new InvalidOperationException("Jwt:Key is not configured.");

builder.Services
    .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(options =>
    {
        options.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = jwtSection["Issuer"],
            ValidateAudience = true,
            ValidAudience = jwtSection["Audience"],
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(jwtKey)),
            ValidateLifetime = true,
            ClockSkew = TimeSpan.FromMinutes(1)
        };
    });

builder.Services.AddAuthorization(options =>
{
    options.AddPolicy("TeacherOnly", policy => policy.RequireRole("Teacher"));
});

builder.Services.AddSingleton<IJwtTokenService, JwtTokenService>();
builder.Services.AddSingleton<PromptBuilder>();
builder.Services.AddHttpClient<LlmClient>(client =>
{
    var llmUrl = builder.Configuration["LLM_SERVER_URL"] ?? "http://localhost:8080";
    client.BaseAddress = new Uri(llmUrl);
    client.Timeout = TimeSpan.FromMinutes(5);
});
builder.Services.AddSingleton(_ => new NatsConnection(new NatsOpts
{
    Url = builder.Configuration["Nats:Url"] ?? "nats://localhost:4222",
    RetryOnInitialConnect = true,
    SerializerRegistry = NatsJsonSerializerRegistry.Default
}));
builder.Services.AddSingleton<INatsJSContext>(services =>
    new NatsJSContext(services.GetRequiredService<NatsConnection>()));
builder.Services.AddSingleton<IAnalysisRequestPublisher, NatsAnalysisRequestPublisher>();
builder.Services.AddSingleton<AnalysisResultStore>();
builder.Services.AddSingleton<AssignmentStore>();
builder.Services.AddHostedService<NatsResultConsumerService>();

var app = builder.Build();

// Configure the HTTP request pipeline.
if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
    app.MapScalarApiReference(options =>
    {
        options
            .WithTitle("API Gateway")
            .WithTheme(ScalarTheme.DeepSpace)
            .WithDefaultHttpClient(ScalarTarget.CSharp, ScalarClient.HttpClient);
    });
    app.MapGet("/", () => Results.Redirect("/scalar/v1"));
}

app.UseHttpsRedirection();

app.UseAuthentication();
app.UseAuthorization();

app.MapHealthChecks("/health");
app.MapAuthEndpoints();
app.MapAnalysisEndpoints();
app.MapAgentEndpoints();
app.MapAssignmentEndpoints();
app.MapLlmEndpoints();

app.Run();
