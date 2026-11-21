import pandas as pd
df = pd.read_csv('benchmarks/results/2026-11-18_17-56-09_infinite_gpu_46cd83/submissions.csv')
comp = df[df['final_status'] == 'COMPLETED']
print("=== DELIVERY OVERHEAD BY TERMINAL_VIA ===")
for via, group in comp.groupby('terminal_via'):
    print(f"{via:12s}: count={len(group):5d}, mean_delivery={group['delivery_overhead_ms'].mean():.1f}ms, p95_delivery={group['delivery_overhead_ms'].quantile(0.95):.1f}ms")
