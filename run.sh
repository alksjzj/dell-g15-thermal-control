#!/usr/bin/env bash
# Portable launcher (no installation required).
set -euo pipefail
cd "$(dirname "$0")"
if ! python3 -c 'import gi, psutil, cairo; gi.require_foreign("cairo")' >/dev/null 2>&1; then
  echo "缺少运行依赖，请先安装：" >&2
  echo "  sudo apt install -y python3-gi python3-gi-cairo python3-psutil gir1.2-gtk-3.0" >&2
  exit 1
fi
exec python3 -m dell_g15_thermal.app "$@"
