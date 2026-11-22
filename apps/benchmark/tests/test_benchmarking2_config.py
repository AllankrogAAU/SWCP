import json
import sqlite3
import base64
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from benchmarking_2.cli import launch_detached
from benchmarking_2.config import Config
from benchmarking_2.pipeline import PipelineClient
from benchmarking_2.storage import Store
from benchmarking_2.cluster_switch import switch_local_model


def test_judge_defaults_to_configured_azure_deployment(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-6-sol")
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.delenv("JUDGE_URL", raising=False)

    config = Config()

    assert config.judge_model == "gpt-6-sol"
    assert config.judge_endpoint().startswith(
        "https://example.openai.azure.com/openai/deployments/gpt-6-sol/chat/completions?"
    )


def test_judge_endpoint_normalizes_foundry_model_alias(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "Gpt6Sol")
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.delenv("JUDGE_URL", raising=False)

    config = Config()

    assert "/deployments/gpt-6-sol/" in config.judge_endpoint()


def test_pipeline_refreshes_expired_benchmark_token(monkeypatch):
    monkeypatch.setenv("TARGET_URL", "http://example.test")
    config = Config()
    model = SimpleNamespace(name="test-model", backend="local")
    claims = base64.urlsafe_b64encode(
        json.dumps({"preferred_username": "bench2_user_1"}).encode()
    ).rstrip(b"=").decode()
    token = f"header.{claims}.signature"
    client = PipelineClient(config, model, [token], {})

    async def refresh():
        async with httpx.AsyncClient(
            base_url="http://example.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"accessToken": "refreshed-token"},
                )
            ),
        ) as http_client:
            return await client._refresh_token(token, http_client)

    import asyncio
    refreshed = asyncio.run(refresh())

    assert refreshed == "refreshed-token"
    assert client.tokens[0] == "refreshed-token"


def test_store_does_not_persist_judge_api_key(tmp_path):
    database = tmp_path / "run.sqlite"
    store = Store(database)

    store.start_run(
        "test-run",
        "test-test",
        "hash",
        50,
        True,
        {"judge_api_key": "secret", "judge_model": "gpt-6-sol"},
    )

    with sqlite3.connect(database) as connection:
        config_json = connection.execute(
            "SELECT config_json FROM runs WHERE run_id=?",
            ("test-run",),
        ).fetchone()[0]

    assert "secret" not in config_json
    assert json.loads(config_json)["judge_model"] == "gpt-6-sol"


def test_local_model_switch_does_not_require_overflow(tmp_path):
    model_dir = tmp_path / "meta-llama-3.1-8b-instruct-awq-int4"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    calls = []

    def fake_run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:4] == ["kubectl", "get", "configmap", "vllm-config"]:
            return type("Result", (), {"stdout": "meta-llama-3.1-8b-instruct-awq-int4"})()
        if "endpointslices" in arguments:
            return type("Result", (), {"stdout": "10.42.2.168=true\n"})()
        return type("Result", (), {"stdout": "", "returncode": 0})()

    with (
        patch("benchmarking_2.cluster_switch.subprocess.run", side_effect=fake_run),
        patch("benchmarking_2.cluster_switch.time.sleep"),
    ):
        switch_local_model("meta-llama-3.1-8b-instruct-awq-int4", tmp_path)

    restarted = [
        arguments
        for arguments in calls
        if arguments[:3] == ["kubectl", "rollout", "restart"]
    ]
    assert restarted == [[
        "kubectl", "rollout", "restart",
        "deployment/vllm", "deployment/llm-worker", "-n", "swcp",
    ]]


def test_launch_detached_starts_new_session_and_writes_pid(tmp_path):
    pid_path = tmp_path / "test-test.pid"
    log_path = tmp_path / "test-test.log"
    process = SimpleNamespace(pid=4242)

    with patch("benchmarking_2.cli.subprocess.Popen", return_value=process) as popen:
        pid = launch_detached(
            ["python", "-m", "benchmarking_2", "test-test", "--foreground"],
            pid_path,
            log_path,
        )

    assert pid == 4242
    assert pid_path.read_text(encoding="utf-8") == "4242\n"
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["stdin"] is not None
