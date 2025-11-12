# Dockerfile - Playwright + Python (recommended: use latest Playwright image)
FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Expose port (Render uses PORT env var)
ENV PORT 10000

# Run with gunicorn; keep timeout high for long solver runs
CMD ["bash","-lc","exec gunicorn --bind 0.0.0.0:${PORT} app:app --workers 2 --timeout 300"]
