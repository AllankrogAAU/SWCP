"""Submit benchmark samples through the running SWCP browser frontend."""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Browser, Error as PlaywrightError, Page, TimeoutError as PlaywrightTimeoutError


_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Chromium's clipboard is shared by all browser contexts; serialize paste/verify across burst sessions.
_CLIPBOARD_LOCK = asyncio.Lock()


class ApiError(RuntimeError):
    """An SWCP API request failed."""


def api_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            if not body:
                return response.status, None
            try:
                return response.status, json.loads(body)
            except json.JSONDecodeError as error:
                raise ApiError(f"SWCP API returned invalid JSON for {path}.") from error
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body
        return error.code, detail
    except (urllib.error.URLError, TimeoutError) as error:
        raise ApiError(f"Could not reach SWCP API at {base_url}: {error}") from error


def ensure_student_account(base_url: str, username: str, password: str) -> None:
    status, detail = api_request(
        base_url,
        "/api/auth/register",
        method="POST",
        payload={"username": username, "password": password},
    )
    if status not in (201, 409):
        raise ApiError(f"Could not create benchmark student '{username}' ({status}): {detail}")


def ensure_assignments(
    base_url: str,
    teacher_username: str,
    teacher_password: str,
    samples: list[dict[str, Any]],
) -> dict[str, str]:
    status, login = api_request(
        base_url,
        "/api/auth/login",
        method="POST",
        payload={"username": teacher_username, "password": teacher_password},
    )
    if status != 200 or not isinstance(login, dict) or not login.get("accessToken"):
        raise ApiError(
            "Teacher sign-in failed. Configure SWCP_BENCHMARK_TEACHER_USERNAME and "
            "SWCP_BENCHMARK_TEACHER_PASSWORD, then enable development teacher seeding."
        )
    token = str(login["accessToken"])
    status, assignments = api_request(base_url, "/api/assignments/", token=token)
    if status != 200 or not isinstance(assignments, list):
        raise ApiError(f"Could not list assignments ({status}): {assignments}")
    by_title = {
        str(item["title"]): str(item["id"])
        for item in assignments
        if isinstance(item, dict) and item.get("title") and item.get("id")
    }

    result: dict[str, str] = {}
    for sample in samples:
        title = assignment_title(sample)
        assignment_id = by_title.get(title)
        if assignment_id is None:
            description = str(sample.get("problem_description") or "").strip()
            if not description:
                description = (
                    f"Review the C source fixture {sample.get('source_path', sample['id'])}. "
                    "Provide specific feedback on correctness, safety, and clarity."
                )
            payload = {
                "title": title,
                "description": description,
                "taskFocus": (
                    "Review the submitted C program against the assignment description. "
                    "Give concise, actionable feedback without providing a complete solution."
                ),
                "testCases": [],
            }
            status, created = api_request(
                base_url, "/api/assignments/", method="POST", payload=payload, token=token
            )
            if status != 201 or not isinstance(created, dict) or not created.get("id"):
                raise ApiError(f"Could not create assignment '{title}' ({status}): {created}")
            assignment_id = str(created["id"])
            by_title[title] = assignment_id
        result[str(sample["id"])] = assignment_id
    return result


def assignment_title(sample: dict[str, Any]) -> str:
    if sample.get("slice") == "repo_validation":
        name = str(sample.get("source_path") or sample["id"]).replace("\\", "/").rsplit("/", 1)[-1]
        return f"SWCP Benchmark Repo Validation: {name}"[:120]
    return f"SWCP Benchmark CodeNet: {sample['problem_id']}"[:120]


