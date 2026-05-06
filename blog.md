# Building SwiftDeploy: A Policy-Driven Container Deployment CLI

## Overview

SwiftDeploy is a declarative CLI tool that takes a single `manifest.yaml` file and handles everything: generating Nginx configs, composing Docker services, running pre-flight policy checks with Open Policy Agent (OPA), and exposing live observability metrics in Prometheus format.

This post walks through the design of its Stage 4B features — OPA policy enforcement, Prometheus metrics, a live status dashboard, and audit reporting.

---

## The Problem

Deploying containerised services is easy. Deploying them *safely* — consistently, with guardrails against bad infrastructure state and poorly performing canary builds — is harder. Most teams rely on tribal knowledge or ad-hoc shell scripts. SwiftDeploy replaces that with a single source of truth: `manifest.yaml`.

---

## Architecture

```
manifest.yaml
     │
     ▼
swiftdeploy CLI
     ├─ init       → renders nginx.conf + docker-compose.yml (Jinja2)
     ├─ validate   → 5 pre-flight checks
     ├─ deploy     → OPA infra check → init → docker compose up
     ├─ promote    → OPA canary check → mode switch → restart app
     ├─ teardown   → docker compose down -v
     ├─ status     → live dashboard + history.jsonl append
     └─ audit      → audit_report.md from history.jsonl

Docker Compose Stack:
  ┌─────────┐    ┌───────────────┐    ┌─────┐
  │  nginx  │───▶│  app (Flask)  │    │ OPA │
  └─────────┘    └───────────────┘    └─────┘
      :8080           :3000              :8181
```

---

## Prometheus Metrics (`/metrics`)

The Flask app exposes a `/metrics` endpoint in native Prometheus text format — no `prometheus_client` library needed. Metrics are tracked in-memory using a thread-safe counter and a capped list of request durations.

### Tracking requests

```python
@app.before_request
def _before():
    g.start_time = time.time()

@app.after_request
def _after(response):
    if request.path == "/metrics":
        return response          # don't track scrapes
    duration = time.time() - g.start_time
    key = (request.method, request.path, str(response.status_code))
    with _metrics_lock:
        _request_counts[key] += 1
        _request_durations.append(duration)
    return response
```

### Metrics exposed

| Metric | Type | Labels |
|--------|------|--------|
| `http_requests_total` | counter | method, path, status_code |
| `http_request_duration_seconds` | histogram | le (bucket) |
| `app_uptime_seconds` | gauge | — |
| `app_mode` | gauge | mode (stable/canary) |
| `chaos_active` | gauge | — (0=none, 1=slow, 2=error) |

The histogram uses standard Prometheus buckets: `[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]`. This gives accurate P99 calculation without a full data stream.

---

## OPA Policy Enforcement

