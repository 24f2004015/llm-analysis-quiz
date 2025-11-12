# Dockerfile - robust Playwright + Python image for Render
FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

# Copy requirements and install Python dependencies (do NOT include playwright here ideally,
# but we will explicitly install it below to ensure it's present).
COPY requirements.txt .

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Explicitly install Playwright Python package (ensures the module is present)
RUN pip install --no-cache-dir playwright

# Install browser binaries for Playwright (with deps)
RUN python -m playwright install --with-deps chromium

# Copy application code
COPY . .

# Ensure logs dir exists
RUN mkdir -p /app/logs

# Port (Render will set PORT env var when running)
ENV PORT 10000

# Run the app with gunicorn; keep timeout high for solver tasks
CMD ["bash","-lc","exec gunicorn --bind 0.0.0.0:${PORT} app:app --workers 2 --timeout 300"]
