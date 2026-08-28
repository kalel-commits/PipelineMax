FROM python:3.11-slim

WORKDIR /app

# Install deps first so the layer caches across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render (and most PaaS) inject $PORT and expect the process to bind to it.
# Fall back to 8000 for local `docker run` with no PORT set.
ENV PORT=8000
EXPOSE 8000

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

CMD ["sh", "-c", "exec uvicorn guardrail_main:app --host 0.0.0.0 --port ${PORT:-8000}"]
