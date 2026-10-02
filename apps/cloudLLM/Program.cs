using cloudLLM;
using cloudLLM.Agents;
using cloudLLM.Endpoints;
using cloudLLM.Interfaces;
using cloudLLM.Services;
using Scalar.AspNetCore;

EnvFileLoader.Load(AppContext.BaseDirectory);

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddOpenApi();

builder.Services.AddSingleton<IChatClientProvider, ChatClientProvider>();

builder.Services.AddSingleton<ILlmAgent, CompilerAgent>();
builder.Services.AddSingleton<ILlmAgent, LintAgent>();
builder.Services.AddSingleton<ILlmAgent, RuntimeAgent>();
builder.Services.AddSingleton<AgentFactory>();

builder.Services.AddHostedService<Worker>();

var app = builder.Build();

if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
    app.MapScalarApiReference(options =>
    {
        options
            .WithTitle("Cloud LLM Worker API")
            .WithTheme(ScalarTheme.DeepSpace)
            .WithDefaultHttpClient(ScalarTarget.CSharp, ScalarClient.HttpClient);
    });
    app.MapGet("/", () => Results.Redirect("/scalar/v1"));
}

app.UseHttpsRedirection();

app.MapAgentEndpoints();

app.Run();
