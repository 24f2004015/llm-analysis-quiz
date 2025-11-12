# app.py
import os
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify

from solver import QuizSolver

# Setup logging
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "server.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger().addHandler(console)

# Load secrets (email -> secret)
# Priority:
# 1) Environment variable SECRETS_JSON (stringified JSON)
# 2) Local secrets.json file (development)
SECRETS = {}
SECRETS_ENV = os.getenv("SECRETS_JSON")
SECRETS_FILE = Path("secrets.json")

if SECRETS_ENV:
    try:
        SECRETS = json.loads(SECRETS_ENV)
        logging.info("Loaded secrets from SECRETS_JSON environment variable.")
    except Exception:
        logging.exception("Failed to parse SECRETS_JSON environment variable; falling back to file if present.")
        SECRETS = {}
elif SECRETS_FILE.exists():
    try:
        with SECRETS_FILE.open() as f:
            SECRETS = json.load(f)
        logging.info("Loaded secrets from local secrets.json (local development).")
    except Exception:
        logging.exception("Failed to load secrets.json; starting with empty secrets.")
        SECRETS = {}
else:
    logging.warning("No secrets provided (SECRETS_JSON env var not set and secrets.json not found). Starting with empty secrets.")
    SECRETS = {}

app = Flask(__name__)
solver = QuizSolver(log_dir=LOG_DIR)


@app.route("/", methods=["GET"])
def index():
    """
    Basic info endpoint — handy to open in a browser to confirm service is live.
    """
    info = {
        "service": "LLM Analysis Quiz Solver",
        "status": "ok",
        "time": datetime.utcnow().isoformat() + "Z",
        "secrets_loaded": len(SECRETS),
        "endpoints": ["/api/solve (POST)", "/health (GET)"],
    }
    return jsonify(info), 200


@app.route("/health", methods=["GET"])
def health():
    """
    Lightweight health check for uptime monitors (should return very quickly).
    """
    return "ok", 200


@app.route("/api/solve", methods=["POST"])
def api_solve():
    start_ts = datetime.utcnow()
    req_id = int(time.time() * 1000)
    try:
        payload = request.get_json(force=True)
    except Exception:
        logging.exception("Invalid JSON received")
        return jsonify({"error": "invalid json"}), 400

    # required keys: email, secret, url
    email = payload.get("email")
    secret = payload.get("secret")
    url = payload.get("url")

    logging.info(f"[{req_id}] Received task for email={email} url={url}")

    if not (email and secret and url):
        logging.warning(f"[{req_id}] Missing required fields")
        return jsonify({"error": "missing required fields (email, secret, url)"}), 400

    expected = SECRETS.get(email)
    if expected is None or secret != expected:
        # do not log the secret values; only log the fact that validation failed
        logging.warning(f"[{req_id}] Invalid secret for {email}")
        return jsonify({"error": "invalid secret"}), 403

    # At this point: secret valid -> return HTTP 200 as required by spec
    resp = {"status": "accepted", "message": "task accepted; processing started"}
    logging.info(f"[{req_id}] Secret validated for {email}. Starting background worker.")

    # Launch background worker to handle within 3 minutes
    thread = threading.Thread(
        target=_background_process, args=(req_id, payload), daemon=True
    )
    thread.start()

    return jsonify(resp), 200


def _background_process(req_id, payload):
    try:
        # solver.run() will attempt to visit and submit within the time budget
        result = solver.run(payload)
        logging.info(f"[{req_id}] Solver finished: {result}")
    except Exception:
        logging.exception(f"[{req_id}] Solver crashed")


if __name__ == "__main__":
    # Read PORT from env so platform (Render/Cloud Run) can control it; default to 8000 locally.
    port = int(os.getenv("PORT", "8000"))
    logging.info(f"Starting LLM Analysis Quiz endpoint on http://0.0.0.0:{port} (secrets_loaded={len(SECRETS)})")
    # For local dev we use Flask's server. In production we expect gunicorn to be used.
    app.run(host="0.0.0.0", port=port)
