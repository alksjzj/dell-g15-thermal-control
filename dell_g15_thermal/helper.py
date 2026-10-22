#!/usr/bin/env python3
"""Privileged helper for Dell G15 thermal control.

Runs as root under systemd and exposes a tiny JSON-over-Unix-socket API.
Only the UID recorded at install time (plus root) may connect.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from pathlib import Path

VERSION = "1.0.0"
DEFAULT_SOCKET = "/run/dell-g15-thermal/helper.sock"
DEFAULT_CONFIG = "/etc/dell-g15-thermal.conf"
HWMON_ROOT = "/sys/class/hwmon"
PROFILE_ROOT = "/sys/class/platform-profile"


def log(msg: str) -> None:
    print(f"[dell-g15-helper] {msg}", file=sys.stderr, flush=True)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def find_hwmon(name: str, root: str = HWMON_ROOT) -> Path | None:
    try:
        entries = sorted(Path(root).glob("hwmon*"))
    except OSError:
        return None
    for entry in entries:
        if read_text(entry / "name") == name:
            return entry
    return None


def find_profile(root: str = PROFILE_ROOT) -> Path | None:
    try:
        entries = sorted(Path(root).glob("platform-profile-*"))
    except OSError:
        return None
    for entry in entries:
        if read_text(entry / "name") == "alienware-wmi":
            return entry
    return None


def parse_allowed_uids(config_path: str) -> set[int]:
    allowed: set[int] = {0}
    try:
        text = Path(config_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return allowed
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in ("ALLOWED_UIDS", "ALLOWED_UID"):
            for part in value.replace(",", " ").split():
                try:
                    allowed.add(int(part))
                except ValueError:
                    pass
    return allowed


class FanController:
    def __init__(self, hwmon_root: str, profile_root: str) -> None:
        self.hwmon_root = hwmon_root
        self.profile_root = profile_root
        self.alien = find_hwmon("alienware_wmi", hwmon_root)
        self.profile_dir = find_profile(profile_root)
        self.lock = threading.Lock()
        self._last_write = 0.0

    def _ensure_interfaces(self) -> None:
        if self.alien is None:
            self.alien = find_hwmon("alienware_wmi", self.hwmon_root)
        if self.profile_dir is None:
            self.profile_dir = find_profile(self.profile_root)

    def state(self) -> dict:
        self._ensure_interfaces()
        out: dict = {
            "ok": True,
            "version": VERSION,
            "profile": None,
            "choices": [],
            "fan1_rpm": None,
            "fan2_rpm": None,
            "fan1_boost": None,
            "fan2_boost": None,
        }
        if self.profile_dir is not None:
            out["profile"] = read_text(self.profile_dir / "profile")
            out["choices"] = (read_text(self.profile_dir / "choices") or "").split()
        if self.alien is not None:
            for key, attr in (
                ("fan1_rpm", "fan1_input"),
                ("fan2_rpm", "fan2_input"),
                ("fan1_boost", "fan1_boost"),
                ("fan2_boost", "fan2_boost"),
            ):
                raw = read_text(self.alien / attr)
                try:
                    out[key] = int(raw) if raw is not None else None
                except ValueError:
                    out[key] = None
        return out

    def _throttle(self) -> None:
        # The EC/WMI interface dislikes bursts; keep writes >= 120 ms apart.
        delta = time.monotonic() - self._last_write
        if delta < 0.12:
            time.sleep(0.12 - delta)
        self._last_write = time.monotonic()

    def _write(self, path: Path, value: str) -> None:
        try:
            with open(path, "w", encoding="ascii") as fh:
                fh.write(value + "\n")
        except OSError as exc:
            raise RuntimeError(f"写入 {path} 失败：{exc.strerror or exc}") from exc

    def set_profile(self, profile: str) -> dict:
        self._ensure_interfaces()
        if self.profile_dir is None:
            raise RuntimeError("未找到 Alienware WMI 性能模式接口")
        choices = (read_text(self.profile_dir / "choices") or "").split()
        if profile not in choices:
            raise RuntimeError(f"固件不支持模式 {profile!r}（可用：{', '.join(choices)}）")
        with self.lock:
            current = read_text(self.profile_dir / "profile")
            if current != profile:
                self._throttle()
                self._write(self.profile_dir / "profile", profile)
                time.sleep(0.05)
            new = read_text(self.profile_dir / "profile")
        return {"ok": True, "profile": new, "choices": choices}

    def set_boosts(self, cpu: int | None, gpu: int | None, profile: str | None) -> dict:
        self._ensure_interfaces()
        if self.alien is None:
            raise RuntimeError("未找到 alienware_wmi 风扇接口")
        if cpu is None and gpu is None and not profile:
            raise RuntimeError("没有需要写入的参数")

        warning = None
        with self.lock:
            if profile:
                choices = []
                if self.profile_dir is not None:
                    choices = (read_text(self.profile_dir / "choices") or "").split()
                if profile in choices:
                    current = read_text(self.profile_dir / "profile")
                    if current != profile:
                        self._throttle()
                        self._write(self.profile_dir / "profile", profile)
                        time.sleep(0.05)
                elif self.profile_dir is not None:
                    warning = (
                        f"固件未提供 {profile!r} 模式，已保留当前模式；"
                        "风扇增强可能受温度曲线影响。"
                    )

            if cpu is not None:
                self._throttle()
                self._write(self.alien / "fan1_boost", str(max(0, min(255, int(cpu)))))
            if gpu is not None:
                self._throttle()
                self._write(self.alien / "fan2_boost", str(max(0, min(255, int(gpu)))))
            time.sleep(0.03)
            result = self.state()

        result["warning"] = warning
        result["ok"] = True
        return result


CONTROLLER: FanController | None = None
ALLOWED_UIDS: set[int] = {0}


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
        except OSError:
            return
        if uid not in ALLOWED_UIDS:
            self._reply({"ok": False, "error": "当前用户没有风扇控制权限，请重新运行安装脚本"})
            return

        sock.settimeout(3.0)
        buf = bytearray()
        try:
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 65536:
                    self._reply({"ok": False, "error": "请求过大"})
                    return
        except OSError:
            return
        if not buf:
            return
        try:
            request = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reply({"ok": False, "error": "无效的 JSON 请求"})
            return
        if not isinstance(request, dict):
            self._reply({"ok": False, "error": "请求必须是对象"})
            return

        op = request.get("op")
        try:
            if op == "ping":
                self._reply(
                    {
                        "ok": True,
                        "version": VERSION,
                        "uid": uid,
                        "allowed": sorted(ALLOWED_UIDS),
                        "model": read_text(Path("/sys/class/dmi/id/product_name")),
                    }
                )
            elif op == "get":
                self._reply(CONTROLLER.state())  # type: ignore[union-attr]
            elif op == "set_profile":
                profile = str(request.get("profile", ""))
                self._reply(CONTROLLER.set_profile(profile))  # type: ignore[union-attr]
            elif op == "set_boosts":
                cpu = request.get("cpu")
                gpu = request.get("gpu")
                cpu = int(cpu) if cpu is not None else None
                gpu = int(gpu) if gpu is not None else None
                profile = request.get("profile")
                profile = str(profile) if profile else None
                self._reply(CONTROLLER.set_boosts(cpu, gpu, profile))  # type: ignore[union-attr]
            else:
                self._reply({"ok": False, "error": f"未知操作 {op!r}"})
        except (RuntimeError, ValueError, TypeError) as exc:
            self._reply({"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            log(f"unexpected error: {exc!r}")
            self._reply({"ok": False, "error": "内部错误"})

    def _reply(self, payload: dict) -> None:
        try:
            self.request.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    global CONTROLLER, ALLOWED_UIDS

    parser = argparse.ArgumentParser(description="Dell G15 thermal control helper")
    parser.add_argument("--socket", default=os.environ.get("DELL_G15_HELPER_SOCKET", DEFAULT_SOCKET))
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--hwmon-root", default=HWMON_ROOT)
    parser.add_argument("--profile-root", default=PROFILE_ROOT)
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()

    ALLOWED_UIDS = parse_allowed_uids(args.config)
    CONTROLLER = FanController(args.hwmon_root, args.profile_root)

    if CONTROLLER.alien is None:
        log("warning: alienware_wmi hwmon interface not found")

    socket_path = Path(args.socket)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"cannot remove stale socket: {exc}")

    server = Server(str(socket_path), Handler)
    os.chmod(socket_path, 0o666)
    log(
        f"ready on {socket_path} (uid allow-list: {sorted(ALLOWED_UIDS)}, "
        f"fan backend: {'alienware_wmi' if CONTROLLER.alien else 'none'})"
    )

    def stop(_signum, _frame):
        log("stopping")
        # shutdown() must not be called from the serve_forever thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        try:
            socket_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
