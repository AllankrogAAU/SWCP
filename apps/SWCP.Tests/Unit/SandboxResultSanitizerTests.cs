using System.Text.Json;
using coreApi.Services;
using SWCP.Contracts;

namespace SWCP.Tests.Unit;

public sealed class SandboxResultSanitizerTests
{
    [Fact]
    public void SanitizeRemovesNullCharactersFromEveryTextField()
    {
        var result = new SandboxResult(
            Guid.NewGuid(),
            new CompilationResult(true, "compile\0out", "compile\0err", 0, 1),
            [new StaticAnalysisWarning(1, 1, "warn\0ing", "message\0text")],
            [new SandboxTestResult(true, "test\0out", "test\0err", 0, 1, 2)],
            "class\0ification",
            "lint\0output");

        var sanitized = SandboxResultSanitizer.Sanitize(result);
        var json = JsonSerializer.Serialize(sanitized);

        Assert.Equal("compileout", sanitized.Compilation.Stdout);
        Assert.Equal("compileerr", sanitized.Compilation.Stderr);
        Assert.Equal("warning", sanitized.StaticAnalysis[0].Severity);
        Assert.Equal("messagetext", sanitized.StaticAnalysis[0].Message);
        Assert.Equal("testout", sanitized.TestResults[0].Stdout);
        Assert.Equal("testerr", sanitized.TestResults[0].Stderr);
        Assert.Equal("classification", sanitized.ErrorClassification);
        Assert.Equal("lintoutput", sanitized.LintOutput);
        Assert.DoesNotContain("\\u0000", json, StringComparison.OrdinalIgnoreCase);
    }
}
