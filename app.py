# app.py
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
SECRETS_FILE = Path("secrets.json")
if not SECRETS_FILE.exists():
    logging.warning("secrets.json not found. Create it with mappings email->secret.")
    SECRETS = {}
else:
    with SECRETS_FILE.open() as f:
        SECRETS = json.load(f)

app = Flask(__name__)
solver = QuizSolver(log_dir=LOG_DIR)


@app.route("/api/solve", methods=["POST"])
def api_solve():
    start_ts = datetime.utcnow()
    req_id = int(time.time() * 1000)
    try:
        payload = request.get_json(force=True)
    except Exception as e:
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
    logging.info("Starting LLM Analysis Quiz endpoint on http://0.0.0.0:8000")
    app.run(host="0.0.0.0", port=8000)
