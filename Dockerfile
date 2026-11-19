# Server image. Run with docker compose (see docker-compose.yml) or:
#   docker run -p 8645:8645 -v permit-data:/data --env-file .env permit-alerts
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PERMIT_ALERTS_DATA_DIR=/data \
    PERMIT_ALERTS_HOST=0.0.0.0
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]"
VOLUME /data
EXPOSE 8645
CMD ["permit-alerts", "run", "--no-browser"]
