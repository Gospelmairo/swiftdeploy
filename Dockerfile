# syntax=docker/dockerfile:1
FROM python:3.11-alpine AS builder
WORKDIR /build
RUN pip install --no-cache-dir --upgrade pip
COPY app/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-alpine AS runtime
WORKDIR /app

RUN addgroup -S appgroup && adduser -S -G appgroup appuser

RUN pip install --no-cache-dir --upgrade pip

COPY --from=builder /install /usr/local
COPY app/main.py .

USER appuser

ENV MODE=stable
ENV APP_VERSION=1.0.0
ENV APP_PORT=3000

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:3000/healthz || exit 1

CMD ["python", "main.py"]
