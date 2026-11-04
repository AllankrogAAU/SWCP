import base64
import binascii

from evaluation.result import PhaseResult, SandboxReport

_PHASES = ("compile", "lint", "run")


def _decode(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


def parse_report(raw: str) -> SandboxReport:
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value

    parsed: dict[str, PhaseResult | None] = {}
    for phase in _PHASES:
        rc = fields.get(f"{phase}_rc")
        if rc is None:
            parsed[phase] = None
            continue
        try:
            rc_value = int(rc)
            duration = int(fields.get(f"{phase}_ms", "0"))
        except ValueError:
            parsed[phase] = None
            continue
        parsed[phase] = PhaseResult(
            rc=rc_value,
            duration_ms=duration,
            stdout=_decode(fields.get(f"{phase}_stdout", "")),
            stderr=_decode(fields.get(f"{phase}_stderr", "")),
        )
    test_runs: list[PhaseResult] = []
    index = 0
    while (rc := fields.get(f"test_{index}_rc")) is not None:
        try:
            rc_value = int(rc)
            duration = int(fields.get(f"test_{index}_ms", "0"))
        except ValueError:
            break
        test_runs.append(
            PhaseResult(
                rc=rc_value,
                duration_ms=duration,
                stdout=_decode(fields.get(f"test_{index}_stdout", "")),
                stderr=_decode(fields.get(f"test_{index}_stderr", "")),
            )
        )
        index += 1
    return SandboxReport(
        compile=parsed["compile"],
        lint=parsed["lint"],
        run=parsed["run"],
        test_runs=tuple(test_runs),
    )
