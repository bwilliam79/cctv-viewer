FROM python:3.12-slim

# Install static ffmpeg with OpenSSL (required for RTSPS / tls_verify support).
# BtbN publishes arch-specific builds; linux64 is amd64-only and breaks under
# Colima aarch64 (qemu-x86_64 missing ld-linux → black camera tiles).
# Detect arch inside the build (classic builder often leaves TARGETARCH empty).
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates wget xz-utils \
        libva2 libva-drm2 mesa-va-drivers nginx && \
    ARCH="$(dpkg --print-architecture)" && \
    case "$ARCH" in \
      arm64) FFARCH=linuxarm64 ;; \
      amd64) FFARCH=linux64 ;; \
      *) echo "unsupported dpkg arch=$ARCH" >&2; exit 1 ;; \
    esac && \
    echo "Installing BtbN ffmpeg arch=$ARCH FFARCH=$FFARCH" && \
    wget -qO /tmp/ffmpeg.tar.xz "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-${FFARCH}-gpl.tar.xz" && \
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
