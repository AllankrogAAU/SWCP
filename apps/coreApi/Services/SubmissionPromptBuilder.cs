using System.Text.Json;
using coreApi.Models;
using SWCP.Contracts;

namespace coreApi.Services;

public sealed record SubmissionPrompt(
    string SystemPrompt,
    string UserPrompt,
    string EvaluationMode,
    bool SandboxFailed,
    LlmInferenceParameters Parameters);

public static class SubmissionPromptBuilder
{
    private const string BaseSystemPrompt =
        "You are an expert programming tutor. Give accurate, clear, actionable feedback. " +
        "The assignment description defines the requirements. Teacher focus is supplemental emphasis; " +
        "it does not replace the assignment requirements or the rules for the requested action.";

    private const string TeacherFocusHeading = "Additional teacher focus for this assignment:";

    private const string HintInstructions =
        "The student requested a hint. Explain the most useful next step without giving away a complete solution. " +
        "Do not judge whether the assignment is solved. End with exactly this metadata line: " +
        "SWCP_TASK_SOLVED: not_evaluated";

    private const string FailedSubmitInstructions =
        "The sandbox reported a compilation or runtime failure. Focus on explaining the failure and how the student " +
        "can debug it educationally. Do not judge whether the assignment is solved. End with exactly this metadata " +
        "line: SWCP_TASK_SOLVED: not_evaluated";

    private const string SuccessfulSubmitInstructions =
        "Review the code for significant non-crashing quality issues first. If significant issues exist, explain them " +
        "and do not judge task completion. If no significant issues exist, compare the program with the assignment " +
        "description and decide whether it is correctly solved. Do not give away the solution. End with exactly one metadata line: " +
        "SWCP_TASK_SOLVED: true or SWCP_TASK_SOLVED: false. Do not include that line in the user-facing explanation.";

    private static readonly LlmInferenceParameters HintParameters = new(0.2, 2048, 1.0);
    private static readonly LlmInferenceParameters FailedSubmitParameters = new(0.2, 2048, 1.0);
    private static readonly LlmInferenceParameters SuccessfulSubmitParameters = new(0.2, 2048, 1.0);


    public static SubmissionPrompt Build(Assignment assignment, Submission submission, SandboxResult sandboxResult)
    {
        if (submission.Action is not ("hint" or "submit"))
        {
            throw new ArgumentException("Only Hint and Submit actions create LLM prompts.", nameof(submission));
        }

        var sandboxFailed = !sandboxResult.Compilation.Success || sandboxResult.TestResults.Any(test => !test.Passed);
        var evaluationMode = submission.Action;
        var flowInstructions = evaluationMode == "hint"
            ? HintInstructions
            : sandboxFailed
                ? FailedSubmitInstructions
                : SuccessfulSubmitInstructions;
        var parameters = evaluationMode == "hint"
            ? HintParameters
            : sandboxFailed
                ? FailedSubmitParameters
                : SuccessfulSubmitParameters;

        var systemPromptSections = new List<string> { BaseSystemPrompt };
        if (!string.IsNullOrWhiteSpace(assignment.TaskFocus))
        {
            systemPromptSections.Add(
            $"{TeacherFocusHeading}\n{assignment.TaskFocus.Trim()}");
        }
        systemPromptSections.Add(flowInstructions);

        var systemPrompt = string.Join("\n\n", systemPromptSections);
        var userPrompt =
            $"Assignment:\n{assignment.Description}\n\nC source:\n```c\n{submission.SourceCode}\n```\n\nSandbox output:\n{JsonSerializer.Serialize(sandboxResult)}";

        return new SubmissionPrompt(systemPrompt, userPrompt, evaluationMode, sandboxFailed, parameters);
    }
}