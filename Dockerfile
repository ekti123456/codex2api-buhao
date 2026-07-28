FROM python:3.12-alpine

RUN addgroup -S app && adduser -S -G app app
WORKDIR /app
COPY server.py ./server.py
COPY web ./web
RUN mkdir -p /app/data && chown -R app:app /app

USER app
ENV POOL_MANAGER_HOST=0.0.0.0 \
    POOL_MANAGER_PORT=8790 \
    POOL_MANAGER_SETTINGS_FILE=/app/data/settings.json \
    POOL_MANAGER_AUDIT_FILE=/app/data/audit.jsonl
EXPOSE 8790
VOLUME ["/app/data"]
CMD ["python", "server.py"]
