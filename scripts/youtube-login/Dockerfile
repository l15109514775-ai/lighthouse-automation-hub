FROM lighthouse-hub:ytdlp-v1c
USER 0:0
RUN apt-get update && apt-get install -y --no-install-recommends x11vnc novnc websockify && rm -rf /var/lib/apt/lists/*
USER 10001:10001
