ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

# Synchronize shell
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install runtime dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-requests \
    py3-pycryptodome \
    py3-wheel \
    ffmpeg \
    bash \
    jq \
    tzdata \
    curl

# Copy and install python package
COPY app /app
RUN pip install --no-cache-dir --break-system-packages --no-deps /app

# Copy entrypoint script
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
