import pandas as pd
df = pd.read_csv('benchmarks/results/2026-11-18_17-56-09_infinite_gpu_46cd83/summary.csv')
cols = [
    'step_label', 'target_load', 'achieved_throughput_per_min',
    'mean_enqueue_ms', 'p95_sandbox_wait_ms', 'p95_sandbox_exec_ms',
    'p95_llm_wait_ms', 'p95_llm_exec_ms',
    'p95_orchestration_overhead_ms', 'p95_delivery_overhead_ms',
    'p50_e2e_ms', 'p95_e2e_ms'
]
print(df[cols].to_string())
