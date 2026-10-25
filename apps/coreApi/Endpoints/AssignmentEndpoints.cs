using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using coreApi.Data;
using coreApi.Models;
using Microsoft.EntityFrameworkCore;
using SWCP.Contracts;

namespace coreApi.Endpoints;

public static class AssignmentEndpoints
{
    private static readonly JsonSerializerOptions TestCaseJsonOptions = new(JsonSerializerDefaults.Web)
    {
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
    };

    public static RouteGroupBuilder MapAssignmentEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/assignments").WithTags("Assignments").RequireAuthorization();
        group.MapGet("/", List);
        group.MapGet("/{id:guid}", Get);
        group.MapGet("/{id:guid}/harness", GetHarness).RequireAuthorization("TeacherOnly");
        group.MapPost("/", Create).RequireAuthorization("TeacherOnly");
        group.MapPut("/{id:guid}/test-cases", ReplaceTestCases).RequireAuthorization("TeacherOnly");
        group.MapPut("/{id:guid}/harness", ReplaceHarness).RequireAuthorization("TeacherOnly");
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

    private static async Task<IResult> GetHarness(
        Guid id,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        var assignment = await database.Assignments
            .SingleOrDefaultAsync(item => item.Id == id, cancellationToken);
        return assignment is null
            ? Results.NotFound(new { error = "Assignment not found." })
            : Results.Ok(new AssignmentHarnessResponse(assignment.HarnessSource));
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

        if (!TryParseTestCases(request.TestCases, allowMissing: true, out var testCases, out var error))
        {
            return Results.BadRequest(new { error });
        }

        if (!IsValidHarnessSource(request.HarnessSource))
        {
            return Results.BadRequest(new { error = "Harness source must be at most 64 KB UTF-8." });
        }

        var assignment = new Assignment
        {
            Id = Guid.NewGuid(),
            Title = request.Title.Trim(),
            Description = request.Description,
            TaskFocus = request.TaskFocus,
            TestCasesJson = JsonSerializer.Serialize(testCases, TestCaseJsonOptions),
            HarnessSource = NormalizeHarnessSource(request.HarnessSource),
            CreatedAtUtc = DateTimeOffset.UtcNow
        };
        database.Assignments.Add(assignment);
        await database.SaveChangesAsync(cancellationToken);
        return Results.Created($"/api/assignments/{assignment.Id}", ToResponse(assignment));
    }

    private static async Task<IResult> ReplaceTestCases(
        Guid id,
        ReplaceAssignmentTestCasesRequest request,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        if (!TryParseTestCases(request.TestCases, allowMissing: false, out var testCases, out var error))
        {
            return Results.BadRequest(new { error });
        }

        var assignment = await database.Assignments
            .SingleOrDefaultAsync(item => item.Id == id, cancellationToken);
        if (assignment is null)
        {
            return Results.NotFound(new { error = "Assignment not found." });
        }

        assignment.TestCasesJson = JsonSerializer.Serialize(testCases, TestCaseJsonOptions);
        await database.SaveChangesAsync(cancellationToken);
        return Results.Ok(ToResponse(assignment));
    }

    private static async Task<IResult> ReplaceHarness(
        Guid id,
        ReplaceAssignmentHarnessRequest request,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        if (!IsValidHarnessSource(request.HarnessSource))
        {
            return Results.BadRequest(new { error = "Harness source must be at most 64 KB UTF-8." });
        }

        var assignment = await database.Assignments
            .SingleOrDefaultAsync(item => item.Id == id, cancellationToken);
        if (assignment is null)
        {
            return Results.NotFound(new { error = "Assignment not found." });
        }

        assignment.HarnessSource = NormalizeHarnessSource(request.HarnessSource);
        await database.SaveChangesAsync(cancellationToken);
        return Results.Ok(new AssignmentHarnessResponse(assignment.HarnessSource));
    }

    private static bool IsValidHarnessSource(string? source) =>
        source is null || Encoding.UTF8.GetByteCount(source) <= 64 * 1024;

    private static string? NormalizeHarnessSource(string? source) =>
        string.IsNullOrWhiteSpace(source) ? null : source;

    private static bool TryParseTestCases(
        JsonElement value,
        bool allowMissing,
        out IReadOnlyList<SandboxTestCase> testCases,
        out string error)
    {
        testCases = [];
        error = string.Empty;
        if (value.ValueKind == JsonValueKind.Undefined && allowMissing)
        {
            return true;
        }

        if (value.ValueKind != JsonValueKind.Array)
        {
            error = "TestCases must be an array.";
            return false;
        }

        List<SandboxTestCase>? parsed;
        try
        {
            parsed = value.Deserialize<List<SandboxTestCase>>(TestCaseJsonOptions);
        }
        catch (JsonException)
        {
            error = "Each test case must contain only stdin, expectedStdout, timeoutMilliseconds, and memoryLimitMegabytes.";
            return false;
        }

        if (parsed is null)
        {
            error = "TestCases must be an array.";
            return false;
        }

        for (var index = 0; index < parsed.Count; index++)
        {
            var testCase = parsed[index];
            if (testCase is null ||
                testCase.Stdin is null ||
                testCase.ExpectedStdout is null ||
                testCase.TimeoutMilliseconds <= 0 ||
                testCase.MemoryLimitMegabytes <= 0)
            {
                error = $"Test case {index + 1} must have string stdin and expectedStdout values and positive timeoutMilliseconds and memoryLimitMegabytes values.";
                return false;
            }
        }

        testCases = parsed;
        return true;
    }

    private static AssignmentResponse ToResponse(Assignment assignment) => new(
        assignment.Id,
        assignment.Title,
        assignment.Description,
        assignment.TaskFocus,
        JsonDocument.Parse(assignment.TestCasesJson).RootElement.Clone(),
        assignment.CreatedAtUtc);
}