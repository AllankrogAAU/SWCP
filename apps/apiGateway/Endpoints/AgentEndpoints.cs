using api.Models;
using api.Services;

namespace api.Endpoints;

public static class AgentEndpoints
{
    public static RouteGroupBuilder MapAgentEndpoints(this IEndpointRouteBuilder app)
    {
        var group = app.MapGroup("api/cloud-llm").WithTags("Cloud LLM").RequireAuthorization();

        group.MapPost("/{category}/analyze", Submit)
            .WithName("SubmitCloudLlmAnalysis")
            .WithSummary("Submits code analysis to the cloud LLM worker.")
            .WithDescription("Returns a job ID; poll the analysis status route to retrieve the result.")
            .Produces<AnalysisSubmitResponse>(StatusCodes.Status202Accepted);

        return group;
    }

    private static Task<IResult> Submit(
        ErrorCategory category,
        AgentAnalyzeRequest request,
        IAnalysisRequestPublisher publisher,
        CancellationToken cancellationToken) =>
        AnalysisEndpoints.SubmitAnalysisAsync(
            category,
            request.CCode,
            request.Logs,
            publisher,
            cancellationToken);
}