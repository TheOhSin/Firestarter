FROM python:3.13-slim

ARG APP_VERSION=dev

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_VERSION=${APP_VERSION}

LABEL org.opencontainers.image.title="Firestarter"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.description="Discord onboarding and account management for Fireshare"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/
COPY VERSION /app/VERSION

RUN useradd \
      --create-home \
      --uid 1000 \
      firestarter \
    && mkdir -p /data \
    && chown -R firestarter:firestarter /data /app

USER firestarter

HEALTHCHECK \
  --interval=30s \
  --timeout=5s \
  --start-period=30s \
  --retries=3 \
  CMD ["python", "/app/healthcheck.py"]

CMD ["python", "/app/bootstrap.py"]
