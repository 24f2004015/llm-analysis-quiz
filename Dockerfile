# Dockerfile - Playwright + Python production image
# Uses the official Playwright image which already includes Playwright + browser binaries.
FROM mcr.microsoft.com/playwright/python:latest

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies (do NOT include playwright in requirements.txt)
COPY requirements.txt .

# Install Python deps. We don't reinstall Playwright here to avoid version/binary mismatches.
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port (Render/Cloud Run will set PORT env var at runtime)
ENV PORT 10000

# Ensure any temp or cache dirs exist (optional)
RUN mkdir -p /app/logs

# Run with gunicorn in production; keep timeout high for long solver tasks
# Using bash -lc lets us expand ${PORT} at runtime.
CMD ["bash","-lc","exec gunicorn --bind 0.0.0.0:${PORT} app:app --workers 2 --timeout 300"]
