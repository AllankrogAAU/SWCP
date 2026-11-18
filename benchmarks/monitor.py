"""Real-time local submission rate and socket monitor for SWCP benchmarks."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


def count_tcp_sockets_to_target(target_ip: str, target_port: int) -> dict[str, int]:
    """Sample active TCP connections to the benchmark target IP:port."""
    counts = {"ESTABLISHED": 0, "TIME_WAIT": 0, "CLOSE_WAIT": 0, "OTHER": 0}
    try:
        if sys.platform == "win32":
            output = subprocess.check_output(
                ["netstat", "-n", "-p", "tcp"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            )
        else:
            output = subprocess.check_output(
                ["ss", "-nt"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
            )

        pattern = f"{target_ip}:{target_port}"
        for line in output.splitlines():
            if pattern in line:
                upper = line.upper()
                if "ESTABLISHED" in upper:
                    counts["ESTABLISHED"] += 1
                elif "TIME_WAIT" in upper:
                    counts["TIME_WAIT"] += 1
                elif "CLOSE_WAIT" in upper:
                    counts["CLOSE_WAIT"] += 1
                else:
                    counts["OTHER"] += 1
    except Exception:
        pass
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Live monitor for SWCP benchmark submission rates")
    parser.add_argument("--results-dir", default="benchmarks/results", help="Path to benchmarks/results directory")
    parser.add_argument("--target", default="100.111.90.116:80", help="Target host:port (default: 100.111.90.116:80)")
    parser.add_argument("--interval", type=float, default=1.0, help="Refresh interval in seconds (default: 1.0)")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    telemetry_file = results_dir / ".live_telemetry.json"

    target_str = args.target
    if "://" not in target_str:
        target_str = f"http://{target_str}"
    parsed = urlparse(target_str)
    target_host = parsed.hostname or "100.111.90.116"
    target_port = parsed.port or 80

    print(f"\033[2J\033[H", end="")
    print("Starting SWCP Live Benchmark Monitor...")
    print(f"Monitoring target: {target_host}:{target_port}")
    print(f"Watching telemetry at: {telemetry_file.resolve()}\n")

    while True:
        try:
            now = time.time()
            data = None
            if telemetry_file.exists():
                try:
                    with open(telemetry_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    pass

            socket_counts = count_tcp_sockets_to_target(target_host, target_port)

            print("\033[H", end="")

            header = "=" * 80
            print(header)
            print("                 SWCP LIVE BENCHMARK RATE & NETWORK MONITOR")
            print(header)

            if data and (now - data.get("timestamp", 0) < 15.0):
                elapsed_min = int(data.get("elapsed_s", 0) // 60)
                elapsed_sec = int(data.get("elapsed_s", 0) % 60)
                time_str = f"{elapsed_min:02d}:{elapsed_sec:02d}"

                step = data.get("step_label", "unknown")
                target_rpm = data.get("target_rpm", 0.0)
                sent_rpm = data.get("sent_rpm", 0.0)
                accepted_rpm = data.get("accepted_rpm", 0.0)
                completed_rpm = data.get("completed_rpm", 0.0)
                in_flight = data.get("in_flight", 0)
                free_users = data.get("available_users", 0)
                total_users = data.get("total_users", 0)
                submit_lat = data.get("submit_latency_median_ms", 0.0)
                total_dispatched = data.get("total_dispatched", 0)

                user_pct = (free_users / total_users * 100.0) if total_users else 0.0

                print(f"  Status:             ACTIVE BENCHMARK RUNNING")
                print(f"  Elapsed Time:       {time_str:<20} Current Step:   {step}")
                print(f"  Target Rate:        {target_rpm:>8.1f} subs/min")
                print(f"  Actual Sent Rate:   {sent_rpm:>8.1f} subs/min (from this machine)")
                print(f"  Server Accepted:    {accepted_rpm:>8.1f} subs/min (HTTP 202)")
                print(f"  Pipeline Completed: {completed_rpm:>8.1f} subs/min")
                print(f"  ----------------------------------------------------------------------------")
                print(f"  Active In-Flight:   {in_flight:>6} submissions in progress")
                print(f"  Virtual User Pool:  {free_users:>6} / {total_users} available ({user_pct:.1f}% free)")
                print(f"  HTTP Submit Latency:{submit_lat:>8.1f} ms (p50 roundtrip to 202 Accepted)")
                print(f"  Total Dispatched:   {total_dispatched:>8} submissions sent so far")
            else:
                print("  Status:             WAITING FOR ACTIVE BENCHMARK...")
                print(f"  (Checking {telemetry_file.name} — start a run with `python -m benchmarks.cli run ...`)")
                print("  " + "-" * 76)

            print(f"  Local TCP Sockets:  Established: {socket_counts['ESTABLISHED']} | Time-Wait: {socket_counts['TIME_WAIT']} | Close-Wait: {socket_counts['CLOSE_WAIT']}")
            print(header)
            print(f"  Updated at: {time.strftime('%H:%M:%S')} (Press Ctrl+C to stop)")

            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break
        except Exception:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
