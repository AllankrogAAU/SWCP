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

CodeNet assignments use an isolated `codenet-gnu11` compiler profile (`-std=gnu11`, warnings, optimization, and `-lm`); regular teacher assignments retain the standard C11 profile. Before any model requests start, the benchmark validates each accepted reference and candidate through the sandbox-only `validate` action. It retains only candidates whose observed compile/runtime/sample-test stage matches the CodeNet label, and writes rejected candidates and validation outcomes to `validation.json`. Increase `--validation-candidate-multiplier` (default 10; only about 1 in 5 CodeNet `RE` labels reproduces on the sample input) if the initial candidate pool does not yield enough verified examples.

Validation also runs a token preflight: a candidate is kept only if its estimated `with_context_transfer` prompt (problem description, unaltered source, and the cSandbox report, built as in `SubmissionPromptBuilder`) is below `--token-budget` (default 2,500 tokens). This keeps every request well within the vLLM context window (`VLLM_MAX_MODEL_LEN=8192` with `max_tokens=4096`). Rejected candidates are replaced by the next unaltered candidate of the same status. Validation requests that do not complete are retried, then logged and skipped instead of aborting. If a status is still underfilled, the shortfall is logged and the run continues with the validated samples. The final selection is written to `samples.json`, which the judge uses.

## Run

From the repository root:

```bash
set -a && . ./.env && set +a
~/.venvs/swcp-benchmark/bin/python -m apps.benchmark.runner \
  --archive /home/test/Downloads/Project_CodeNet.tar.gz \
  --target-url http://localhost --skip-fixtures
```

By default this selects 100 unaltered submissions each for `CE`, `RE`, `WA` and `AC` (400 in total), each preflighted to fewer than 2,500 estimated prompt tokens including the system prompt and sandbox context. It runs them with `with_context_transfer` through the default model matrix (see below) and then through `azure-gpt-6-sol`. It then judges every response with GPT-6-sol and writes the figures and tables. Each submission that does not complete is retried up to `--max-attempts` (default 3) with exponential backoff; persistent failures are recorded, with their failed attempt IDs, and the run continues. After each model swap, `--warmup-requests` (default 2) unrecorded end-to-end requests are sent. Live progress output is off by default; pass `--progress-file apps/benchmark/LIVE_PROGRESS.md` to rewrite it every `--progress-every` records and on every phase change.

Model swaps scale the `vllm` deployment to zero. They wait for the old pod to terminate and for GPU memory to be released, apply the new model, scale back up, and wait for `/health` to return 200. Every swap is logged in `run_metadata.json` (`model_swaps`). Use `--skip-judge` and `--skip-report` to run only the sweep.

You can also pass the legacy options explicitly: `--statuses AC CE RE WA TLE MLE`, `--context-modes with_context_transfer raw_code_only`, and a larger `--per-status`. The run sends up to a bounded four-request burst per model, with at most one sample per selected CodeNet status. The configured vLLM maximum sequence count is four; larger bursts are rejected. Local models are switched through the single-replica `vllm` deployment one at a time. The deployment's original model environment is restored in a `finally` block.

Use `--context-modes with_context_transfer` to run a single context condition, `--burst-size 0` to disable bursts, `--skip-fixtures` to omit SWCP fixtures, and `--models` to select a subset. For an initial small run, use `--per-status 1`; the full default is six statuses per model and context condition. Local model switching updates both the vLLM served model and the LLM worker's requested model name, then restores both deployments' original configuration.

Available local model keys include `qwen2.5-coder-3b`, `qwen2.5-coder-7b`, `qwen2.5-coder-14b`, `meta-llama-3.1-8b`, `gemma-2-9b`, `mistral-nemo-12b`, `deepseek-r1-distill-llama-8b`, and `deepseek-r1-distill-qwen-7b`. The corresponding AWQ model folders must exist under `apps/localLLM/models/`; the benchmark checks for the selected folder before switching vLLM. When `--models` is omitted, the installed models of the default matrix run serially in this order: `qwen2.5-coder-7b`, `meta-llama-3.1-8b`, `gemma-2-9b`, `mistral-nemo-12b`, `deepseek-r1-distill-llama-8b`, `qwen2.5-coder-14b`, `qwen2.5-coder-3b`, then `azure-gpt-6-sol`.

Each model spec can override vLLM and llm-worker environment variables for the duration of its pass. `gemma-2-9b` and `mistral-nemo-12b` run with `VLLM_MAX_MODEL_LEN=4096` and `VLLM_GPU_MEMORY_UTILIZATION=0.90`, and Gemma-2 merges the system prompt into the user turn because its chat template has no system role. Every local model is capped at `LocalLlm__MaxTokens=1280` output tokens so that a preflighted prompt plus the completion always fits a 4,096-token context. All overrides are removed again on restore.

The cloud models are `azure-gpt-6-sol` (llm-worker key `Gpt6Sol`, the default baseline) and `azure-gpt-4o` (`Gpt4o`). No Azure credentials are written to results.

