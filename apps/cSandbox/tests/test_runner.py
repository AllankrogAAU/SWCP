import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def evaluate_source(path: str) -> dict:
    """Helper, not a test: submits a fixture .c file via /evaluate
    and returns the parsed JSON body for a test to assert on."""
    with open(path) as f:
        source = f.read()

    response = client.post("/evaluate", json={"source": source, "submission_id": "test"})
    response.raise_for_status()
    return response.json()


def test_evaluate_valid_source():
    result = evaluate_source("fixtures/source_code/testprogram.c")
    assert result["status"] == "success"
    assert result["exit_code"] == 0


def test_evaluate_syntax_error():
    expected = """submission.c: In function 'main':
submission.c:3:5: error: expected ',' or ';' before 'return'
    3 |     return a;
      |     ^~~~~~
submission.c:2:9: warning: unused variable 'a' [-Wunused-variable]
    2 |     int a = 2
      |         ^
"""
    result = evaluate_source("fixtures/source_code/broken_syntax.c")
    assert result["status"] == "compile_error"
    assert result["compile_stderr"] == expected

def test_evaluate_infinite_loop():
    result = evaluate_source("fixtures/source_code/infinite_loop.c")
    assert result["status"] == "run_timeout"
    assert result["exit_code"] is None
    assert result["compile_stderr"] == ""

def test_evaluate_fork_bombing():
    result = evaluate_source("fixtures/source_code/fork_bombing.c")
    assert result["exit_code"]  is None
    assert result["compile_stderr"] == ""
    assert result["status"] == "run_timeout"
    assert result["stderr"] == ""

#def test_evaluate_seg_fault():

#def test_evaluate_oversized_source():

#def test_evaluate_read_fails_outside_of_dir():
