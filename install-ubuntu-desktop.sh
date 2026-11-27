#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
BIN_DIR="${HOME}/.local/bin"
LIBEXEC_DIR="${HOME}/.local/libexec"
UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
LAUNCHER="${BIN_DIR}/odysseus-voice"
SHORTCUT_HELPER="${LIBEXEC_DIR}/odysseus-ubuntu-shortcut.py"
DOCKER_BIN="$(command -v docker || true)"

systemd_escape_value() {
  local value="$1"
  local escaped=""
  local char
  local index
  for ((index=0; index<${#value}; index++)); do
    char="${value:index:1}"
    case "${char}" in
      ' ') escaped+='\x20' ;;
      $'\t') escaped+='\x09' ;;
      $'\n') escaped+='\x0a' ;;
      '\') escaped+='\x5c' ;;
      '"') escaped+='\x22' ;;
      '%') escaped+='%%' ;;
      *) escaped+="${char}" ;;
    esac
  done
  printf '%s' "${escaped}"
}

sed_escape_replacement() {
  local value="${1//\\/\\\\}"
  value="${value//&/\\&}"
  value="${value//|/\\|}"
  printf '%s' "${value}"
}

ESCAPED_PROJECT_DIR="$(sed_escape_replacement "$(systemd_escape_value "${PROJECT_DIR}")")"
ESCAPED_DOCKER_BIN="$(sed_escape_replacement "$(systemd_escape_value "${DOCKER_BIN}")")"

if [[ "${XDG_CURRENT_DESKTOP:-}" != *GNOME* && "${XDG_CURRENT_DESKTOP:-}" != *ubuntu* ]]; then
  echo "This installer registers GNOME custom keybindings. Current desktop: ${XDG_CURRENT_DESKTOP:-unknown}." >&2
  echo "Use your desktop environment's supported global-shortcut mechanism instead." >&2
  exit 1
fi
if ! command -v gsettings >/dev/null || ! command -v systemctl >/dev/null; then
  echo "Ubuntu GNOME requires gsettings and systemd --user." >&2
  exit 1
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "Install Docker Engine with the Compose plugin and ensure your user can run docker." >&2
  exit 1
fi
if [[ ! -x "${PROJECT_DIR}/desktop/bin/odysseus-voice" ]]; then
  echo "Desktop launcher script is missing or not executable." >&2
  exit 1
fi
if [[ ! -x "${PROJECT_DIR}/desktop/src-tauri/target/release/odysseus-voice" ]]; then
  if [[ ! -x "${PROJECT_DIR}/desktop/node_modules/.bin/tauri" ]]; then
    echo "Install desktop dependencies first: cd \"${PROJECT_DIR}/desktop\" && npm ci" >&2
    exit 1
  fi
  (cd "${PROJECT_DIR}/desktop" && npm run tauri build -- --bundles deb)
fi

BACKUP_DIR="$(mktemp -d)"
MANAGED_PATHS=()
MANAGED_BACKUPS=()
MANAGED_EXISTED=()
INSTALL_MODIFICATIONS_STARTED=0

snapshot_managed_path() {
  local path="$1"
  local backup="${BACKUP_DIR}/${#MANAGED_PATHS[@]}"
  MANAGED_PATHS+=("${path}")
  MANAGED_BACKUPS+=("${backup}")
  if [[ -e "${path}" || -L "${path}" ]]; then
    MANAGED_EXISTED+=(1)
    cp -a -- "${path}" "${backup}"
  else
    MANAGED_EXISTED+=(0)
  fi
}

cleanup() {
  local status="$?"
  local cleanup_failed=0
  local index
  trap - EXIT
  if [[ "${status}" -ne 0 && "${INSTALL_MODIFICATIONS_STARTED}" -eq 1 ]]; then
    for ((index=${#MANAGED_PATHS[@]} - 1; index >= 0; index--)); do
      if ! rm -f -- "${MANAGED_PATHS[index]}"; then
        cleanup_failed=1
      fi
      if [[ "${MANAGED_EXISTED[index]}" -eq 1 ]] &&
        ! cp -a -- "${MANAGED_BACKUPS[index]}" "${MANAGED_PATHS[index]}"; then
        cleanup_failed=1
      fi
    done
    if ! systemctl --user daemon-reload; then
      cleanup_failed=1
    fi
    if [[ "${cleanup_failed}" -eq 0 ]]; then
      echo "Installation failed (exit ${status}); cleanup restored previous launcher and service files." >&2
    else
      echo "Installation failed (exit ${status}); cleanup was incomplete. Check: ${MANAGED_PATHS[*]}" >&2
    fi
  fi
  rm -rf -- "${BACKUP_DIR}"
  exit "${status}"
}
trap cleanup EXIT

mkdir -p "${BIN_DIR}" "${LIBEXEC_DIR}" "${DESKTOP_DIR}" "${UNIT_DIR}"
snapshot_managed_path "${LAUNCHER}"
snapshot_managed_path "${SHORTCUT_HELPER}"
snapshot_managed_path "${UNIT_DIR}/odysseus-host-bridge.service"
snapshot_managed_path "${UNIT_DIR}/odysseus-backend.service"
snapshot_managed_path "${DESKTOP_DIR}/odysseus-voice.desktop"
INSTALL_MODIFICATIONS_STARTED=1
ln -sfn "${PROJECT_DIR}/desktop/bin/odysseus-voice" "${LAUNCHER}"
install -m 0755 "${PROJECT_DIR}/scripts/ubuntu-shortcut.py" "${SHORTCUT_HELPER}"

DESKTOP_LAUNCHER="${LAUNCHER//\\/\\\\}"
DESKTOP_LAUNCHER="${DESKTOP_LAUNCHER//\"/\\\"}"
sed "s|%h/Projects/odysseus|${ESCAPED_PROJECT_DIR}|g" \
  "${PROJECT_DIR}/odysseus-host-bridge.service" > "${UNIT_DIR}/odysseus-host-bridge.service"
sed -e "s|%h/Projects/odysseus|${ESCAPED_PROJECT_DIR}|g" \
  -e "s|@DOCKER_BIN@|${ESCAPED_DOCKER_BIN}|g" \
  "${PROJECT_DIR}/odysseus-backend.service" > "${UNIT_DIR}/odysseus-backend.service"
chmod 0644 "${UNIT_DIR}/odysseus-host-bridge.service" "${UNIT_DIR}/odysseus-backend.service"
systemctl --user daemon-reload

cat > "${DESKTOP_DIR}/odysseus-voice.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Jarvis-AI
Comment=Open the Jarvis-AI desktop assistant
Exec="${DESKTOP_LAUNCHER}" --toggle
Terminal=false
Categories=Utility;
EOF
chmod 0644 "${DESKTOP_DIR}/odysseus-voice.desktop"

python3 "${SHORTCUT_HELPER}" install --launcher "${LAUNCHER}" --shortcut Ctrl+Alt+Shift+J
trap - EXIT
rm -rf -- "${BACKUP_DIR}"
echo "Installed Jarvis-AI launcher and Ctrl+Alt+Shift+J cold-start shortcut."
echo "Backend and host bridge start through systemd --user when the desktop opens."
echo "The first shortcut activation starts the configured Docker Compose services in the background."
