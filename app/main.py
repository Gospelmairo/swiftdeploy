import os
import time
import random
import threading
from flask import Flask, jsonify, request, g

app = Flask(__name__)

MODE = os.getenv("MODE", "stable")
VERSION = os.getenv("APP_VERSION", "1.0.0")
PORT = int(os.getenv("APP_PORT", 3000))
START_TIME = time.time()

# Chaos state
_chaos_lock = threading.Lock()
_chaos = {"mode": None, "duration": 0, "rate": 0.0}


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


@app.route("/")
def index():
    chaos_resp = apply_chaos()
    if chaos_resp:
        return chaos_resp
    return make_response({
        "message": f"Welcome to SwiftDeploy API",
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
