FROM python:3.12-slim

# Install static ffmpeg with OpenSSL (required for RTSPS / tls_verify support)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates wget xz-utils \
        libva2 libva-drm2 mesa-va-drivers nginx && \
    wget -qO /tmp/ffmpeg.tar.xz "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" && \
    tar xf /tmp/ffmpeg.tar.xz --strip-components=2 -C /usr/local/bin --wildcards '*/bin/ffmpeg' '*/bin/ffprobe' && \
    rm /tmp/ffmpeg.tar.xz && \
    apt-get purge -y wget xz-utils && \
    apt-get autoremove -y && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy application files
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
COPY server.py .
COPY nginx.conf /etc/nginx/sites-enabled/default
COPY public/ public/

# Create directories for streams and config
RUN mkdir -p streams config

# Config volume — mount to persist camera configuration
VOLUME /app/config

EXPOSE 8090

ENV API_PORT=8091
ENV CONFIG_PATH=/app/config/cameras.json
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "server.py"]
