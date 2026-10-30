#!/usr/bin/env bash
# Install and start FileComp on a Raspberry Pi (Raspberry Pi OS / Debian / Ubuntu).
# Run from the project root:   ./scripts/raspberry-pi/install.sh
set -euo pipefail

cd "$(dirname "$0")/../.."

echo "==> Architecture: $(uname -m), RAM: $(awk '/MemTotal/ {printf "%.1f GB", $2/1024/1024}' /proc/meminfo)"

if ! command -v docker >/dev/null 2>&1; then
    echo "==> Installing Docker (official convenience script)"
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "    Added $USER to the docker group; it takes effect after the next login."
fi
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
fi
if ! $DOCKER compose version >/dev/null 2>&1; then
    echo "Docker Compose v2 is required: sudo apt-get install docker-compose-plugin" >&2
    exit 1
fi

if [ ! -f .env ]; then
    echo "==> Creating .env from .env.example"
    cp .env.example .env
    sed -i "s/^PUID=.*/PUID=$(id -u)/; s/^PGID=.*/PGID=$(id -g)/" .env
fi

DATA_DIR=$(grep -E '^FILECOMP_DATA_DIR=' .env | cut -d= -f2-)
DATA_DIR=${DATA_DIR:-./data}
mkdir -p "$DATA_DIR"
if [ "$(stat -c %u "$DATA_DIR")" != "$(id -u)" ]; then
    sudo chown "$(id -u):$(id -g)" "$DATA_DIR"
fi
echo "==> Data directory: $(realpath "$DATA_DIR") ($(df -h --output=avail "$DATA_DIR" | tail -1 | tr -d ' ') free)"

echo "==> Building and starting the container (the first build on a Pi takes a few minutes)"
$DOCKER compose up -d --build

PORT=$(grep -E '^FILECOMP_WEB_PORT=' .env | cut -d= -f2-)
PORT=${PORT:-80}
# Port 80 is the default for http://, so it is left out of the printed addresses.
SUFFIX=":${PORT}"
[ "$PORT" = 80 ] && SUFFIX=""
echo "==> Waiting for the service"
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

echo
echo "FileComp is running. Open it from any device on the local network:"
for address in $(hostname -I); do
    case "$address" in
        *:*) ;;                       # skip IPv6
        172.17.*|172.18.*) ;;         # skip Docker bridges
        *) echo "    http://${address}${SUFFIX}" ;;
    esac
done
echo "    http://$(hostname).local${SUFFIX}   (mDNS, if avahi-daemon is running)"
echo

./scripts/raspberry-pi/enable-autostart.sh

echo
echo "Logs:    $DOCKER compose logs -f"
echo "Stop:    sudo systemctl stop filecomp   (starts again on the next boot)"
