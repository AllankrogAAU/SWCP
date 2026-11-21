import pandas as pd

df = pd.read_csv('benchmarks/results/2026-11-18_16-53-18_infinite_gpu_206621/submissions.csv')

print("Overall sandbox_node value counts:")
print(df['sandbox_node'].value_counts(dropna=False))

print("\nBy step:")
for step in df['step_label'].unique():
    sdf = df[df['step_label'] == step]
    nodes = sdf['sandbox_node'].value_counts(dropna=False).to_dict()
    print(f"{step:18s}: {nodes}")
