import pandas as pd
import numpy as np

df = pd.read_csv('benchmarks/results/2026-11-18_16-53-18_infinite_gpu_206621/submissions.csv')
for b in df['step_label'].unique():
    sdf = df[df['step_label'] == b]
    print(f"{b:18s}: count={len(sdf):4d}")
