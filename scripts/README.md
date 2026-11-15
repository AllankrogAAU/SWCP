# Intro C assignments and sample-submission generator

The core API seeds 25 numbered Intro C assignments in Development, or when
`SeedSampleAssignments` is enabled. The local Kubernetes config enables this.
Restart the API to pick up seed changes:

```powershell
kubectl rollout restart deployment/core-api -n swcp
kubectl rollout status deployment/core-api -n swcp
```

## Restart or redeploy the local WSL cluster

From the repository root in PowerShell, rerun the deploy script to rebuild and
import the current service images and re-apply the Kubernetes manifests:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy-local-k8s-wsl.ps1 -Distro Ubuntu
```

The script is intended to be rerun and does not delete the cluster or its
database. It restarts c-sandbox, but does not automatically restart core-api
just because its local image was rebuilt. After the deploy script succeeds,
restart core-api so its new image runs and the updated assignment seed takes
effect:

```powershell
wsl.exe -d Ubuntu -u root -- env KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl rollout restart deployment/core-api -n swcp
wsl.exe -d Ubuntu -u root -- env KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl rollout status deployment/core-api -n swcp --timeout=180s
wsl.exe -d Ubuntu -u root -- env KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl get pods -n swcp
```

This restarts an application deployment, not the k3s cluster. Avoid
`wsl.exe --shutdown` or restarting k3s for a routine app redeploy. Docker
Desktop can expose `/Docker/host` to systemd and k3s with mount options
containing `C:\Program Files`; kubelet rejects that mount table and k3s can
crash-loop. If the k3s service itself is stopped, first check whether that
mount is present in systemd's mount namespace and unmount it there before
restarting k3s:

```powershell
wsl.exe -d Ubuntu -u root -- nsenter -t 1 -m -- mountpoint -q /Docker/host
if ($LASTEXITCODE -eq 0) { wsl.exe -d Ubuntu -u root -- nsenter -t 1 -m -- umount /Docker/host }
wsl.exe -d Ubuntu -u root -- systemctl restart k3s
wsl.exe -d Ubuntu -u root -- env KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl wait --for=condition=Ready node --all --timeout=180s
```

Three assignments ask students to write a complete program with `main` and do
not use a function harness:

- **01. Hello, kursus** — print a greeting.
- **03. Seconds to h/m/s** — read input and print the converted time.
- **09. Star triangle** — read a row count and print the triangle.

The other 22 assignments focus on implementing a particular function and use a
test harness. The harness supplies `main`, reads test input, calls the
student's function, and prints its result.

## Generate submissions

Preview the default plan without contacting the API or running code:

```powershell
python .\scripts\generate-sample-submissions.py --dry-run
```

To test execution **without LLM feedback**, use `--action run`. This sends each
example through the sandbox, then completes the submission without sending it
to the LLM worker. A count of 25 runs one example per assignment:

```powershell
python .\scripts\generate-sample-submissions.py --action run --count 25 --students 25 --concurrency 5
```

Run stdout, stderr, and exit codes are printed in the console and saved with
each `run` submission's sandbox output in the database. The usual category
mix still applies, so this run includes intentional infinite loops, memory
leaks, and pointer errors; those exercise the sandbox's time and isolation
limits. Choose a smaller `--count` for a quick smoke test.

To send examples through the sandbox **and** the LLM feedback pipeline:

```powershell
python .\scripts\generate-sample-submissions.py --action submit --count 300 --students 25 --concurrency 5 --backend local
```

`submit` is the default action if `--action` is omitted. At the default count,
the plan sends **300 submissions**: 12 per assignment, split into:

| Category | Count | Share |
|---|---:|---:|
| Correct examples | 90 | 30% |
| Common/random mistakes | 150 | 50% |
| Infinite loops | 30 | 10% |
| Memory leaks | 15 | 5% |
| Pointer errors | 15 | 5% |

The examples are reproducible by default (`--seed 20261115`). For counts not
divisible by 20, the script rounds category sizes to the closest whole-number
distribution. Assignments are always distributed as evenly as possible.

The script creates or reuses student accounts named `swcp-load01`,
`swcp-load02`, and so on, using the fixed local-test password `test-123`.
These predictable credentials are only for local testing, not production. If
you already created an account with the default prefix using a different
password, choose a new `--user-prefix` or remove those test accounts before
running the script.

Each account submits one item at a time and waits for it to finish before
submitting the next, avoiding the per-student active-submission limit. Other
accounts continue independently: when any one finishes, that account queues
its next example without waiting for the rest. The script prints a `queued`
line as soon as the API accepts a job and a `finished` line when it completes,
so a slow loop should not look like a batch-wide pause.
`--students` controls how many student accounts are used, while
`--concurrency` controls how many accounts submit simultaneously. Accounts
and submissions remain in the database; a later run reuses accounts and adds
more submissions.

Options:

| Option | Default | Description |
|---|---|---|
| `--api-url URL` | `http://swcp.local` | API base URL; `SWCP_API_URL` can also set it. |
| `--count N` | `300` | Total number of submissions. |
| `--students N` | `25` | Size of the reusable student account pool. |
| `--concurrency N` | `5` | Maximum number of students submitting at once. |
| `--action run\|submit` | `submit` | `run` executes in the sandbox without LLM feedback; `submit` also requests LLM feedback. |
| `--user-prefix TEXT` | `swcp-load` | Prefix for generated student usernames. |
| `--backend local\|azure` | `local` | Feedback backend for `submit` actions. Azure may incur model usage costs. |
| `--poll-seconds N` | `2` | Delay between submission status checks. |
| `--timeout-seconds N` | `1200` | Per-submission completion timeout. |
| `--seed N` | `20261115` | Random seed for reproducible category assignment. |
| `--dry-run` | off | Print the plan without calling the API or creating accounts. |
| `--yes` | off | Skip the confirmation before live submissions. |

Each submission runs in its own sandbox pod. Infinite-loop examples are
terminated by the sandbox's 2-second run timeout; they occupy one worker slot
while running, but do not block other students from submitting. The local
deployment has 3 c-sandbox worker replicas, each configured for up to 8
concurrent jobs, for a theoretical cluster total of 24. The generator waits
for each student's current submission to finish before sending the next, so
`--concurrency 5` allows at most five of those slots to be used by this script.
You can raise it, for example to 12, to use more of the worker capacity:

```powershell
python .\scripts\generate-sample-submissions.py --action run --count 25 --students 25 --concurrency 12
```

Higher concurrency can still increase CPU, memory, and pod-startup contention
on the single local WSL node; it doesn't make the node itself faster.

The intentional fault examples exercise the configured sandbox limits. Infinite
loops are time-limited by the sandbox; pointer-error programs may terminate
abnormally. Memory-leak examples allocate memory and intentionally do not free
it. These examples test sandbox and feedback behavior, not a particular
compiler or static analyzer's ability to detect every defect.
