FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs keys

# Fix: Run as non-root user for defense-in-depth security
RUN useradd -m -r workflow_api_user && \
    chown -R workflow_api_user:workflow_api_user /app

USER workflow_api_user

EXPOSE 8000

CMD ["python", "main.py"]
