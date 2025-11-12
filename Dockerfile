# Dockerfile - Playwright image with Python
FROM mcr.microsoft.com/playwright/python:latest

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT 8080
CMD exec gunicorn --bind 0.0.0.0:$PORT app:app --workers 2 --timeout 300
