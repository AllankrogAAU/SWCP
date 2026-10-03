using api.Models;
using api.Services;

namespace api.Endpoints
{
    public static class AssignmentEndpoints
    {
        public static RouteGroupBuilder MapAssignmentEndpoints(this IEndpointRouteBuilder app)
        {
            var group = app.MapGroup("api/assignments").WithTags("Assignments").RequireAuthorization();

            group.MapPost("/", Create)
                .WithName("CreateAssignment")
                .WithSummary("Creates a new assignment. Teacher only.")
                .RequireAuthorization("TeacherOnly")
                .Produces<AssignmentResponse>(StatusCodes.Status201Created);

            group.MapPut("/{id:guid}", Update)
                .WithName("UpdateAssignment")
                .WithSummary("Edits an existing assignment. Teacher only.")
                .RequireAuthorization("TeacherOnly")
                .Produces<AssignmentResponse>()
                .Produces(StatusCodes.Status404NotFound);

            group.MapDelete("/{id:guid}", Delete)
                .WithName("DeleteAssignment")
                .WithSummary("Deletes an assignment. Teacher only.")
                .RequireAuthorization("TeacherOnly")
                .Produces(StatusCodes.Status204NoContent)
                .Produces(StatusCodes.Status404NotFound);

            group.MapGet("/", GetAll)
                .WithName("GetAllAssignments")
                .WithSummary("Gets all assignments. Students do not see the Focus field.")
                .Produces<IEnumerable<AssignmentResponse>>();

            group.MapGet("/{id:guid}", GetById)
                .WithName("GetAssignmentById")
                .WithSummary("Gets a single assignment. Students do not see the Focus field.")
                .Produces<AssignmentResponse>()
                .Produces(StatusCodes.Status404NotFound);

            group.MapGet("/titles", GetAllTitles)
                .WithName("GetAllAssignmentTitles")
                .WithSummary("Gets the id/title of every assignment.")
                .Produces<IEnumerable<AssignmentTitleResponse>>();

            group.MapGet("/{id:guid}/title", GetTitleById)
                .WithName("GetAssignmentTitleById")
                .WithSummary("Gets the id/title of a single assignment.")
                .Produces<AssignmentTitleResponse>()
                .Produces(StatusCodes.Status404NotFound);

            return group;
        }

        private static bool IsTeacher(HttpContext context) => context.User.IsInRole("Teacher");

        private static AssignmentResponse ToResponse(Assignment assignment, bool includeFocus) => new()
        {
            Id = assignment.Id,
            Title = assignment.Title,
            AssignmentText = assignment.AssignmentText,
            Focus = includeFocus ? assignment.Focus : null,
            CreatedAtUtc = assignment.CreatedAtUtc,
            UpdatedAtUtc = assignment.UpdatedAtUtc
        };

        private static IResult Create(CreateAssignmentRequest request, AssignmentStore store)
        {
            var assignment = store.Create(request);
            return Results.CreatedAtRoute("GetAssignmentById", new { id = assignment.Id }, ToResponse(assignment, includeFocus: true));
        }

        private static IResult Update(Guid id, UpdateAssignmentRequest request, AssignmentStore store)
        {
            if (!store.TryUpdate(id, request, out var assignment) || assignment is null)
            {
                return Results.NotFound();
            }

            return Results.Ok(ToResponse(assignment, includeFocus: true));
        }

        private static IResult Delete(Guid id, AssignmentStore store)
        {
            return store.TryDelete(id) ? Results.NoContent() : Results.NotFound();
        }

        private static IResult GetAll(HttpContext context, AssignmentStore store)
        {
            var includeFocus = IsTeacher(context);
            var assignments = store.GetAll().Select(a => ToResponse(a, includeFocus));
            return Results.Ok(assignments);
        }

        private static IResult GetById(Guid id, HttpContext context, AssignmentStore store)
        {
            if (!store.TryGet(id, out var assignment) || assignment is null)
            {
                return Results.NotFound();
            }

            return Results.Ok(ToResponse(assignment, includeFocus: IsTeacher(context)));
        }

        private static IResult GetAllTitles(AssignmentStore store)
        {
            var titles = store.GetAll().Select(a => new AssignmentTitleResponse { Id = a.Id, Title = a.Title });
            return Results.Ok(titles);
        }

        private static IResult GetTitleById(Guid id, AssignmentStore store)
        {
            if (!store.TryGet(id, out var assignment) || assignment is null)
            {
                return Results.NotFound();
            }

            return Results.Ok(new AssignmentTitleResponse { Id = assignment.Id, Title = assignment.Title });
        }
    }
}
