from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from evaluation.evaluator import evaluate

router = APIRouter()

MAX_SOURCE_BYTES = 1_000_000


class EvaluateRequest(BaseModel):
    source: str
    submission_id: str | None = None


class EvaluateResponse(BaseModel):
    status: str
    compile_stdout: str | None = None
    compile_stderr: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None


@router.post("/evaluate", response_model=EvaluateResponse)
def evaluate_submission(req: EvaluateRequest):
    if len(req.source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise HTTPException(413, "Source exceeds maximum allowed size")

    result = evaluate(req.source)

    return EvaluateResponse(
        status=result.status.value,
        compile_stdout=result.compile_stdout,
        compile_stderr=result.compile_stderr,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )