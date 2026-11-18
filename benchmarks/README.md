# SWCP Benchmarking & Load Testing Suite

The **SWCP Benchmarking Suite** evaluates throughput, end-to-end latency, stage-by-stage delays, vLLM inference efficiency, and 2-node spillover/autoscaling behavior across the Software Construction Platform (SWCP).

It is designed around **two core principles** for the 2-node cluster:
1. **Preserve the idle worker-node baseline**: `run-benchmarks.sh` never pre-warms compute pods on `worker-node` — every capacity/ramp/burst test waits until spilled compute pods on `worker-node` have scaled back to `0` (`vllm-overflow` stays running persistently), measuring true scale-out spill behavior.
2. **Separate duration-independent sustained capacity from finite-window burst SLAs**:
   - **Continuous Ramps** (`continuous-ramp`) smoothly escalate arrivals from low load past saturation using exact Lewis–Shedler non-homogeneous Poisson thinning, measuring **Achieved Completion Rate (`completed/min`)** vs. **Arrival Rate** and active **Queue Backlog (`in_flight_end`)** so the true sustainable ceiling ($\mu$) is independent of test duration.
   - **Targeted Stepped Plateaus** (`sla_curve`) verify steady-state latency and stage breakdowns around the knees discovered by the ramp, explicitly distinguishing the **Sustained Zone ($dQ/dt \approx 0$)** from **$T$-second Burst Overload SLAs**.

---

## Configuration (`benchmarks/suites.toml`)

