import pandas as pd
df = pd.read_csv('benchmarks/results/2026-11-18_17-56-09_infinite_gpu_46cd83/submissions.csv')
print(df['sandbox_queue_wait_ms'].describe())
print(df[df['sandbox_queue_wait_ms'] > 0][['step_label', 'sandbox_queue_wait_ms', 'orchestration_overhead_ms', 'pipeline_total_ms']].head(10))
