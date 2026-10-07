# Stage 1: Build Python dependencies
# Set the base image using Python 3.13 and Debian Bookworm
FROM python:3.13-slim-bookworm  as builder

# Version of the published scietex.log_aggregator_service wheel to install.
# build_image.sh passes the version read from
# src/scietex/log_aggregator_service/version.py, so the image and the PyPI
# artifact always match.
ARG VERSION

WORKDIR /app

# Install system dependencies (required for some Python packages)
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create and activate virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install the released package from PyPI. The image is a distribution channel of
# the same tagged source that was published, so it does not build from the local
# checkout.
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir "scietex.log_aggregator_service==${VERSION}"

# Stage 2: Runtime image
FROM python:3.13-slim-bookworm

WORKDIR /app

# Copy virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Run as non-root user. The aggregator needs no devices or privileged ports;
# it only talks to Valkey.
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app && \
    chmod -R 755 /app

# Configuration lives in a mounted volume so it survives container replacement.
# SCIETEX_CONFIG_DIR is honored by the framework's prepare_conf_dir; the service
# namespaces its files under <config-dir>/log_aggregator/.
ENV SCIETEX_CONFIG_DIR=/config \
    SCIETEX_SERVICE_NAME=LogAggregatorService \
    SCIETEX_LOGGING_LEVEL=INFO
RUN mkdir -p /config && chown appuser:appuser /config
VOLUME ["/config"]

USER appuser

# Run the app
CMD ["start-log-aggregator"]
