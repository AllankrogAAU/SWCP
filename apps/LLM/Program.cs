using LLM.Services;
using LLM.Endpoints;
using Scalar.AspNetCore;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddSingleton<PromptBuilder>();

builder.Services.AddOpenApi();

builder.Services.AddHttpClient<LlmClient>(client =>
{
    string llmUrl = builder.Configuration["LLM_SERVER_URL"] ?? "http://llm-server:8080";
    client.BaseAddress = new Uri(llmUrl);
    client.Timeout = TimeSpan.FromMinutes(5);
});

var app = builder.Build();

app.MapOpenApi();

// Enable Scalar UI (Accessible at http://localhost:5000/scalar/v1)
app.MapScalarApiReference();

app.MapLlmEndpoints();
app.Run();
