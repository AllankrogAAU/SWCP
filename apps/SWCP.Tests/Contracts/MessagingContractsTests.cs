using System.Text.Json;
using System.Text.RegularExpressions;
using SWCP.Contracts;
using Xunit;

namespace SWCP.Tests.Contracts;

public sealed partial class MessagingContractsTests
{
    [GeneratedRegex("^00-[0-9a-f]{32}-[0-9a-f]{16}-01$")]
    private static partial Regex W3CTraceParentRegex();

    [Fact]
    public void CurrentOrNewTraceParent_MatchesW3CStandardFormat()
    {
        var traceParent = Messaging.CurrentOrNewTraceParent();
        Assert.Matches(W3CTraceParentRegex(), traceParent);
    }

    [Fact]
    public void MessageIds_FormatDeterministicDeduplicationKeys()
    {
        var submissionId = Guid.Parse("11111111-2222-3333-4444-555555555555");
        Assert.Equal("11111111-2222-3333-4444-555555555555-sandbox-0", Messaging.SandboxMessageId(submissionId, 0));
        Assert.Equal("11111111-2222-3333-4444-555555555555-llm", Messaging.LlmMessageId(submissionId));
        Assert.Equal("notifications.11111111-2222-3333-4444-555555555555", Messaging.NotificationSubject(submissionId));
    }

    [Fact]
    public void SandboxTask_SerializationRoundTrip_PreservesAllFields()
    {
        var task = new SandboxTask(
            Guid.NewGuid(),
            Guid.NewGuid(),
            "int main() { return 0; }",
            ["-O2", "-Wall"],
            [new SandboxTestCase("1 2", "3", 1000, 64)],
            Messaging.CurrentOrNewTraceParent());

        var json = JsonSerializer.Serialize(task);
        var deserialized = JsonSerializer.Deserialize<SandboxTask>(json);

        Assert.NotNull(deserialized);
        Assert.Equal(task.SubmissionId, deserialized.SubmissionId);
        Assert.Equal(task.AssignmentId, deserialized.AssignmentId);
        Assert.Equal(task.Source, deserialized.Source);
        Assert.Equal(task.CompilerFlags, deserialized.CompilerFlags);
        Assert.Single(deserialized.TestCases);
        Assert.Equal("1 2", deserialized.TestCases[0].Stdin);
        Assert.Equal("3", deserialized.TestCases[0].ExpectedStdout);
    }

    [Fact]
    public void SandboxResult_SerializationRoundTrip_PreservesAllFields()
    {
        var result = new SandboxResult(
            Guid.NewGuid(),
            new CompilationResult(true, "ok", "", 0, 42),
            [new StaticAnalysisWarning(10, 5, "warning", "unused variable")],
            [new SandboxTestResult(true, "3", "", 0, 15, 4.2)],
            null);

        var json = JsonSerializer.Serialize(result);
        var deserialized = JsonSerializer.Deserialize<SandboxResult>(json);

        Assert.NotNull(deserialized);
        Assert.Equal(result.SubmissionId, deserialized.SubmissionId);
        Assert.True(deserialized.Compilation.Success);
        Assert.Single(deserialized.StaticAnalysis);
        Assert.Equal(10, deserialized.StaticAnalysis[0].Line);
        Assert.Single(deserialized.TestResults);
        Assert.True(deserialized.TestResults[0].Passed);
    }

    [Fact]
    public void Deserializing_MalformedJson_ThrowsJsonException()
    {
        var malformedJson = "{ \"submissionId\": \"not-a-guid\", \"broken\": ";
        Assert.ThrowsAny<JsonException>(() => JsonSerializer.Deserialize<SandboxTask>(malformedJson));
    }
}

