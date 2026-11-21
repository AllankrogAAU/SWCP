import pandas as pd
df = pd.read_csv('benchmarks/results/2026-11-18_17-56-09_infinite_gpu_46cd83/submissions.csv')
r14 = df[df['step_label'] == 'ramp_14_2125rpm']
missing = r14[r14['sandbox_queue_wait_ms'] == 0]
present = r14[r14['sandbox_queue_wait_ms'] > 0]
print(f"Missing wait count: {len(missing)}, mean orch: {missing['orchestration_overhead_ms'].mean():.1f}ms, p95 orch: {missing['orchestration_overhead_ms'].quantile(0.95):.1f}ms")
print(f"Present wait count: {len(present)}, mean orch: {present['orchestration_overhead_ms'].mean():.1f}ms, p95 orch: {present['orchestration_overhead_ms'].quantile(0.95):.1f}ms")
print(f"Present mean sandbox_queue_wait_ms: {present['sandbox_queue_wait_ms'].mean():.1f}ms, p95: {present['sandbox_queue_wait_ms'].quantile(0.95):.1f}ms")