[Open Policy Agent](https://www.openpolicyagent.org/) evaluates declarative Rego policies before critical operations. SwiftDeploy runs OPA as a one-shot container (`docker run --rm`) — no persistent OPA process needed on the host.

### Infrastructure policy (pre-deploy)

`policies/infrastructure.rego`:
```rego
package infrastructure

deny contains msg if {
    input.disk_free_gb < 10
    msg := sprintf("Insufficient disk space: %.1f GB free (minimum 10 GB required)", [input.disk_free_gb])
}

deny contains msg if {
    input.cpu_load_1m > 2.0
    msg := sprintf("CPU load too high: %.2f (maximum 2.0 allowed)", [input.cpu_load_1m])
}
```

Before every `deploy`, the CLI collects host stats and sends them to OPA:
```
→  Running OPA infrastructure policy check...
→    disk_free: 45.3 GB   cpu_load_1m: 0.21
✔  Infrastructure policy passed
```

### Canary policy (pre-promote)

`policies/canary.rego`:
```rego
package canary

deny contains msg if {
    input.error_rate_pct > 1.0
    msg := sprintf("Error rate too high: %.2f%%", [input.error_rate_pct])
}

deny contains msg if {
    input.p99_ms > 500
    msg := sprintf("P99 latency too high: %.0fms", [input.p99_ms])
}
```

Before every `promote`, the CLI scrapes `/metrics`, calculates the error rate and P99 from the histogram buckets, and evaluates them against the canary policy. If the canary is degraded, promotion is blocked:
```
→  Running OPA canary policy check...
→    error_rate: 3.20%  p99: 120ms  total_requests: 500
✘  OPA denied: Error rate too high: 3.20% (maximum 1% allowed)
```

### OPA in the Docker Compose stack

An OPA service also runs as part of the deployed stack, loading policies from `./policies` and serving the REST API on port 8181. Application services can query it at runtime for fine-grained authorization:

```yaml
opa:
  image: openpolicyagent/opa:latest
  command: ["run", "--server", "--addr=0.0.0.0:8181", "/policies"]
  volumes:
    - ./policies:/policies:ro
  ports:
    - "8181:8181"
```

---

## Status Dashboard

`./swiftdeploy status` prints a live snapshot of the running stack and appends a structured record to `history.jsonl`:

```
  ────────────────────────────────────────────────────
               SwiftDeploy Status Dashboard
  ────────────────────────────────────────────────────
  Timestamp                2025-05-04T14:32:01Z
  Nginx port               8080
  App mode                 canary
  App uptime               312s
  Stack healthy            yes
  Total requests           1042
  Error rate               0.00%
  P99 latency              18ms
  Chaos active             none
  ────────────────────────────────────────────────────
  Containers:
    ✔  swiftdeploy-app-1       Up 5 minutes
    ✔  swiftdeploy-nginx-1     Up 5 minutes
    ✔  swiftdeploy-opa-1       Up 5 minutes
  ────────────────────────────────────────────────────
```

Each snapshot in `history.jsonl` looks like:
```json
{"timestamp": "2025-05-04T14:32:01Z", "mode": "canary", "uptime_seconds": 312, "total_requests": 1042, "error_rate_pct": 0.0, "p99_ms": 18.0, "chaos": "none", "healthy": true}
```

---

## Audit Report

`./swiftdeploy audit` reads `history.jsonl` and generates `audit_report.md` — a markdown report with availability stats, a full snapshot table, and a log of any chaos injection events:

```
✔  audit_report.md generated (12 snapshots, 2 chaos events)
```

This gives ops teams a paper trail of every status check: when the stack was healthy, what mode it was in, and whether chaos was active.

---

## Key Design Decisions

**No external Prometheus server required.** The `/metrics` endpoint is plain text — any monitoring system that can scrape HTTP can consume it. The CLI itself parses it for OPA inputs.

**OPA as one-shot, not daemon.** For CLI-level enforcement, running `docker run --rm openpolicyagent/opa:latest eval ...` avoids the need to maintain a separate OPA process. Policy evaluation is synchronous, deterministic, and requires no persistent state.

**Single source of truth.** All configuration — image names, ports, network names, OPA settings — lives in `manifest.yaml`. Nginx configs and docker-compose files are generated from it, never edited manually.

**Chaos injection for canary validation.** The `/chaos` endpoint (canary mode only) lets you simulate slow responses or error bursts. This intentionally degrades the canary's metrics, which then triggers OPA denial on the next `promote stable` — demonstrating that the policy gates actually work.

---

## Running It

```bash
# Build the app image
docker build -t swift-deploy-1-node:latest .

# Deploy (runs OPA infra check first)
./swiftdeploy deploy

# Inject some chaos in canary mode
curl -X POST http://localhost:8080/chaos \
  -H "Content-Type: application/json" \
  -d '{"mode": "error", "rate": 0.5}'

# Try to promote — OPA will block it if error rate > 1%
./swiftdeploy promote stable

# Check live status and record a snapshot
./swiftdeploy status

# Generate audit report
./swiftdeploy audit
```

---

## Conclusion

SwiftDeploy shows that you don't need a Kubernetes cluster or a commercial platform to get policy-driven, observable deployments. A 300-line Python CLI, a couple of Rego files, and Docker Compose are enough to enforce infrastructure guardrails, track SLO metrics, and block bad promotions — all from a single `manifest.yaml`.