All suite parameters (rates, ramp durations, bucket widths, burst intervals, vLLM sweep grids, user pool size) live in [`benchmarks/suites.toml`](file:///home/test/SWCP/benchmarks/suites.toml). Edit that file to adjust defaults or increase resolution, or override any setting on the CLI.

Defaults configured for the **2-node RTX 5070 Ti** cluster:
- **`vllm_sweep`**: closed-loop `--concurrency 16`, `120s` per point, `mem_values = "0.88"`, `seqs_values = "4,8,12,16"`.
- **`infinite_gpu`**: `continuous-ramp` `100 -> 800 subs/min` over `420s` (`30s` buckets), `mock_llm = true`.
- **`ramp_capacity`**: `continuous-ramp` `30 -> 260 subs/min` over `480s` (`30s` buckets).
- **`sla_curve`**: targeted `stepped-poisson` plateaus at `"40,80,120,160,200,240"` subs/min (`90s` each).
- **`burst`**: classroom deadline bursts of `"20,40,60,80,100,120"` simultaneous submissions within `3.0s`.

---

## Telemetry & Metrics Collected

- **Client & Edge Network Breakdown (`submissions.csv`, `summary.csv`)**:
  - `client_submit_ms` vs. server-side `http_enqueue_ms` $\rightarrow$ `submit_edge_network_ms`
  - `orchestration_overhead_ms` (NATS/controller glue gap inside `pipeline_total_ms`)
  - `delivery_overhead_ms` (Ingress/WebSocket push return delay to client)
  - `terminal_via` and `ws_completion_rate`
- **2-Node Attribution & Cluster Baselines (`run_metadata.json`, `prometheus`, `loki`)**:
  - `llm_target` (`primary`, `overflow`, `overflow_fallback`) and `p95_llm_exec_primary_ms` vs. `p95_llm_exec_overflow_ms`
  - `sandbox_node` / `llm_worker_node` and `control-node` vs. `worker-node` max pod replicas
  - `target_http_rtt_ms`, `clock_offset_s` (automatically aligns off-cluster Loki/Prometheus query windows), and `inter_node_rtt_ms`
- **Queue Saturation Ground Truth (`summary.csv`)**:
  - `achieved_throughput_per_min` (active-window completion rate), `in_flight_end` (un-drained backlog before drain), `backlog_growth_per_min`, `max_e2e_ms` (total burst drain time), and `is_sustained` (`1` vs `0`).

---

## Running from a Remote Non-Cluster Machine (`--ssh-host`)

To avoid consuming CPU/network on `control-node` and capture realistic LAN/Tailscale edge latency, run the benchmark container from an external machine:

### Prerequisites on the Remote Machine
1. Docker and passwordless SSH key access (`ssh <user>@<control-node>`) to `control-node`.
2. A local checkout of the `SWCP` repository (`git pull` so `benchmarks/` and `scripts/` match the cluster).

### How Remote Mode Works
- Pass **`--ssh-host <user@control-node>`** (or export `SWCP_CONTROL_SSH=<user@control-node>`).
- **Load traffic** (`POST /api/submissions`, `GET`, `WS`) goes **directly** over the network to `--target-url` (defaults automatically to `http://<control-node>` if `--target-url` is omitted).
- **Cluster orchestration & monitoring** (`kubectl`, Postgres/NATS pipeline drain, worker cooldown checks, `.env` updates, and out-of-band `ssh -N -L` tunnels to Prometheus & Loki ClusterIPs) run automatically via SSH.

```bash
# Optional: export once for the session instead of repeating --ssh-host
export SWCP_CONTROL_SSH=test@172.25.11.240

# Quick 2-request verification from remote machine
./scripts/run-benchmarks.sh smoke-test --ssh-host "$SWCP_CONTROL_SSH"
```

*(When running directly on `control-node`, simply omit `--ssh-host` and use `--target-url http://localhost`.)*

---

## Benchmark Suites & Commands (5-Goal Workflow)

### Goal 1: Tune Single-GPU vLLM (`vllm_sweep`)
Isolates primary `vllm` (disabling overflow routing during the sweep) across `(MEM, SEQS)` combinations under saturated closed-loop concurrency, then optionally replicates the winning config to both GPUs and `.env`.

- **Chart**: `vllm_tuning_matrix.png`
```bash
# From remote machine (uses defaults from benchmarks/suites.toml: concurrency=16, 120s/step)
./scripts/sweep-vllm.sh \
  --ssh-host "$SWCP_CONTROL_SSH" \
  --mem-values 0.88 \
  --seqs-values 4,8,12,16 \
  --apply-winner
```
Re-print the comparison table and regenerate the Pareto chart from SQLite without re-running:
```bash
./scripts/sweep-vllm.sh --report-only
```

---

### Goal 2: Find Non-GPU & Autoscaler Limits (`infinite_gpu`)
Bypasses GPU inference with a 5ms synthetic mock response on `llm-worker`, running a smooth `continuous-ramp` so KEDA/HPA scaling and `worker-node` pod spillover are observed in sequence without discrete step-reset distortion.

- **Chart**: `service_scaling_stacked_area.png` (Arrival vs. Completion Rate + Backlog, Stage Latencies, Control- vs. Worker-Node Replicas)
```bash
./scripts/run-benchmarks.sh run \
  --ssh-host "$SWCP_CONTROL_SSH" \
  --suite infinite_gpu
```
Override ramp bounds inline if needed:
```bash
./scripts/run-benchmarks.sh run \
  --ssh-host "$SWCP_CONTROL_SSH" \
  --suite infinite_gpu \
  --start-rate 150 --end-rate 1000 --ramp-duration-s 480 --bucket-duration-s 30
```

---

### Goal 3: Find 2-Node Spillover Threshold & Dual-GPU Ceiling (`ramp_capacity`)
Runs a single slow `continuous-ramp` on real vLLM starting from `0` spilled compute pods on `worker-node`. Pinpoints:
1. Exact arrival rate where `vllm-overflow` activates,
2. Rate where compute pods (`c-sandbox`, `llm-worker`) spill onto `worker-node`, and
3. The duration-independent **sustained completion ceiling** ($\mu_1 + \mu_2$, where completion rate flattens below arrival rate and backlog rises).

- **Chart**: `spillover_saturation_ramp.png`
```bash
./scripts/run-benchmarks.sh run \
  --ssh-host "$SWCP_CONTROL_SSH" \
  --suite ramp_capacity
```

---

### Goal 4: Verify Steady-State SLA & Stage Breakdown (`sla_curve`)
Runs targeted fixed-rate Poisson plateaus around the knees identified in Goal 3 (e.g., `40,80,120,160,200,240` subs/min from `benchmarks/suites.toml`). Produces both:
- **`sla_capacity_curve.png`**: Explicitly shades the **Sustained Steady Zone** ($dQ/dt \approx 0$) vs. the **Finite $T$-Second Burst Overload Zone** (annotating crossings accurately as `Sustained:` vs. `90s Burst:`).
- **`pipeline_latency_breakdown_stacked.png`**: Mean additive stage breakdown including Edge Network, API Enqueue, Sandbox, Bus/Orchestration, LLM Queue, and vLLM Inference, with overloaded bars flagged as `[Overload @90s]`.

```bash
./scripts/run-benchmarks.sh run \
  --ssh-host "$SWCP_CONTROL_SSH" \
  --suite sla_curve
```
*(Customize rates around the exact knee found in Goal 3 with e.g. `--rates 60,100,140,170,210 --step-duration-s 120`.)*

---

### Goal 5: Classroom Deadline Burst Absorption & Drain Time (`burst`)
Fires simultaneous submission waves (`20,40,60,80,100,120` in `3s` by default) from a clean worker baseline and drains to 100% completion. Tests cold-start ingestion spikes, zero dropped requests, spillover engagement speed, and total wall-clock drain time.

- **Chart**: `burst_absorption_curve.png` (`p50`, `p95`, Total Drain Time `100% Done`, Overflow GPU %, and Success Rate %)
```bash
./scripts/run-benchmarks.sh run \
  --ssh-host "$SWCP_CONTROL_SSH" \
  --suite burst
```

---

## Utility Commands & Artifacts

- Pass `--skip-worker-cooldown` to `./scripts/run-benchmarks.sh` to skip waiting for `worker-node` pods to scale back to 0 between back-to-back runs.
- Pass `--warmup` / `--no-warmup` to toggle 3 pre-run warmup submissions (enabled by default in `benchmarks/suites.toml`).
- Re-plot or export an existing run:
```bash
./scripts/run-benchmarks.sh plot --run-id latest
./scripts/run-benchmarks.sh export --run-id latest
```

All outputs are stored under `benchmarks/results/<RUN_ID>/`:
- `run_metadata.json`: Hardware/vLLM settings plus `target_http_rtt_ms`, `inter_node_rtt_ms`, and `clock_offset_s`.
- `submissions.csv`: Per-submission stage timings, edge/orchestration/delivery delays, `llm_target`, and pod node names.
- `summary.csv`: Per-step/bucket percentiles, completion rate, queue backlog, KV cache %, and per-node pod replicas.
- `*.png`: Suite-specific 300 DPI Matplotlib charts.
- `benchmarks.sqlite`: Master SQLite database across all runs.




Commands I run :smile:

Remote benchmarks:

Smoketest:
.\scripts\run-benchmarks.ps1 smoke-test --ssh-host school-project-pc

Dual-GPU Continuous Ramp Test:
.\scripts\run-benchmarks.ps1 run --suite ramp_capacity --ssh-host school-project-pc

Infinite-GPU Scaling Test
.\scripts\run-benchmarks.ps1 run --suite infinite_gpu --ssh-host school-project-pc

SLA Capacity & Stage Breakdown Plateaus
.\scripts\run-benchmarks.ps1 run --suite sla_curve --ssh-host school-project-pc

Synchronized Burst Drain Test
.\scripts\run-benchmarks.ps1 run --suite burst --ssh-host school-project-pc


Deployment status on control-node:

kubectl get pods -n swcp -o wide