import pandas as pd
df = pd.read_csv('benchmarks/results/2026-11-18_17-56-09_infinite_gpu_46cd83/submissions.csv')
comp = df[df['final_status'] == 'COMPLETED']
for step, group in comp.groupby('step_label', sort=False):
    missing = (group['sandbox_queue_wait_ms'] == 0.0).sum()
    total = len(group)
    pct = (missing / total) * 100
    mean_orch = group['orchestration_overhead_ms'].mean()
    print(f"{step:18s}: total={total:4d}, missing_wait={missing:4d} ({pct:5.1f}%), mean_orch={mean_orch:6.1f}ms")
