# Temporary overlay only. Does not replace the core application image tag.
ARG BASE_IMAGE=lighthouse-hub:0.1.0
FROM ${BASE_IMAGE}
USER 0
RUN apt-get update && apt-get install -y --no-install-recommends x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
