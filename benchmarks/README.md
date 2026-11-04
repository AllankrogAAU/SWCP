# SWCP Benchmarking & Load Testing Suite

The **SWCP Benchmarking Suite** is a containerized, production-grade load testing and telemetry framework for the Software Construction Platform (SWCP). It evaluates pipeline throughput, end-to-end latency, queue wait dynamics, vLLM inference efficiency, and Kubernetes autoscaling behavior under realistic traffic models.

---

### Telemetry Correlation
Telemetry is gathered post-run from:
- **Prometheus**: vLLM GPU metrics (`vllm:num_requests_running`, `vllm:gpu_cache_usage_factor`, `vllm:prompt_tokens_total`, `vllm:generation_tokens_total`), pod replica counts (`kube_deployment_status_replicas`), and host CPU/memory.
- **Grafana Loki**: Structured distributed logs via Serilog correlation (`SubmissionId`, `ElapsedMilliseconds`, stage timestamps).
- **Postgres Database**: Exact submission outcomes and stage transition timings.
- **SQLite Database**: Persisted benchmark runs, submission records, and aggregated step summaries (`benchmarks/results/benchmarks.sqlite`).

---

## Prerequisites & Warmup

### Warmup (Cold-Start JIT & CUDA Graph Initialization)
When `vllm` or `llm-worker` pods are first deployed or restarted, the first 1–3 requests incur cold-start overheads (CUDA graph compilation, AWQ Marlin kernel initialization, and .NET JIT).

To prime the pipeline before running benchmark measurements:

```bash
# Send 3 warmup submissions through the full pipeline
./scripts/run-benchmarks.sh warmup --count 3 --target-url http://localhost
```

You can also pass `--warmup` directly to any `run` command to automatically execute 3 warmup requests prior to timed measurements.

---

## Benchmark Suites & Commands

### 1. Steady-State SLA Capacity Curve (`sla_curve`)
Evaluates system stability and finds the exact saturation knee where the p95 end-to-end latency crosses the 30-second SLA limit.

- **Traffic Model**: `adaptive-poisson` (Poisson process where inter-arrival times follow an exponential distribution $\Delta t \sim \text{Exp}(\lambda)$).
- **Ramping Mechanism**: Starts at `--start-rate` and increments by `--rate-delta` per plateau until step $p95 \ge 30\text{s}$, followed by 1 confirmation step. `--max-rate` is optional (default: 0 = unbounded dynamic ramp).
- **Generated Charts**:
  - `sla_capacity_curve.png`: Dual-axis plot of p50/p95 latency and achieved throughput vs arrival rate with a 30s SLA threshold line.
  - `pipeline_latency_breakdown_stacked.png`: Stacked bar chart showing mean additive stage latencies across arrival rates.

```bash
# Run adaptive SLA curve test (dynamic unbounded ramp until p95 >= 30s)
./scripts/run-benchmarks.sh run \
  --suite sla_curve \
  --arrival-mode adaptive-poisson \
  --start-rate 30 \
  --rate-delta 10 \
  --target-p95-s 30 \
  --step-duration-s 75 \
  --min-completions 10 \
  --users 150 \
  --warmup \
  --target-url http://localhost
```

---

### 2. Classroom Deadline Burst Shockwave (`burst`)
Simulates students simultaneously hitting "Submit" right before a deadline (e.g., 20, 40, 60, 80 submissions dispatched within a 2-second window).

- **Traffic Model**: Synchronized bursts dispatched concurrently over `--burst-window-s`.
- **Metrics Evaluated**: Queue drain latency, backpressure absorption, memory pressure, and maximum queue duration under thundering herds.
- **Generated Chart**:
  - `burst_absorption_curve.png`: Burst size vs. queue drain time and p50/p95 latency.

```bash
# Run synchronized burst shockwave test
./scripts/run-benchmarks.sh run \
  --suite burst \
  --arrival-mode burst \
  --bursts 20,40,60,80 \
  --burst-window-s 2.0 \
  --users 100 \
  --warmup \
  --target-url http://localhost
```

