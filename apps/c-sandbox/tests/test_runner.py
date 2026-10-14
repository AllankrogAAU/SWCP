import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from api.submissions import MAX_SOURCE_BYTES
from main import app

client = TestClient(app)

FIXTURES = Path(__file__).parent / "fixtures" / "source_code"

def evaluate_text(source: str) -> dict:
    """Helper, not a test: submits C source text via /evaluate and
    returns the parsed JSON body for a test to assert on."""
    response = client.post("/evaluate", json={"source": source, "submission_id": "test"})
    response.raise_for_status()
    return response.json()


def evaluate_source(filename: str) -> dict:
    """Helper, not a test: submits a file from fixtures/source_code/."""
    return evaluate_text((FIXTURES / filename).read_text())


# ---------------------------------------------------------------------
# Compile + run
# ---------------------------------------------------------------------

def test_evaluate_valid_source():
    result = evaluate_source("testprogram.c")
    assert result["status"] == "success"
    assert result["exit_code"] == 0

def test_evaluate_clean_program_has_no_diagnostics():
    CLEAN_PRIMES_STDOUT = (
    "Primes up to 50: 2 3 5 7 11 13 17 19 23 29 31 37 41 43 47\n"
    "Fibonacci: 0 1 1 2 3 5 8 13 21 34 55 89 144 233 377\n"
    "min=3 max=88 mean=35.00\n"
    "reversed: xobdnas\n"
    )
    
    result = evaluate_source("clean_primes.c")
    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert result["stdout"] == CLEAN_PRIMES_STDOUT
    assert result["compile_stderr"] == ""
    assert result["lint_output"] == ""

def test_evaluate_syntax_error():
    expected = """submission.c: In function 'main':
submission.c:3:5: error: expected ',' or ';' before 'return'
    3 |     return a;
      |     ^~~~~~
submission.c:2:9: warning: unused variable 'a' [-Wunused-variable]
    2 |     int a = 2
      |         ^
"""
    result = evaluate_source("broken_syntax.c")
    assert result["status"] == "compile_error"
    assert result["compile_stderr"] == expected
    assert result["lint_output"] is None  # lint is skipped when compilation fails

# ---------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------

def test_evaluate_lint_finds_leak_that_gcc_misses():
    result = evaluate_source("report_with_leak.c")
    assert result["status"] == "success"
    assert result["exit_code"] == 0
    assert "Class report" in result["stdout"]
    assert "memleak" in result["lint_output"]
    assert "memleak" not in result["compile_stderr"]
    assert "unused_counter" in result["compile_stderr"]

# ---------------------------------------------------------------------
# Runtime behavior
# ---------------------------------------------------------------------

def test_evaluate_infinite_loop():
    result = evaluate_source("infinite_loop.c")
    assert result["status"] == "run_timeout"
    assert result["exit_code"] is None
    assert result["compile_stderr"] == ""

def test_evaluate_fork_bombing():
    result = evaluate_source("fork_bombing.c")
    assert result["status"] == "run_timeout"
    assert result["exit_code"] is None
    assert result["compile_stderr"] == ""

def test_evaluate_seg_fault():
    result = evaluate_source("seg_fault.c")
    assert result["status"] == "success"  # it ran; the program itself crashed
    assert result["exit_code"] == 139     # 128 + SIGSEGV
    assert "nullPointer" in result["lint_output"]

# ---------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------

def test_evaluate_oversized_source():
    response = client.post("/evaluate", json={"source": "x" * (MAX_SOURCE_BYTES + 1)})
    assert response.status_code == 413

# ---------------------------------------------------------------------
# Sandbox isolation
# ---------------------------------------------------------------------

def test_evaluate_cannot_write_outside_tmp():
    result = evaluate_source("write_outside_tmp.c")
    assert result["status"] == "success"
    assert result["stdout"] == "fopen_result=failed\n"

def test_evaluate_can_write_to_tmp():
    result = evaluate_source("write_tmp_src.c")
    assert result["status"] == "success"
    assert result["stdout"] == "fopen_result=success\n"


def test_evaluate_network_is_blocked():
    result = evaluate_source("network_probe.c")
    assert result["status"] == "success"
    assert "connect_rc=-1" in result["stdout"]