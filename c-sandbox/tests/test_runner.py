import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))  # so `import runner` works
from runner import run_binary


FIXTURES = Path(__file__).parent / "fixtures"

def test_simple_example():
    result = run_binary(str(FIXTURES / "test"))
    assert result["exit_code"] == 0
    assert result["stdout"] == "a: 2\tb: 3 - lagt sammen =5"

def test_run_on_non_elf_file():
    result = run_binary(str(FIXTURES / "source_code" / "testprogram.c"))
    assert result["exit_code"] is None
    assert result["stdout"] == ""
    assert result["stderr"] == "Input is not a valid ELF executable."    