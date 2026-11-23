import pandas as pd
df = pd.read_csv('benchmarks/results/2026-11-18_17-56-09_infinite_gpu_46cd83/summary.csv')
cols = [c for c in df.columns if 'replicas' in c]
print(df[['step_label'] + cols].to_string())
