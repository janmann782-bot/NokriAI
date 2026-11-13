FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY nokri ./nokri
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home nokri
COPY config ./config
COPY assets ./assets
RUN mkdir -p /app/state && chown -R nokri:nokri /app/state
USER nokri
CMD ["nokri", "run"]
