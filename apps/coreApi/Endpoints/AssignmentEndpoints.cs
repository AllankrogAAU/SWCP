using System.Text.Json;
using coreApi.Data;
using coreApi.Models;
using Microsoft.EntityFrameworkCore;
using System.Security.Claims;

namespace coreApi.Endpoints;

public static class AssignmentEndpoints
{
    private sealed record SaveAssignmentStateRequest(
        string CurrentCode);

    public static RouteGroupBuilder MapAssignmentEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/assignments").WithTags("Assignments").RequireAuthorization();
        group.MapGet("/", List);
        group.MapGet("/{id:guid}", Get);
        group.MapGet("/{id:guid}/state", GetState);
        group.MapPut("/{id:guid}/state", SaveState);
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

    private static async Task<IResult> GetState(
        Guid id,
        ClaimsPrincipal principal,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        if (!Guid.TryParse(
            principal.FindFirstValue(ClaimTypes.NameIdentifier),
            out var userId))
        {
            return Results.Unauthorized();
        }

        var state = await database.UserAssignmentStates
            .AsNoTracking()
            .SingleOrDefaultAsync(
                x => x.UserId == userId &&
                    x.AssignmentId == id,
                cancellationToken);

        if (state is null)
        {
            return Results.NotFound();
        }

        return Results.Ok(new
        {
            state.CurrentCode,
            state.UpdatedAtUtc
        });
    }

    private static async Task<IResult> SaveState(
        Guid id,
        SaveAssignmentStateRequest request,
        ClaimsPrincipal principal,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        if(!Guid.TryParse(
            principal.FindFirstValue(ClaimTypes.NameIdentifier), 
            out var userId))
        {
            return Results.Unauthorized();
        }

        var state = await database.UserAssignmentStates.SingleOrDefaultAsync(
            x => x.UserId == userId && x.AssignmentId == id, cancellationToken);

           if (state is null)
                {
                    state = new UserAssignmentState
                    {
                        UserId = userId,
                        AssignmentId = id,
                        CurrentCode = request.CurrentCode,
                        UpdatedAtUtc = DateTimeOffset.UtcNow
                    };

                    database.UserAssignmentStates.Add(state);
                }
                else
                {
                    state.CurrentCode = request.CurrentCode;
                    state.UpdatedAtUtc = DateTimeOffset.UtcNow;
                }

           await database.SaveChangesAsync(cancellationToken);

           return Results.NoContent();     
        
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