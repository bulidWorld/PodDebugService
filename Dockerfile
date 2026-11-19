FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY poddebug ./poddebug
COPY api ./api
COPY models ./models
RUN pip install --no-cache-dir .
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["pod-debug-service"]
