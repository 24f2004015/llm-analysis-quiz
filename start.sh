#!/bin/bash
set -e
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browsers
python -m playwright install chromium

# Run app (development)
# For production use gunicorn: e.g. gunicorn -w 2 -b 0.0.0.0:8000 app:app
exec gunicorn -w 2 -b 0.0.0.0:8000 app:app
