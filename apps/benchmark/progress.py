"""Live benchmark progress written to a Markdown file and the log."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import time

LOGGER = logging.getLogger("apps.benchmark.progress")


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}h {minutes:02d}m {secs:02d}s"


class ProgressTracker:
    def __init__(
        self,
        path: Path,
        total_submissions: int,
        model_keys: list[str],
        run_id: str,
        every: int = 10,
    ) -> None:
        self.path = path
        self.total = max(0, total_submissions)
        self.model_keys = model_keys
        self.run_id = run_id
        self.every = max(1, every)
        self.started = time.monotonic()
        self.phase = "starting"
        self.model_index = 0
        self.model_key: str | None = None
        self.completed = 0
        self.succeeded = 0
        self.failed_ids: list[str] = []
        self.history: list[str] = []
        self.judge_total = 0
        self.judge_done = 0

    def set_phase(self, phase: str, model_key: str | None = None, note: str | None = None) -> None:
        self.phase = phase
        if model_key is not None:
            self.model_key = model_key
            if model_key in self.model_keys:
                self.model_index = self.model_keys.index(model_key) + 1
        self._event(note or phase)
        self.write()

    def record(self, success: bool, submission_id: str) -> None:
        self.completed += 1
        if success:
            self.succeeded += 1
        else:
            self.failed_ids.append(submission_id)
        if self.completed % self.every == 0 or self.completed == self.total:
            self.write()

    def record_judgement(self) -> None:
        self.judge_done += 1
        if self.judge_done % self.every == 0 or self.judge_done == self.judge_total:
            self.write()

    def _event(self, text: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        self.history.append(f"{stamp} {text}")
        self.history = self.history[-25:]

    def snapshot(self) -> dict[str, object]:
        elapsed = time.monotonic() - self.started
        eta = (
            elapsed / self.completed * (self.total - self.completed)
            if self.completed and self.total > self.completed
            else (0.0 if self.total and self.completed >= self.total else None)
        )
        return {
            "phase": self.phase,
            "model": f"{self.model_key or '--'} ({self.model_index} of {len(self.model_keys)})",
            "submissions": f"{self.completed} / {self.total}",
            "success_rate": (
                f"{100.0 * self.succeeded / self.completed:.1f}%" if self.completed else "--"
            ),
            "elapsed": _duration(elapsed),
            "eta": _duration(eta),
        }

    def write(self) -> None:
        snap = self.snapshot()
        banner = (
            f"[{snap['phase']} | Model {snap['model']} | Submissions {snap['submissions']} | "
            f"Success {snap['success_rate']} | Elapsed {snap['elapsed']} | ETA {snap['eta']}]"
        )
        LOGGER.info("=" * 20 + " PROGRESS " + "=" * 20)
        LOGGER.info(banner)
        lines = [
            "# SWCP CodeNet benchmark - live progress",
            "",
            f"Run: `{self.run_id}`  ",
            f"Updated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "",
            "| Current phase | Model | Submissions completed | Success rate | Elapsed | Estimated remaining |",
            "|---|---|---|---|---|---|",
            f"| {snap['phase']} | {snap['model']} | {snap['submissions']} | {snap['success_rate']} "
            f"| {snap['elapsed']} | {snap['eta']} |",
            "",
        ]
        if self.judge_total:
            lines += [f"Judge progress: {self.judge_done} / {self.judge_total}", ""]
        if self.failed_ids:
            lines += [
                f"Failed after retries ({len(self.failed_ids)}): "
                + ", ".join(f"`{item}`" for item in self.failed_ids[-20:]),
                "",
            ]
        lines += ["## Recent events", ""] + [f"- {item}" for item in reversed(self.history)] + [""]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8")
        temporary.replace(self.path)
