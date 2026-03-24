FROM python:3.12-slim

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy application files
COPY server.py .
COPY public/ public/

# Create directories for streams and config
RUN mkdir -p streams config

# Config volume — mount to persist camera configuration
VOLUME /app/config

EXPOSE 8090

ENV PORT=8090
ENV CONFIG_PATH=/app/config/cameras.json

CMD ["python", "server.py"]
