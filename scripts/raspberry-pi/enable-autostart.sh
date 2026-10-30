#!/usr/bin/env bash
# Start FileComp on every boot from the already built image, with or without internet.
# Run once from the project root after the first `docker compose up -d --build`:
#   ./scripts/raspberry-pi/enable-autostart.sh
set -euo pipefail

cd "$(dirname "$0")/../.."
PROJECT_DIR=$(pwd -P)
DOCKER_BIN=$(command -v docker)
IMAGE=filecompression:latest
UNIT=/etc/systemd/system/filecomp.service

# The build is the only step that needs internet (base images and packages), so it must be done.
if ! sudo "$DOCKER_BIN" image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Image $IMAGE not found. Build it once with internet access: docker compose up -d --build" >&2
    exit 1
fi

echo "==> Installing $UNIT"
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=FileComp (Docker Compose, starts from the local image)
# Only the local Docker daemon is needed. There is deliberately no network-online dependency:
# without internet, or with Wi-Fi busy as the access point, FileComp must still come up.
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJECT_DIR
# --no-build and --pull never: use the image already on the Pi and never contact a registry.
# Also recreates the container if it was removed with "docker compose down".
ExecStart=$DOCKER_BIN compose up -d --no-build --pull never
ExecStop=$DOCKER_BIN compose stop
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable docker.service containerd.service >/dev/null
sudo systemctl enable --now filecomp.service
echo "==> FileComp now starts on every boot, also without internet."
echo "    Status:            systemctl status filecomp"
echo "    Disable autostart: sudo systemctl disable filecomp"
