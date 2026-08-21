FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app /app/app
COPY web /app/web
RUN mkdir -p /app/data

ENV MEDIA_APP_DATA_DIR=/app/data
EXPOSE 18080

CMD ["python3", "app/server.py", "--host", "0.0.0.0", "--port", "18080"]

