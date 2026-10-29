#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
UNIT_PATH="${UNIT_DIR}/odysseus-host-bridge.service"

mkdir -p "${UNIT_DIR}"
sed "s|%h/Projects/odysseus|${PROJECT_DIR}|g" "${PROJECT_DIR}/odysseus-host-bridge.service" > "${UNIT_PATH}"
chmod 0644 "${UNIT_PATH}"
systemctl --user daemon-reload
systemctl --user enable --now odysseus-host-bridge.service
systemctl --user --no-pager --full status odysseus-host-bridge.service
