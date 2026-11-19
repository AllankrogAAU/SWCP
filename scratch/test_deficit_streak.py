import pandas as pd

for p, name in [
    ('benchmarks/results/2026-11-18_14-43-52_ramp_capacity_7dc5e6/summary.csv', 'DUAL_GPU 7dc5e6'),
    ('benchmarks/results/2026-11-18_16-53-18_infinite_gpu_206621/summary.csv', 'INFINITE_GPU 206621'),
]:
    df = pd.read_csv(p)
    print(f"\n=== {name} ===")
    streak = 0
    for idx, r in df.iterrows():
        mid = r['target_load']
        ach = r['achieved_throughput_per_min']
        inf = r['in_flight_end']
        behind = ach < 0.88 * mid
        # Condition: either behind and inf >= floor, or sustained deficit where max(inf, prev_inf) >= floor
        # Or simply: behind is sustained for 3 buckets, with at least one high backlog
        if behind:
            streak += 1
        else:
            streak = 0
        print(f"{r['step_label']:18s}: target={mid:4.0f}, ach={ach:4.0f}, inf={inf:4.0f}, behind={behind!s:5s}, streak={streak}")