async def open_student_page(
    browser: Browser,
    base_url: str,
    username: str,
    password: str,
) -> tuple[Any, Page]:
    await asyncio.to_thread(ensure_student_account, base_url, username, password)
    context = await browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    try:
        page = await context.new_page()
    except PlaywrightError:
        await context.close()
        raise
    try:
        await page.goto(base_url, wait_until="domcontentloaded")
        await page.get_by_label("Username").fill(username)
        await page.get_by_label("Password").fill(password)
        await page.get_by_role("button", name="Sign in", exact=True).click()
        await page.locator(".backend-select").wait_for(state="visible", timeout=30_000)
    except PlaywrightTimeoutError as error:
        panel = page.locator(".auth-panel")
        detail = await panel.inner_text() if await panel.count() else "No authenticated frontend view appeared."
        await context.close()
        raise RuntimeError(f"Benchmark student sign-in failed for '{username}': {detail}") from error
    except PlaywrightError:
        await context.close()
        raise
    return context, page


async def submit_from_frontend(
    page: Page,
    sample: dict[str, Any],
    assignment_id: str,
    backend: str,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    title = assignment_title(sample)
    await page.get_by_text(title, exact=True).first.click()
    option = "Azure AI Foundry" if backend == "azure" else "Local vLLM"
    selector = page.locator(".backend-select")
    if option not in await selector.inner_text():
        await selector.locator(".v-field").click()
        await page.get_by_role("option", name=option, exact=True).click()
        await page.wait_for_function(
            "(text) => document.querySelector('.backend-select')?.innerText.includes(text)", arg=option, timeout=10_000
        )

    editor = page.locator(".monaco-editor").first
    await editor.wait_for(state="visible", timeout=30_000)
    await editor.click(position={"x": 80, "y": 50})
    source = str(sample["source"])
    async with _CLIPBOARD_LOCK:
        # Paste like a student would; typed input triggers Monaco auto-closing brackets and corrupts the source.
        await page.evaluate("(text) => navigator.clipboard.writeText(text)", source)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Control+V")
        await page.wait_for_timeout(150)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Control+C")
        await page.wait_for_timeout(50)
        editor_text = await page.evaluate("() => navigator.clipboard.readText()")
    if _normalize_source(editor_text) != _normalize_source(source):
        raise RuntimeError(
            f"Editor content does not match the sample after paste ({len(editor_text)} vs {len(source)} chars)."
        )

    events: list[dict[str, Any]] = []
    first_ws_at: float | None = None

    def on_websocket(websocket: Any) -> None:
        def on_frame(frame: Any) -> None:
            nonlocal first_ws_at
            received_at = time.perf_counter()
            payload = frame
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")
            if not isinstance(payload, str):
                return
            try:
                notification = json.loads(payload)
            except json.JSONDecodeError:
                return
            if not isinstance(notification, dict):
                return
            if first_ws_at is None:
                first_ws_at = received_at
            events.append({
                "event_type": notification.get("eventType"),
                "stage": notification.get("stage"),
                "timestamp_utc": notification.get("timestampUtc"),
                "received_at": received_at,
                "received_at_utc": datetime.now(timezone.utc).isoformat(),
            })

        websocket.on("framereceived", on_frame)

    page.once("websocket", on_websocket)
    previous_alert = "\n".join(await page.locator(".v-alert").all_inner_texts())
    previous_match = _UUID.search(previous_alert)
    previous_submission_id = previous_match.group(0) if previous_match else ""
    started_at_utc = datetime.now(timezone.utc)
    click_started = time.perf_counter()
    await page.get_by_role("button", name="Submit", exact=True).click()
    await page.wait_for_function(
        """(previousId) => [...document.querySelectorAll('.v-alert')].some(el => {
          const match = el.innerText.match(
            /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i
          );
          return match && match[0] !== previousId;
        })""",
        arg=previous_submission_id,
        timeout=30_000,
    )
    accepted_at = time.perf_counter()
    alert_text = "\n".join(await page.locator(".v-alert").all_inner_texts())
    match = _UUID.search(alert_text)
    if match is None:
        raise RuntimeError("Frontend accepted the submission but displayed no submission ID.")
    platform_submission_id = match.group(0)

    await page.wait_for_function(
        """(id) => {
          const alert = [...document.querySelectorAll('.v-alert')]
            .find(el => el.innerText.includes(id));
          const submit = [...document.querySelectorAll('button')]
            .find(el => el.innerText.trim() === 'Submit');
          return alert && /\\b(COMPLETED|FAILED)\\b/.test(alert.innerText)
            && submit && !submit.disabled;
        }""",
        arg=platform_submission_id,
        timeout=timeout_seconds * 1000,
    )
    rendered_at = time.perf_counter()
    details = await page.evaluate(
        """async (id) => {
          const token = localStorage.getItem('swcp.accessToken');
          const response = await fetch('/api/submissions/' + id, {
            headers: token ? { Authorization: 'Bearer ' + token } : {},
          });
          return response.ok ? await response.json() : null;
        }""",
        platform_submission_id,
    )
    if not isinstance(details, dict):
        details = {}
    final_alert = "\n".join(await page.locator(".v-alert").all_inner_texts())
    status_match = re.search(r"\b(COMPLETED|FAILED)\b", final_alert)
    status = status_match.group(1) if status_match else "UNKNOWN"
    feedback = ""
    feedback_element = page.locator(".feedback-text")
    if status == "COMPLETED" and await feedback_element.count():
        feedback = await feedback_element.first.inner_text()
    error_element = page.locator(".feedback-card .v-alert")
    error_message = (
        await error_element.first.inner_text() if await error_element.count() else None
    )

    final_ws_event = next(
        (
            event for event in reversed(events)
            if event.get("stage") in {"COMPLETED", "FAILED", "completed", "failed"}
        ),
        None,
    )
    phase_events: dict[str, float] = {}
    for event in events:
        stage = str(event.get("stage") or "").upper()
        if stage in {
            "SANDBOX_PROCESSING", "LLM_QUEUED", "LLM_PROCESSING", "COMPLETED", "FAILED"
        }:
            phase_events.setdefault(stage, event["received_at"])
    ws_breakdown = {
        "sandbox_queue": _elapsed_ms(accepted_at, phase_events.get("SANDBOX_PROCESSING")),
        "sandbox_execution": _elapsed_ms(
            phase_events.get("SANDBOX_PROCESSING"), phase_events.get("LLM_QUEUED")
        ),
        "llm_queue": _elapsed_ms(
            phase_events.get("LLM_QUEUED"), phase_events.get("LLM_PROCESSING")
        ),
        "llm_inference": _elapsed_ms(
            phase_events.get("LLM_PROCESSING"),
            phase_events.get("COMPLETED") or phase_events.get("FAILED"),
        ),
    }
    ws_delivery_ms = _websocket_delivery_ms(final_ws_event)
    return {
        "platform_submission_id": platform_submission_id,
        "status": status,
        "feedback": feedback,
        "sandbox_output": details.get("sandboxOutput"),
        "task_solved": details.get("taskSolved"),
        "platform_error": details.get("errorMessage"),
        "error": error_message,
        "events": [
            {key: value for key, value in event.items() if key != "received_at"}
            for event in events
        ],
        "frontend_network_ms": round((accepted_at - click_started) * 1000),
        "frontend_ttft_ms": (
            round((first_ws_at - click_started) * 1000) if first_ws_at is not None else None
        ),
        "gateway_websocket_delivery_ms": ws_delivery_ms,
        "frontend_render_after_ws_ms": (
            round((rendered_at - final_ws_event["received_at"]) * 1000)
            if final_ws_event is not None else None
        ),
        "e2e_total_latency_ms": round((rendered_at - click_started) * 1000),
        "websocket_phase_breakdown_ms": ws_breakdown,
        "started_at_utc": started_at_utc.isoformat(),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "assignment_id": assignment_id,
    }


def _normalize_source(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _elapsed_ms(start: float | None, end: float | None) -> int | None:
    return round((end - start) * 1000) if start is not None and end is not None else None


def _websocket_delivery_ms(event: dict[str, Any] | None) -> int | None:
    if event is None or not event.get("timestamp_utc"):
        return None
    try:
        sent = datetime.fromisoformat(str(event["timestamp_utc"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    received = datetime.fromisoformat(str(event["received_at_utc"]))
    elapsed = round((received - sent).total_seconds() * 1000)
    return elapsed if elapsed >= 0 else None


async def close_student_context(context: Any) -> None:
    await context.close()
