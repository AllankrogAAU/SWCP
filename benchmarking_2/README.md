# SWCP Benchmarking 2

This benchmark compares model feedback through the real SWCP submission API. It does not use
the deterministic solution-leak detector, TTFT, token throughput, or cost metrics.

Install the benchmark-only dependencies with:

```bash
python -m pip install -r benchmarking_2/requirements.txt
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
python -m benchmarking_2 latency --models local:qwen,azure:GPT-6-sol
python -m benchmarking_2 complete --models local:qwen,azure:GPT-6-sol
python -m benchmarking_2 test-test --models local:qwen
```

`complete` and `latency` use 100 records from every category. `test-test` uses 50 records from
every category, judges every response, and is intended to validate the result format and cloud
judge configuration.

The same deterministic sample is used for every model. Two ordinary pipeline submissions are
sent after each model switch and stored as warm-up records, excluded from measured results.

Set `TARGET_URL`, `BENCHMARK_MODELS`, `JUDGE_URL` and `JUDGE_API_KEY`, or pass the corresponding
CLI options. For Azure Foundry, set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`,
`AZURE_OPENAI_API_KEY`, and optionally `AZURE_OPENAI_API_VERSION`.

If model changes require an external operation, use a command template:

```bash
python -m benchmarking_2.cli complete \
  --models local:qwen \
  --switch-command "ssh cluster 'switch-vllm --model {model}'"
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
