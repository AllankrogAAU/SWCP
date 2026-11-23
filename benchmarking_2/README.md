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

## Single-Command Complete Benchmark

Run the entire benchmark (all 9 local models + `azure:GPT-6-sol`, 50 submissions across each of the 14 submission categories = 700 submissions per model at 100 submissions/minute with `VLLM_MAX_NUM_SEQS=10`, cloud judging via Azure GPT-6-sol, CSV export, and `fig1`–`fig10` rendering) with a single command:

```bash
python -m benchmarking_2 complete
```

Or use the self-contained helper script (which automatically selects `.venv-benchmarking2` or `~/.venvs/swcp-benchmark`):

```bash
bash scripts/run-benchmarking-2.sh
```

## Commands

Prepare or refresh the persistent normalized 14-category dataset first (also done automatically by `complete`, `latency`, and `test-test`):

```bash
python -m benchmarking_2 prepare
```

This creates [`benchmarking_2/submissions.sqlite`](C:/Users/allan/source/repos/AllankrogAAU/SWCP/benchmarking_2/submissions.sqlite)
with `category_folders`, `assignments`, and `submissions` tables across 14 categories (`correct` plus 13 error categories).

```bash
python -m benchmarking_2 complete
python -m benchmarking_2 latency
python -m benchmarking_2 test-test --models local:qwen2.5-coder-14b-instruct-awq
```

Run the benchmark using only Qwen3 14B without contacting the cloud judge:

```bash
python -m benchmarking_2 latency --models local:qwen3-14b-awq
```

`latency` records submissions and timings but skips cloud judging.

Benchmark runs detach from the terminal by default, so you can close the VS Code window
without stopping `complete`, `latency`, `test-test`, or `judge`. Their output is written to
`benchmarking_2/<mode>.log` and the process ID is stored in `benchmarking_2/<mode>.pid`.
Use `--foreground` to keep a benchmark attached to the current terminal instead:

```bash
python -m benchmarking_2 test-test --models local:qwen2.5-coder-14b-instruct-awq --foreground
```

`complete`, `latency`, and `test-test` use 50 records from every category (700 submissions per model across the 14 categories).

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
The submissions CSV includes `confidence_score_percent`, read from the submission API's
`confidenceScorePercent` field when the target API provides it.

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
