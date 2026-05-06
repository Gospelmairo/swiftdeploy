import os
import time
import random
import threading
from collections import defaultdict
from flask import Flask, jsonify, request, g

app = Flask(__name__)

MODE = os.getenv("MODE", "stable")
VERSION = os.getenv("APP_VERSION", "1.0.0")
PORT = int(os.getenv("APP_PORT", 3000))
START_TIME = time.time()

# Chaos state
_chaos_lock = threading.Lock()
_chaos = {"mode": None, "duration": 0, "rate": 0.0}

# Metrics state
_metrics_lock = threading.Lock()
_request_counts = defaultdict(int)  # (method, path, status_code) -> count
_request_durations = []             # list of float seconds (capped at 10 000)

HISTOGRAM_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
_MAX_DURATIONS = 10_000


def apply_chaos():
    with _chaos_lock:
        mode = _chaos["mode"]
        if mode == "slow":
            time.sleep(_chaos["duration"])
        elif mode == "error":
            if random.random() < _chaos["rate"]:
                return jsonify({"error": "chaos error injection"}), 500
    return None


def make_response(data, status=200):
    resp = jsonify(data)
    resp.status_code = status
    if MODE == "canary":
        resp.headers["X-Mode"] = "canary"
    return resp


@app.before_request
def _before():
    g.start_time = time.time()


@app.after_request
def _after(response):
    if request.path == "/metrics":
        return response
    duration = time.time() - g.start_time
    key = (request.method, request.path, str(response.status_code))
    with _metrics_lock:
        _request_counts[key] += 1
        _request_durations.append(duration)
        if len(_request_durations) > _MAX_DURATIONS:
            del _request_durations[:-_MAX_DURATIONS]
    return response


@app.route("/")
def index():
    chaos_resp = apply_chaos()
    if chaos_resp:
        return chaos_resp
    return make_response({
        "message": "Welcome to SwiftDeploy API",
        "mode": MODE,
        "version": VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


@app.route("/healthz")
def healthz():
    return make_response({
        "status": "ok",
        "uptime_seconds": round(time.time() - START_TIME, 2),
    })


@app.route("/metrics")
def metrics():
    with _metrics_lock:
        counts = dict(_request_counts)
        durations = list(_request_durations)

    lines = []

    # http_requests_total
    lines += [
        "# HELP http_requests_total Total HTTP requests by method, path, and status",
        "# TYPE http_requests_total counter",
    ]
    for (method, path, status), count in sorted(counts.items()):
        lines.append(
            f'http_requests_total{{method="{method}",path="{path}",status_code="{status}"}} {count}'
        )

    # http_request_duration_seconds histogram
    lines += [
        "# HELP http_request_duration_seconds HTTP request latency in seconds",
        "# TYPE http_request_duration_seconds histogram",
    ]
    n = len(durations)
    total_sum = sum(durations)
    for b in HISTOGRAM_BUCKETS:
        bucket_count = sum(1 for d in durations if d <= b)
        lines.append(f'http_request_duration_seconds_bucket{{le="{b}"}} {bucket_count}')
    lines.append(f'http_request_duration_seconds_bucket{{le="+Inf"}} {n}')
    lines.append(f'http_request_duration_seconds_sum {total_sum:.6f}')
    lines.append(f'http_request_duration_seconds_count {n}')

    # app_uptime_seconds
    lines += [
        "# HELP app_uptime_seconds Seconds since application start",
        "# TYPE app_uptime_seconds gauge",
        f"app_uptime_seconds {time.time() - START_TIME:.2f}",
    ]

    # app_mode
    mode_val = 1 if MODE == "canary" else 0
    lines += [
        "# HELP app_mode Current mode: 0=stable 1=canary",
        "# TYPE app_mode gauge",
        f'app_mode{{mode="{MODE}"}} {mode_val}',
    ]

    # chaos_active
    with _chaos_lock:
        chaos_mode = _chaos["mode"]
    chaos_val = {"slow": 1, "error": 2}.get(chaos_mode, 0)
    lines += [
        "# HELP chaos_active Chaos injection state: 0=none 1=slow 2=error",
        "# TYPE chaos_active gauge",
        f"chaos_active {chaos_val}",
    ]

    return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; version=0.0.4"}


@app.route("/chaos", methods=["POST"])
def chaos():
    if MODE != "canary":
        return make_response({"error": "chaos endpoint only available in canary mode"}, 403)

    body = request.get_json(silent=True) or {}
    chaos_mode = body.get("mode")

    with _chaos_lock:
        if chaos_mode == "slow":
            _chaos["mode"] = "slow"
            _chaos["duration"] = int(body.get("duration", 2))
            return make_response({"message": f"chaos mode: slow for {_chaos['duration']}s"})
        elif chaos_mode == "error":
            _chaos["mode"] = "error"
            _chaos["rate"] = float(body.get("rate", 0.5))
            return make_response({"message": f"chaos mode: error at rate {_chaos['rate']}"})
        elif chaos_mode == "recover":
            _chaos["mode"] = None
            _chaos["duration"] = 0
            _chaos["rate"] = 0.0
            return make_response({"message": "chaos cancelled — service recovered"})
        else:
            return make_response({"error": "unknown chaos mode"}, 400)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
