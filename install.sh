#!/usr/bin/env bash
# Dell G15 温控中心 — one-time privileged helper installation.
set -euo pipefail

APP_NAME="Dell G15 温控中心"
INSTALL_DIR="/opt/dell-g15-thermal"
HELPER_DIR="/usr/local/libexec/dell-g15-thermal"
SERVICE_NAME="dell-g15-thermal-helper.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
CONFIG_PATH="/etc/dell-g15-thermal.conf"
BIN_PATH="/usr/local/bin/dell-g15-thermal"
DESKTOP_PATH="/usr/share/applications/dell-g15-thermal.desktop"
ICON_PATH="${INSTALL_DIR}/dell_g15_thermal/assets/icon.svg"

REAL_USER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      REAL_USER="${2:-}"
      shift 2
      ;;
    --user=*)
      REAL_USER="${1#*=}"
      shift
      ;;
    -h|--help)
      echo "用法: sudo bash install.sh [--user 用户名]"
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 root 权限运行：sudo bash \"$0\"" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -d "${SRC_DIR}/dell_g15_thermal" ]]; then
  echo "安装源不完整：找不到 ${SRC_DIR}/dell_g15_thermal" >&2
  exit 1
fi

# Work out which desktop user is allowed to talk to the helper.
if [[ -z "${REAL_USER}" ]]; then
  if [[ -n "${PKEXEC_UID:-}" ]]; then
    REAL_USER="$(getent passwd "${PKEXEC_UID}" | cut -d: -f1 || true)"
  fi
  if [[ -z "${REAL_USER}" && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    REAL_USER="${SUDO_USER}"
  fi
  if [[ -z "${REAL_USER}" ]] && command -v loginctl >/dev/null 2>&1; then
    REAL_USER="$(loginctl list-sessions --no-legend 2>/dev/null | awk 'NF>=3 && $3!="root" {print $3; exit}')"
  fi
fi

REAL_UID=""
if [[ -n "${REAL_USER}" ]]; then
  REAL_UID="$(id -u "${REAL_USER}" 2>/dev/null || true)"
fi

echo "== ${APP_NAME} 安装程序 =="
echo "应用目录 : ${INSTALL_DIR}"
echo "授权用户 : ${REAL_USER:-（仅 root）}${REAL_UID:+ (uid ${REAL_UID})}"

# Runtime dependencies (all are in the Ubuntu archive / normally preinstalled).
if ! python3 -c 'import gi, psutil, cairo; gi.require_foreign("cairo")' >/dev/null 2>&1; then
  echo "缺少 Python 运行依赖。" >&2
  echo "请先运行：" >&2
  echo "  sudo apt install -y python3-gi python3-gi-cairo python3-psutil gir1.2-gtk-3.0" >&2
  exit 1
fi
if ! python3 -c 'import gi; gi.require_version("Gtk","3.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
  echo "缺少 GTK3 Python 绑定（gir1.2-gtk-3.0）。" >&2
  exit 1
fi

# Check hardware interfaces and try to load the module if necessary.
if ! grep -qs "alienware_wmi" /sys/class/hwmon/hwmon*/name; then
  echo "未检测到 alienware_wmi，尝试加载内核模块…"
  modprobe alienware_wmi 2>/dev/null || true
fi
if ! grep -qs "alienware_wmi" /sys/class/hwmon/hwmon*/name; then
  echo "警告：仍未检测到 alienware_wmi 风扇接口。" >&2
  echo "      温控界面仍可监测，但手动风扇控制可能不可用。" >&2
fi

# Stop an existing service before replacing files.
if systemctl list-unit-files "${SERVICE_NAME}" >/dev/null 2>&1; then
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
fi

install -d -m 0755 "${INSTALL_DIR}"
install -d -m 0755 "${HELPER_DIR}"
rm -rf "${INSTALL_DIR}/dell_g15_thermal"
cp -a "${SRC_DIR}/dell_g15_thermal" "${INSTALL_DIR}/"
find "${INSTALL_DIR}/dell_g15_thermal" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "${INSTALL_DIR}/dell_g15_thermal" -type f -name '*.pyc' -delete 2>/dev/null || true
for extra in install.sh uninstall.sh README.md; do
  if [[ -f "${SRC_DIR}/${extra}" ]]; then
    cp -a "${SRC_DIR}/${extra}" "${INSTALL_DIR}/"
  fi
done
if [[ -d "${SRC_DIR}/docs" ]]; then
  rm -rf "${INSTALL_DIR}/docs"
  cp -a "${SRC_DIR}/docs" "${INSTALL_DIR}/"
fi
chmod -R a+rX "${INSTALL_DIR}"
chmod 0755 "${INSTALL_DIR}/install.sh" "${INSTALL_DIR}/uninstall.sh" 2>/dev/null || true

install -d -m 0755 "${HELPER_DIR}"
install -m 0755 "${SRC_DIR}/dell_g15_thermal/helper.py" "${HELPER_DIR}/helper.py"

cat > "${CONFIG_PATH}" <<EOF
# Managed by ${APP_NAME} install.sh
# Only these local UIDs may request fan speed changes through the helper.
ALLOWED_UIDS=${REAL_UID:-0}
EOF
chmod 0644 "${CONFIG_PATH}"

cat > "${UNIT_PATH}" <<EOF
[Unit]
Description=Dell G15 Thermal Control Helper (fan/preset writes)
After=multi-user.target
ConditionPathExists=/sys/class/hwmon

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${HELPER_DIR}/helper.py
Restart=on-failure
RestartSec=2
RuntimeDirectory=dell-g15-thermal
RuntimeDirectoryMode=0755
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=full
ReadWritePaths=/sys /run/dell-g15-thermal
RestrictAddressFamilies=AF_UNIX

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "${UNIT_PATH}"

cat > "${BIN_PATH}" <<'EOF'
#!/bin/sh
export PYTHONPATH="/opt/dell-g15-thermal${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m dell_g15_thermal.app "$@"
EOF
chmod 0755 "${BIN_PATH}"

cat > "${DESKTOP_PATH}" <<EOF
[Desktop Entry]
Type=Application
Name=Dell G15 温控中心
Name[en]=Dell G15 Thermal Control
GenericName=Thermal Monitor
Comment=实时温度、双风扇与 Dell 性能模式控制
Exec=${BIN_PATH}
Icon=${ICON_PATH}
Terminal=false
Categories=System;Monitor;
Keywords=thermal;fan;temperature;dell;g15;cpu;gpu;
StartupWMClass=dell-g15-thermal
EOF
chmod 0644 "${DESKTOP_PATH}"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi

# Desktop shortcut for the invoking user.
if [[ -n "${REAL_USER}" && "${REAL_USER}" != "root" ]]; then
  USER_HOME="$(getent passwd "${REAL_USER}" | cut -d: -f6)"
  if [[ -n "${USER_HOME}" && -d "${USER_HOME}" ]]; then
    USER_DESKTOP=""
    if command -v sudo >/dev/null 2>&1; then
      USER_DESKTOP="$(sudo -u "${REAL_USER}" xdg-user-dir DESKTOP 2>/dev/null || true)"
    fi
    if [[ -z "${USER_DESKTOP}" || "${USER_DESKTOP}" == "${USER_HOME}" ]]; then
      if [[ -d "${USER_HOME}/桌面" ]]; then
        USER_DESKTOP="${USER_HOME}/桌面"
      else
        USER_DESKTOP="${USER_HOME}/Desktop"
      fi
    fi
    install -d -m 0755 "${USER_DESKTOP}"
    cp -f "${DESKTOP_PATH}" "${USER_DESKTOP}/dell-g15-thermal.desktop"
    chmod 0755 "${USER_DESKTOP}/dell-g15-thermal.desktop"
    chown "${REAL_USER}:$(id -gn "${REAL_USER}")" "${USER_DESKTOP}/dell-g15-thermal.desktop" 2>/dev/null || true
    # GNOME may need the shortcut marked trusted.
    if command -v gio >/dev/null 2>&1; then
      sudo -u "${REAL_USER}" gio set "${USER_DESKTOP}/dell-g15-thermal.desktop" metadata::trusted true 2>/dev/null || true
    fi
    echo "桌面快捷方式 : ${USER_DESKTOP}/dell-g15-thermal.desktop"
  fi
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true

SOCKET="/run/dell-g15-thermal/helper.sock"
rm -f "${SOCKET}" 2>/dev/null || true
systemctl restart "${SERVICE_NAME}"

READY=0
for _ in $(seq 1 40); do
  if [[ -S "${SOCKET}" ]]; then
    READY=1
    break
  fi
  sleep 0.1
done

if [[ "${READY}" -eq 1 ]]; then
  echo "风扇控制服务 : 已启动 (${SOCKET})"
  python3 - <<'PY' || true
import json, socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(2)
s.connect("/run/dell-g15-thermal/helper.sock")
s.sendall(b'{"op":"ping"}\n')
print("服务自检 :", json.loads(s.recv(4096).decode()) )
PY
else
  echo "风扇控制服务 : 启动失败，请运行 systemctl status ${SERVICE_NAME}" >&2
  systemctl --no-pager --full status "${SERVICE_NAME}" >&2 || true
  exit 3
fi

echo
echo "安装完成。"
echo "启动方式：应用菜单搜索 “Dell G15”，或运行 ${BIN_PATH}"
echo "若要完全移除：sudo bash ${INSTALL_DIR}/uninstall.sh"
