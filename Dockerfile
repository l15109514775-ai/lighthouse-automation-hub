FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
ARG NODE_VERSION=24.22.1
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    HUB_ROOT=/opt/lighthouse-hub PLAYWRIGHT_BROWSERS_PATH=/opt/lighthouse-hub/compositor/runtime/browsers \
    TMPDIR=/tmp TEMP=/tmp TMP=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl xz-utils python3 python3-venv ffmpeg git fontconfig xvfb xauth \
    && mkdir -p /tmp/node-install && cd /tmp/node-install \
    && curl -fsSLO https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz \
    && curl -fsSLO https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt \
    && grep " node-v${NODE_VERSION}-linux-x64.tar.xz$" SHASUMS256.txt | sha256sum -c - \
    && tar -xJf node-v${NODE_VERSION}-linux-x64.tar.xz -C /usr/local --strip-components=1 \
    && rm -rf /tmp/node-install /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
WORKDIR /opt/lighthouse-hub
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY compositor/ ./compositor/
RUN cd compositor && npm ci && npm run setup && npx playwright install-deps chromium \
    && npx playwright install chromium --no-shell --no-remove
COPY . .
RUN pip install --no-deps -e . \
    && find compositor vendor -type d ! -perm -0005 -exec chmod a+rx {} + \
    && /usr/sbin/groupadd -g 10001 lighthouse-app \
    && /usr/sbin/useradd -u 10001 -g 10001 -d /opt/lighthouse-hub -s /usr/sbin/nologin lighthouse-app \
    && mkdir -p data media logs backups \
    && chown -R 10001:10001 data media logs backups \
    && chmod -R a+rX compositor/runtime/browsers compositor/assets
USER 10001:10001
RUN python scripts/verify_upstream.py
CMD ["python","-m","services.hub.cli","api"]
