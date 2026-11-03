# SWCP CodeNet Benchmark

This package samples Project CodeNet C submissions, provisions the corresponding SWCP assignments through the teacher-only API, runs each requested local/Azure model serially, and stores client streaming timings plus Loki, Tempo, Prometheus, and GPU telemetry.

## Requirements

- Python packages from [`benchmarks/requirements.txt`](../../benchmarks/requirements.txt)
- `kubectl` access to the `swcp` namespace
- `nvidia-smi` for GPU sampling
- A teacher bearer token in `BENCHMARK_TEACHER_TOKEN`; assignments are never created by bypassing authorization
- An API URL in `TARGET_URL` or `--target-url`
- Project CodeNet archive at the default path or supplied with `--archive`

The archive is read in two streaming passes. The first extracts only problem metadata, descriptions, verified sample input/output, and the CodeNet status lists. The second extracts only the selected C submissions and same-problem accepted references.

CodeNet assignments use an isolated `codenet-gnu11` compiler profile (`-std=gnu11`, warnings, optimization, and `-lm`); regular teacher assignments retain the standard C11 profile. Before any model requests start, the benchmark validates each accepted reference and candidate through the sandbox-only `validate` action. It retains only candidates whose observed compile/runtime/sample-test stage matches the CodeNet label, and writes rejected candidates and validation outcomes to `validation.json`. Increase `--validation-candidate-multiplier` if the initial candidate pool does not yield enough verified examples. When the archive has fewer pairs than the expanded pool target, all available pairs are validated, provided there are at least `--per-status` candidates; the run stops with a clear count and does not start the model sweep if validation underfills any requested status.

## Run

From the repository root:

```bash
python -m apps.benchmark.runner \
  --archive /home/test/Downloads/Project_CodeNet.tar.gz \
  --target-url http://localhost \
  --per-status 100 \
  --statuses AC CE RE WA TLE MLE \
  --models qwen2.5-coder-14b qwen2.5-coder-7b azure-gpt-4o
```

The default run tests both `with_context_transfer` and `raw_code_only`, adds four existing SWCP workload fixtures, and sends up to a bounded four-request burst per model, with at most one sample per selected CodeNet status. The configured vLLM maximum sequence count is four; larger bursts are rejected. Local models are switched through the single-replica `vllm` deployment one at a time. The deployment's original model environment is restored in a `finally` block.

Use `--context-modes with_context_transfer` to run a single context condition, `--burst-size 0` to disable bursts, `--skip-fixtures` to omit SWCP fixtures, and `--models` to select a subset. For an initial small run, use `--per-status 1`; the full default is six statuses per model and context condition. Local model switching updates both the vLLM served model and the LLM worker's requested model name, then restores both deployments' original configuration.

The selected Azure key is `Gpt4o`; no Azure credentials are written to results. Existing submissions that omit `contextMode` or `azureModelKey` retain the current behavior and configured Azure default.

## Artifacts

Each run writes to `benchmarks/results/<run-id>/`:

- `results.jsonl`: an fsync'd submission result before telemetry collection, a joined telemetry-update event, and one-second GPU samples
- `run_metadata.json`: run configuration, git revision, and per-model windows
- `prometheus_<model>.json`: raw queried time series and per-query errors
- `summary.json` and `summary.csv`: latency percentiles and CodeNet sandbox-status agreement
- `ttft_by_model.png`, `e2e_latency_by_model.png`, and `status_agreement.png` when matching data is available

`frontend_ttft_ms` is measured client-side from submission start to the first streamed feedback delta. It is not inferred from buffered completion time. Missing Loki/Tempo/Prometheus data is recorded explicitly instead of being treated as zero.
