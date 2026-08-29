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

RUN useradd --create-home --uid 1000 appuser && chown -R appuser /app
USER appuser

# The webhook server IS the production entrypoint. `python -m pipelineai webhook`
# binds uvicorn to 0.0.0.0:$PORT and prints a startup summary.
CMD ["sh", "-c", "exec python -m pipelineai webhook --host 0.0.0.0 --port ${PORT:-8000}"]
