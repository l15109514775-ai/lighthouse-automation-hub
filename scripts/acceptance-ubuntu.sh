#!/usr/bin/env bash
set -euo pipefail
cd /opt/lighthouse-hub
mkdir -p /home/lighthouse-ops/lighthouse-evidence
docker run --rm --user 0 --network none --shm-size 256m --memory 1800m \
  lighthouse-hub:0.1.0 bash -ec '
    cat /etc/os-release | head -n 3
    node --version
    python --version
    python -m pytest -q -p no:cacheprovider
    cd compositor
    npm run doctor
    npm test
  ' > /home/lighthouse-ops/lighthouse-evidence/ubuntu-tests.log 2>&1
tail -n 35 /home/lighthouse-ops/lighthouse-evidence/ubuntu-tests.log
echo 'UBUNTU_OFFLINE_TESTS_PASSED'
