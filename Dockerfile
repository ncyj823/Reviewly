# Reviewly Dockerfile
# Single image used by both the FastAPI webhook service and the RQ worker.
# Both services share the same codebase — only the startup command differs.

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from all services first (Docker layer caching —
# requirements change less often than code, so this layer gets cached)
COPY github_mcp/requirements.txt ./github_mcp/requirements.txt
COPY review_pipeline/requirements.txt ./review_pipeline/requirements.txt
COPY webhook_service/requirements.txt ./webhook_service/requirements.txt

# Install all dependencies
RUN pip install --no-cache-dir \
    -r github_mcp/requirements.txt \
    -r review_pipeline/requirements.txt \
    -r webhook_service/requirements.txt

# Copy all source code
COPY github_mcp/ ./github_mcp/
COPY review_pipeline/ ./review_pipeline/
COPY webhook_service/ ./webhook_service/
COPY start_worker.py ./

# Add all service directories to Python path
ENV PYTHONPATH=/app/github_mcp:/app/review_pipeline:/app/webhook_service

# Default command (overridden in docker-compose.yml per service)
CMD ["python", "-m", "uvicorn", "webhook_service.main:app", "--host", "0.0.0.0", "--port", "8000"]
