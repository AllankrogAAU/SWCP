using api.Models;
using api.Services;

namespace api.Endpoints
{
    public static class AnalysisEndpoints
    {
        public static RouteGroupBuilder MapAnalysisEndpoints(this IEndpointRouteBuilder app)
        {
            var group = app.MapGroup("api/cloud-llm/analysis").WithTags("Cloud LLM Analysis");

            group.MapPost("/", Submit)
                .WithSummary("THIS IS JUST A TEMPLATE ENDPOINT FOR DEMONSTRATION. DO NOT USE THIS IS PROD")
                .WithDescription("Submits C code (and optional compiler/lint/runtime logs) for LLM-based analysis. The request is published to NATS JetStream and processed asynchronously by the cloud LLM worker.")
                .Produces<AnalysisSubmitResponse>(StatusCodes.Status202Accepted);

            group.MapGet("/{jobId:guid}", GetStatus)
                .WithName("GetCloudLlmAnalysisStatus")
                .WithSummary("THIS IS JUST A TEMPLATE ENDPOINT FOR DEMONSTRATION. DO NOT USE THIS IS PROD")
                .WithDescription("Polls for the result of a previously submitted analysis job.")
                .Produces<AnalysisStatusResponse>();

            return group;
        }

        private static async Task<IResult> Submit(
            AnalysisSubmitRequest request,
            IAnalysisRequestPublisher publisher,
            CancellationToken cancellationToken)
        {
            return await SubmitAnalysisAsync(
                request.Category,
                request.CCode,
                request.Logs,
                publisher,
                cancellationToken);
        }

        internal static async Task<IResult> SubmitAnalysisAsync(
            ErrorCategory category,
            string cCode,
            string logs,
            IAnalysisRequestPublisher publisher,
            CancellationToken cancellationToken)
        {
            var jobId = Guid.NewGuid();
            await publisher.PublishAnalysisRequestAsync(new AnalysisRequestMessage
            {
                JobId = jobId,
                Category = category.ToString(),
                CCode = cCode,
                Logs = logs
            }, cancellationToken);

            return Results.AcceptedAtRoute(
                "GetCloudLlmAnalysisStatus",
                new { jobId },
                new AnalysisSubmitResponse { JobId = jobId });
        }

        private static IResult GetStatus(Guid jobId, AnalysisResultStore resultStore)
        {
            if (!resultStore.TryGetResult(jobId, out var result) || result is null)
            {
                return Results.Ok(new AnalysisStatusResponse
                {
                    JobId = jobId,
                    Status = AnalysisJobStatus.Pending
                });
            }

            return Results.Ok(new AnalysisStatusResponse
            {
                JobId = jobId,
                Status = result.Success ? AnalysisJobStatus.Completed : AnalysisJobStatus.Failed,
                Response = result.Response,
                Error = result.Error
            });
        }
    }
}