## LLM-as-a-judge

`judge.py` scores each sequential CodeNet response with GPT-6-sol. The judge receives the problem description, the line-numbered unaltered student code, the deterministic cSandbox report, the accepted reference, a diff between them, and the feedback. The judge never sees the model name. Its output is strict JSON with these metrics:

- `bug_localization_f1`: F1 between the judge's ground-truth faulty lines and the lines the feedback identifies (±1 line).
- `root_cause_diagnosis_accuracy`: 0, 0.5 or 1.
- `false_positive_hallucination_rate`: share of `AC` responses that report a bug that does not exist.
- `non_spoiler_compliance`: 1 only if both the judge and a deterministic leak detector agree there is no leak. The detector flags fenced or unfenced corrected C code, a `main` definition, and copied accepted-reference lines.
- `socratic_guidance_score` and `actionability_and_clarity`: 1–5.
- 5-point binary checklist: normalized to 0–100%.
- Socratic sub-scores (1–5): `socratic_elicitation`, `socratic_scaffolding`, `socratic_tone`.
- Actionability sub-scores (1–5): `jargon_suitability`, `next_step_clarity`.
- Technical precision (0/1, faulty submissions only): `line_precision`, `concept_precision`.
- `hallucinated_issue_categories`: the categories of invented defects (e.g. Memory Leak, Type / Format Warning) when the feedback reports a bug that does not exist.

Credentials are read from `JUDGE_ENDPOINT`/`JUDGE_API_KEY` or, by default, from the `llm-worker-config` and `llm-worker-secrets` Kubernetes objects; they are never written to disk. Use `--judge-provider openai --judge-endpoint http://.../v1 --judge-model <name>` for an OpenAI-compatible judge, such as a local vLLM server. Requests use `temperature=0.0`. If the deployment rejects an explicit temperature, which the GPT-6-sol deployment does, the default is used and this is recorded in `judge_config.json`. Judging resumes from `judgements.jsonl` and retries earlier `judge_error` entries:

```bash
python -m apps.benchmark.judge benchmarks/results/<run-id>
python -m apps.benchmark.report benchmarks/results/<run-id> \
  --azure-input-usd-per-mtok <price> --azure-output-usd-per-mtok <price>
```

`report.py` writes `report_<timestamp>/` containing:

- `fig1`–`fig10` as 300 DPI PDF and PNG: fig7 is a Socratic sub-score radar, fig8 compares jargon suitability with next-step clarity, fig9 compares line precision with concept precision, and fig10 shows the distribution of hallucinated issue categories on `AC` submissions.
- `table_quality_summary`, `table_status_breakdown`, `table_qos_summary`, `table_granular_subscores` and `table_hallucination_categories` as CSV and LaTeX.
- `report_metadata.json`.

The overall quality score (fig1) is the mean of seven 0–100 components and excludes QoS:

- F1
- root-cause accuracy
- 100 − hallucination rate
- non-spoiler compliance
- Socratic score, normalized as (x−1)/4·100
- actionability, normalized the same way
- checklist

Local cost per 1,000 runs is mean GPU power × mean run time × electricity price, plus the amortized GPU purchase price. Both parameters are configurable with `--electricity-usd-per-kwh`, `--gpu-capex-usd` and `--gpu-lifetime-hours`. Azure cost is shown only when token prices are supplied. Existing submissions that omit `contextMode` or `azureModelKey` retain the current behavior and configured Azure default.

## Artifacts

Each run writes to `benchmarks/results/<run-id>/`:

- `results.jsonl`: an fsync'd submission result before telemetry collection, a joined telemetry-update event, and one-second GPU samples
- `run_metadata.json`: run configuration, git revision, per-model windows, warmups, and model swap log
- `samples.json` and `validation.json`: the selected unaltered CodeNet submissions and the validation/preflight outcomes
- `judgements.jsonl` and `judge_config.json`: raw judge output, derived metrics, and the judge configuration (without credentials)
- `report_<timestamp>/`: the six publication figures and the CSV/LaTeX tables
- `prometheus_<model>.json`: raw queried time series and per-query errors
- `summary.json` and `summary.csv`: latency percentiles and CodeNet sandbox-status agreement
- `ttft_by_model.png`, `e2e_latency_by_model.png`, and `status_agreement.png` when matching data is available

To finalize an existing complete or partial run without submitting more code or changing model deployments:

```bash
python -m apps.benchmark.finalize_codenet benchmarks/results/<run-id>
```

The finalizer reads only `results.jsonl` and `run_metadata.json`. It writes a failure analysis, full-split and matched-intersection CSV/JSON summaries, publication-ready PNG/PDF charts, and LaTeX tables. For a partial context-mode ablation, matched metrics include only CodeNet submission IDs completed by every model in both context modes; full per-model coverage is reported separately.

`frontend_ttft_ms` is measured client-side from submission start to the first streamed feedback delta. It is not inferred from buffered completion time. Missing Loki/Tempo/Prometheus data is recorded explicitly instead of being treated as zero.
