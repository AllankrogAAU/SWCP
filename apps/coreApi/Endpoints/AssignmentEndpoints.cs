using System.Text.Json;
using coreApi.Data;
using coreApi.Models;
using Microsoft.EntityFrameworkCore;

namespace coreApi.Endpoints;

public static class AssignmentEndpoints
{
    public static RouteGroupBuilder MapAssignmentEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/assignments").WithTags("Assignments").RequireAuthorization();
        group.MapGet("/", List);
        group.MapGet("/{id:guid}", Get);
        group.MapPost("/", Create).RequireAuthorization("TeacherOnly");
        return group;
    }

    private static async Task<IResult> List(CoreDbContext database, CancellationToken cancellationToken)
    {
        var assignments = await database.Assignments
            .OrderBy(assignment => assignment.Title)
            .Select(assignment => new AssignmentListResponse(assignment.Id, assignment.Title, assignment.Description))
            .ToListAsync(cancellationToken);
        return Results.Ok(assignments);
    }

    private static async Task<IResult> Get(Guid id, CoreDbContext database, CancellationToken cancellationToken)
    {
        var assignment = await database.Assignments.SingleOrDefaultAsync(item => item.Id == id, cancellationToken);
        return assignment is null ? Results.NotFound() : Results.Ok(ToResponse(assignment));
    }

    private static async Task<IResult> Create(
        CreateAssignmentRequest request,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(request.Title) || string.IsNullOrWhiteSpace(request.Description))
        {
            return Results.BadRequest(new { error = "Title and description are required." });
        }

        var testCases = request.TestCases.ValueKind == JsonValueKind.Undefined
            ? "[]"
            : request.TestCases.GetRawText();
        var assignment = new Assignment
        {
            Id = Guid.NewGuid(),
            Title = request.Title.Trim(),
            Description = request.Description,
            TaskFocus = request.TaskFocus,
            TestCasesJson = testCases,
            CreatedAtUtc = DateTimeOffset.UtcNow
        };
        database.Assignments.Add(assignment);
        await database.SaveChangesAsync(cancellationToken);
        return Results.Created($"/api/assignments/{assignment.Id}", ToResponse(assignment));
    }

    private static AssignmentResponse ToResponse(Assignment assignment) => new(
        assignment.Id,
        assignment.Title,
        assignment.Description,
        assignment.TaskFocus,
        JsonDocument.Parse(assignment.TestCasesJson).RootElement.Clone(),
        assignment.CreatedAtUtc);
}