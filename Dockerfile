FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY reviewer.py .

# GitHub Actions passes inputs as env vars and mounts the event payload
# at GITHUB_EVENT_PATH automatically — no CMD args needed.
ENTRYPOINT ["python", "/app/reviewer.py"]
