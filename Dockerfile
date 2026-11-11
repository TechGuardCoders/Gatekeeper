# syntax=docker/dockerfile:1
# Multi-stage, multi-arch (amd64 + arm64):
#   docker buildx build --platform linux/amd64,linux/arm64 -t gatekeeper .
# Build (local):  docker build -t gatekeeper .
# Run:            docker run --rm -v $PWD:/data gatekeeper /data/baseline.json /data/candidate.json
FROM python:3.12-slim AS builder
WORKDIR /app
COPY . .

FROM python:3.12-slim AS runtime
# Non-root user
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app
USER appuser
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "gatekeeper.py"]
CMD ["--help"]
