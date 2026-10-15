import os
from pathlib import Path

import pytest

from evaluation.evaluator import evaluate
from evaluation.result import EvaluationResult, Status
from sandbox.k8s_pod import KubeSandboxProvider, PodConfig

pytestmark = pytest.mark.skipif(
    os.getenv("SANDBOX_INTEGRATION") != "1", reason="set SANDBOX_INTEGRATION=1 to run"
)

FIXTURES = Path(__file__).parent / "fixtures" / "source_code"


@pytest.fixture(scope="module")
def provider():
    p = KubeSandboxProvider(PodConfig.from_env(), pool_size=0)
    yield p
    p.close()


def run(provider, name: str) -> EvaluationResult:
    return evaluate((FIXTURES / name).read_text(), provider)


def test_valid_source(provider) -> None:
    result = run(provider, "testprogram.c")
    assert result.status is Status.SUCCESS and result.exit_code == 0


def test_clean_program_has_no_diagnostics(provider) -> None:
    result = run(provider, "clean_primes.c")
    assert result.status is Status.SUCCESS
    assert result.stdout is not None and result.stdout.startswith("Primes up to 50:")
    assert result.compile_stderr == "" and result.lint_output == ""


def test_syntax_error_skips_lint(provider) -> None:
    result = run(provider, "broken_syntax.c")
    assert result.status is Status.COMPILE_ERROR
    assert "expected ',' or ';' before 'return'" in (result.compile_stderr or "")
    assert result.lint_output is None


def test_lint_finds_leak_gcc_misses(provider) -> None:
    result = run(provider, "report_with_leak.c")
    assert result.status is Status.SUCCESS
    assert "memleak" in (result.lint_output or "")
    assert "memleak" not in (result.compile_stderr or "")
    assert "unused_counter" in (result.compile_stderr or "")


@pytest.mark.parametrize("name", ["infinite_loop.c", "fork_bombing.c"])
def test_run_timeout(provider, name: str) -> None:
    result = run(provider, name)
    assert result.status is Status.RUN_TIMEOUT and result.exit_code is None


def test_seg_fault(provider) -> None:
    result = run(provider, "seg_fault.c")
    assert result.status is Status.SUCCESS and result.exit_code == 139
    assert "nullPointer" in (result.lint_output or "")


def test_cannot_write_outside_tmp(provider) -> None:
    assert run(provider, "write_outside_tmp.c").stdout == "fopen_result=failed\n"


def test_can_write_to_tmp(provider) -> None:
    assert run(provider, "write_tmp_src.c").stdout == "fopen_result=success\n"


def test_network_is_blocked(provider) -> None:
    assert "connect_rc=-1" in (run(provider, "network_probe.c").stdout or "")
