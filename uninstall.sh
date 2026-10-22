#!/usr/bin/env bash
# Remove the Dell G15 thermal helper and installed application files.
set -euo pipefail

INSTALL_DIR="/opt/dell-g15-thermal"
HELPER_DIR="/usr/local/libexec/dell-g15-thermal"
SERVICE_NAME="dell-g15-thermal-helper.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
CONFIG_PATH="/etc/dell-g15-thermal.conf"
BIN_PATH="/usr/local/bin/dell-g15-thermal"
DESKTOP_PATH="/usr/share/applications/dell-g15-thermal.desktop"

REAL_USER="${PKEXEC_UID:+$(getent passwd "${PKEXEC_UID}" | cut -d: -f1)}"
REAL_USER="${REAL_USER:-${SUDO_USER:-}}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 权限运行：sudo bash \"$0\"" >&2
  exit 1
fi

echo "== 卸载 Dell G15 温控中心 =="
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
rm -f "${UNIT_PATH}" "${CONFIG_PATH}" "${BIN_PATH}" "${DESKTOP_PATH}"
rm -f /run/dell-g15-thermal/helper.sock 2>/dev/null || true
rmdir /run/dell-g15-thermal 2>/dev/null || true
rm -rf "${HELPER_DIR}" "${INSTALL_DIR}"
systemctl daemon-reload

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

if [[ -n "${REAL_USER}" && "${REAL_USER}" != "root" ]]; then
  USER_HOME="$(getent passwd "${REAL_USER}" | cut -d: -f6)"
  for d in "${USER_HOME}/桌面" "${USER_HOME}/Desktop"; do
    rm -f "${d}/dell-g15-thermal.desktop" 2>/dev/null || true
  done
  rm -f "${USER_HOME}/.config/autostart/dell-g15-thermal.desktop" 2>/dev/null || true
fi

echo "已移除服务与应用文件。"
echo "用户设置仍保留在 ~/.config/dell-g15-thermal/（如不再需要可手动删除）。"
