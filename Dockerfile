# Dockerfile - Playwright + Python (suitable for Render)
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Expose port (Render uses PORT env var)
ENV PORT 10000
# When running on Render, set gunicorn to bind to $PORT.
CMD ["bash","-lc","exec gunicorn --bind 0.0.0.0:${PORT} app:app --workers 2 --timeout 300"]
