import pandas as pd
df = pd.read_csv('benchmarks/results/2026-11-18_17-56-09_infinite_gpu_46cd83/summary.csv')
print("=== POD DISTRIBUTION ===")
for _, r in df.iterrows():
    tot = int(r['max_replicas_sandbox'])
    w = int(r['max_replicas_sandbox_worker'])
    c = tot - w
    print(f"{r['step_label']:18s}: Total={tot:2d}, Ctrl={c:2d}, Worker={w:2d}")
