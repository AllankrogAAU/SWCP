from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from apps.benchmark.runner import LoadRunner, _sandbox_stage, _select_burst_samples
from benchmarks.config import BenchmarkConfig


class _TerminalSocket:
    async def __aenter__(self) -> _TerminalSocket:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def __aiter__(self):
        return self._messages()

    async def _messages(self):
        await asyncio.sleep(1.7)
        yield json.dumps({"eventType": "COMPLETED", "stage": "COMPLETED"})

    async def receive(self, *_: object, **__: object) -> None:
        await asyncio.Future()


class RunnerTests(unittest.TestCase):
    def test_burst_selection_caps_to_distinct_statuses(self) -> None:
        samples = [
            SimpleNamespace(status="WA"),
            SimpleNamespace(status="WA"),
            SimpleNamespace(status="CE"),
        ]

        self.assertEqual([samples[0], samples[2]], _select_burst_samples(samples, 4))

    def test_burst_selection_is_empty_when_disabled(self) -> None:
        sample = SimpleNamespace(status="WA")

        self.assertEqual([], _select_burst_samples([sample], 0))

    def test_sandbox_stage_classifies_each_validation_stage(self) -> None:
        self.assertEqual(
            "CE",
            _sandbox_stage({
                "compilation": {"success": False},
                "errorClassification": "compile_error",
            }),
        )
        self.assertEqual(
            "COMPILE_TIMEOUT",
            _sandbox_stage({
                "compilation": {"success": False},
                "errorClassification": "compile_timeout",
            }),
        )
        for classification, expected in (
            ("success", "AC"),
            ("runtime_error", "RE"),
            ("wrong_answer", "WA"),
            ("run_timeout", "TLE"),
            ("memory_limit_exceeded", "MLE"),
        ):
            output = {
                "compilation": {"success": True},
                "errorClassification": classification,
                "testResults": [{"passed": True}],
            }
            self.assertEqual(expected, _sandbox_stage(output))

    def test_completion_refreshes_response_after_websocket_wins_polling_race(self) -> None:
        asyncio.run(self._assert_completion_refreshes_response())

    def test_validation_action_is_sent_to_the_api(self) -> None:
        asyncio.run(self._assert_validation_action_is_sent())

    async def _assert_validation_action_is_sent(self) -> None:
        payloads: list[dict[str, object]] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            return httpx.Response(202, json={"submissionId": "submission-id"})

        runner = LoadRunner(
            BenchmarkConfig(target_url="http://example.test", results_dir=Path(".")),
            tokens=["test-token"],
            assignment_id="assignment-id",
            run_id="test-run",
        )
        transport = httpx.MockTransport(handle_request)
        async with httpx.AsyncClient(
            base_url="http://example.test",
            transport=transport,
        ) as client:
            with patch.object(
                runner,
                "_wait_for_completion",
                new=AsyncMock(return_value=(
                    "COMPLETED", "polling", 0, None, None, "",
                    {"status": "COMPLETED", "sandboxOutput": {}}, None,
                )),
            ):
                result = await runner.execute_single_submission(
                    "validation",
                    client,
                    source_code="int main(void) { return 0; }",
                    assignment_id="assignment-id",
                    action="validate",
                )

        self.assertEqual("COMPLETED", result.final_status)
        self.assertEqual("validate", payloads[0]["action"])

    async def _assert_completion_refreshes_response(self) -> None:
        responses = [
            {"status": "LLM_PROCESSING", "llmFeedback": None},
            {"status": "COMPLETED", "llmFeedback": "final feedback"},
        ]
        request_count = 0
        authorization_headers: list[str | None] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            authorization_headers.append(request.headers.get("Authorization"))
            body = responses[min(request_count, len(responses) - 1)]
            request_count += 1
            return httpx.Response(200, json=body)

        runner = LoadRunner(
            BenchmarkConfig(target_url="http://example.test", results_dir=Path(".")),
            tokens=["test-token"],
            assignment_id="assignment-id",
            run_id="test-run",
        )
        transport = httpx.MockTransport(handle_request)
        async with httpx.AsyncClient(
            base_url="http://example.test",
            transport=transport,
        ) as client:
            with patch(
                "benchmarks.runner.websockets.connect",
                return_value=_TerminalSocket(),
            ):
                result = await runner._wait_for_completion(
                    "submission-id",
                    "ws://example.test/ws/submissions/submission-id",
                    "test-token",
                    client,
                    0.0,
                    timeout_s=10.0,
                )

        self.assertEqual(2, request_count)
        self.assertEqual("final feedback", result[6]["llmFeedback"])
        self.assertEqual(["Bearer test-token", "Bearer test-token"], authorization_headers)


if __name__ == "__main__":
    unittest.main()
