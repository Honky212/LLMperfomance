# LLM Perf Panel - Single container deployment (V2.2 §4.9)
# Build:  docker build -t llmperf-panel -f web/Dockerfile .
# Run:    docker run -p 8686:8686 -v ${PWD}/results:/app/results llmperf-panel
# With auth: docker run -p 8686:8686 -e WEB_TOKEN=mysecret -v ${PWD}/results:/app/results llmperf-panel

FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

# Copy application code
COPY scripts/ scripts/
COPY web/ web/
COPY corpus/ corpus/

# Create data directory for SQLite + secret key
RUN mkdir -p web/data results

# Default environment
ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8686
ENV PYTHONUNBUFFERED=1

EXPOSE 8686

CMD ["python", "web/server.py"]
