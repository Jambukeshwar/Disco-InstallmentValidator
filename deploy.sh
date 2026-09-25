#!/bin/bash
# Podman deploy script for Disco Validator
# Run this on the EC2 server: bash deploy.sh

set -e

APP_DIR=$(cd "$(dirname "$0")" && pwd)
IMAGE=disco-validator:latest
CONTAINER=disco-validator
DB_FILE=$APP_DIR/nso_validator.db

echo "=== Disco Validator — Podman Deploy ==="
echo "App dir : $APP_DIR"

# Pre-create runtime files so volume mounts work correctly
touch "$APP_DIR/auth_data.json" "$APP_DIR/pkce_state.json"
[ -f "$DB_FILE" ] || touch "$DB_FILE"

# Build image from current directory
echo "Building image..."
podman build -t $IMAGE "$APP_DIR"

# Stop and remove old container if it exists
podman rm -f $CONTAINER 2>/dev/null && echo "Removed old container." || true

# Start container
echo "Starting container..."
podman run -d \
  --name $CONTAINER \
  -p 8001:8001 \
  --memory 600m \
  -v "$APP_DIR/.env:/app/.env:Z,ro" \
  -v "$APP_DIR/auth_data.json:/app/auth_data.json:Z" \
  -v "$APP_DIR/pkce_state.json:/app/pkce_state.json:Z" \
  -v "$DB_FILE:/app/nso_validator.db:Z" \
  $IMAGE

echo ""
echo "Container started. Useful commands:"
echo "  podman logs -f $CONTAINER       # live logs"
echo "  podman ps                       # check status"
echo "  podman stats $CONTAINER         # memory / CPU"
echo "  podman stop $CONTAINER          # stop"
echo ""
echo "To set up auto-restart on reboot:"
echo "  podman generate systemd --name $CONTAINER --files --new"
echo "  mkdir -p ~/.config/systemd/user"
echo "  mv container-$CONTAINER.service ~/.config/systemd/user/"
echo "  systemctl --user enable container-$CONTAINER"
echo "  loginctl enable-linger \$USER"
