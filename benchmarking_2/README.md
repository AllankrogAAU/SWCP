# SWCP Benchmarking 2

This benchmark compares model feedback through the real SWCP submission API. It does not use
the deterministic solution-leak detector, TTFT, token throughput, or cost metrics.

Install the benchmark-only dependencies in a virtual environment:

```bash
python3 -m venv .venv-benchmarking2
. .venv-benchmarking2/bin/activate
python -m pip install --upgrade pip
python -m pip install -r benchmarking_2/requirements.txt
```

Activate the environment again before every benchmark command in a new shell:

```bash
cd /home/test/SWCP
. .venv-benchmarking2/bin/activate
```

## Commands

Prepare the persistent normalized dataset first:

```bash
python -m benchmarking_2 prepare
```

This creates [`benchmarking_2/submissions.sqlite`](C:/Users/allan/source/repos/AllankrogAAU/SWCP/benchmarking_2/submissions.sqlite)
with `category_folders`, `assignments`, and `submissions` tables. The benchmark rebuilds
or refreshes this database automatically when it runs.

```bash
python -m benchmarking_2 latency --models local:qwen2.5-coder-14b-instruct-awq,azure:GPT-6-sol
python -m benchmarking_2 complete --models local:qwen2.5-coder-14b-instruct-awq,azure:GPT-6-sol
python -m benchmarking_2 test-test --models local:qwen2.5-coder-14b-instruct-awq
```

Benchmark runs detach from the terminal by default, so you can close the VS Code window
without stopping `complete`, `latency`, `test-test`, or `judge`. Their output is written to
`benchmarking_2/<mode>.log` and the process ID is stored in `benchmarking_2/<mode>.pid`.
Use `--foreground` to keep a benchmark attached to the current terminal instead:

```bash
python -m benchmarking_2 test-test --models local:qwen2.5-coder-14b-instruct-awq --foreground
```

`complete` and `latency` use 100 records from every category. `test-test` uses 50 records from
every category, judges every response, and is intended to validate the result format and cloud
judge configuration.

The same deterministic sample is used for every model. Two ordinary pipeline submissions are
sent after each model switch and stored as warm-up records, excluded from measured results.

The automatic local-model list is discovered from `apps/localLLM/models/`. The current
directories are:

```text
deepseek-r1-distill-llama-8b-awq
deepseek-r1-distill-qwen-7b-awq
gemma-2-9b-it-awq-int4
meta-llama-3.1-8b-instruct-awq-int4
mistral-nemo-instruct-2407-awq
qwen2.5-coder-3b-instruct-awq
qwen2.5-coder-7b-instruct-awq
qwen2.5-coder-14b-instruct-awq
qwen3-14b-awq
qwen35-9b-awq-4bit
```

Set `TARGET_URL`, `BENCHMARK_MODELS`, `JUDGE_URL` and `JUDGE_API_KEY`, or pass the corresponding
CLI options. For Azure Foundry, set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`,
`AZURE_OPENAI_API_KEY`, and optionally `AZURE_OPENAI_API_VERSION`.

If model changes require an external operation, use a command template:

```bash
python -m benchmarking_2.cli complete \
  --models local:qwen \
  --switch-command "ssh cluster 'switch-vllm --model {model}'"
```

By default, local models are discovered automatically from
`apps/localLLM/models/` and switched automatically through the Kubernetes vLLM
deployments. `--switch-command` is optional and only replaces the automatic
switcher for custom cluster setups.

Check a run without reconnecting to the benchmark log:

```bash
python3 -m benchmarking_2 status
watch -n 15 python3 -m benchmarking_2 status
```

Results are SQLite files under `benchmarking_2/results/`. Loki stage timings are collected when
`LOKI_URL` is configured; client timing remains available without monitoring access.

The sandbox telemetry is backend-aware. When Loki is configured, each submission stores:

- Sandbox backend: `runsc` or `k8s`
- Sandbox runtime: `runsc:<platform>` or `k8s:<runtime-class>`
- Acquisition time and whether Kubernetes acquisition came from the warm pool
- GCC compile time and C binary/test execution time
- c-sandbox worker node and pod

Queue waits are derived from Loki pipeline timestamps when no worker-emitted
`queue_wait_ms` field is available.

The cluster currently defaults to `SANDBOX_BACKEND=runsc`. Kubernetes sandbox-pod mode can be
enabled with `SANDBOX_BACKEND=k8s`; the benchmark reads the backend and runtime from sandbox
completion telemetry rather than assuming that every submission used a Kubernetes pod.