---

### 3. Infinite GPU Service Scaling & Autoscaling (`infinite_gpu`)
Isolates and stress-tests non-GPU components (API Gateway, PostgreSQL connection pools, NATS message bus, and gVisor `c-sandbox` code execution) by bypassing LLM GPU inference with a 5ms synthetic mock response.

- **Automation**: `run-benchmarks.sh` automatically patches `llm-worker-config` to enable mock mode (`LocalLlm__MockEnabled: "true"`, `LocalLlm__MockLatencyMs: "5"`), runs the test, and restores local LLM mode upon completion or interruption.
- **Generated Chart**:
  - `service_scaling_stacked_area.png`: Stacked area chart showing contributing service stage latencies with total client e2e p50/p95 overlays, accompanied by a pod replica scaling timeline.

```bash
# Run Infinite GPU service scaling test (dynamic unbounded ramp until p95 >= 15s)
./scripts/run-benchmarks.sh run \
  --suite infinite_gpu \
  --arrival-mode adaptive-poisson \
  --start-rate 300 \
  --rate-delta 60 \
  --target-p95-s 15 \
  --step-duration-s 60 \
  --users 200 \
  --target-url http://localhost
```

---

### 4. Hardware Parameter Sweep (`vllm_sweep`)
Automates the optimization of vLLM parameters (`VLLM_GPU_MEMORY_UTILIZATION` and `VLLM_MAX_NUM_SEQS`) to maximize throughput and token generation speed while preventing KV cache allocation exhaustion.

- **Script**: `scripts/sweep-vllm.sh`
- **Features**:
  - Restarts `vllm` and `llm-worker` deployments for each configuration.
  - Automatically runs a 3-request warmup after pod rollout.
  - Waits for NATS and PostgreSQL pipeline queues to fully drain between iterations.
  - Computes optimal configuration and exports `winner.env`.
  - Optionally applies the winning configuration to the cluster with `--apply-winner`.

```bash
# Run 4-configuration sweep over max_num_seqs (4, 8, 12, 16) at 0.88 GPU memory utilization
./scripts/sweep-vllm.sh \
  --mem-values 0.88 \
  --seqs-values 4,8,12,16 \
  --rate 16 \
  --duration 90 \
  --min-completions 16 \
  --apply-winner \
  --target-url http://localhost
```

To re-print the sweep summary table and re-generate the plot from previous results without re-running tests:

```bash
./scripts/sweep-vllm.sh --report-only
```

---

## Utility Commands

### Smoke Test
Runs a fast 2-request verification test through Ingress and all microservices:

```bash
./scripts/run-benchmarks.sh smoke-test --target-url http://localhost
```

### Pre-Seeding Virtual Users
Pre-registers virtual test students and caches JWT tokens in SQLite and JSON:

```bash
./scripts/run-benchmarks.sh seed --users 150 --target-url http://localhost
```

### Re-Generating Plots & Exporting Artifacts
To re-generate plots or export CSV/JSON artifacts for any previous run:

```bash
# Generate charts for the latest run
./scripts/run-benchmarks.sh plot --run-id latest

# Generate charts for a specific run ID
./scripts/run-benchmarks.sh plot --run-id sla_curve_20261028_120000_a1b2c3

# Export CSV and JSON metrics
./scripts/run-benchmarks.sh export --run-id latest
```

---

## Artifact Locations

All output files are saved under `benchmarks/results/<RUN_ID>/`:
- `summary.json`: High-level run metrics (p50, p95, throughput, token speed, pod counts).
- `submissions.csv`: Per-submission breakdown with stage timings (sandbox, queue wait, LLM inference).
- `step_summaries.csv`: Aggregated metrics grouped by rate or burst step.
- `*.png`: Publication-ready visual charts (300 DPI).
- `benchmarks.sqlite`: Master database containing all historical runs.

