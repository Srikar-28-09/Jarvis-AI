#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
BIN_DIR="${HOME}/.local/bin"
LIBEXEC_DIR="${HOME}/.local/libexec"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
STOP_SERVICES=0

if [[ "${1:-}" == "--stop-services" ]]; then
  STOP_SERVICES=1
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--stop-services]" >&2
  exit 2
fi

HELPER="${LIBEXEC_DIR}/odysseus-ubuntu-shortcut.py"
if [[ -x "${HELPER}" || -f "${HELPER}" ]]; then
  python3 "${HELPER}" remove
else
  python3 "${PROJECT_DIR}/scripts/ubuntu-shortcut.py" remove
fi
if [[ ${STOP_SERVICES} -eq 1 ]]; then
  systemctl --user stop --no-block odysseus-backend.service odysseus-host-bridge.service || true
fi
systemctl --user disable odysseus-backend.service odysseus-host-bridge.service >/dev/null 2>&1 || true
rm -f "${UNIT_DIR}/odysseus-backend.service" "${UNIT_DIR}/odysseus-host-bridge.service"
rm -f "${DESKTOP_DIR}/odysseus-voice.desktop" "${BIN_DIR}/odysseus-voice" "${HELPER}"
systemctl --user daemon-reload
echo "Removed the Jarvis GNOME shortcut and launcher registration."
if [[ ${STOP_SERVICES} -eq 0 ]]; then
  echo "Services were left running. Re-run with --stop-services to stop the Jarvis-managed services."
fi
