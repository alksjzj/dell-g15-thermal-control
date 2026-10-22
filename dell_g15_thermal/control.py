"""Privileged-helper client, preset switching and small settings store."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_SOCKET = "/run/dell-g15-thermal/helper.sock"


class HelperError(RuntimeError):
    pass


class HelperClient:
    """Talks to the root helper over a local Unix socket.

    The helper authenticates the peer with SO_PEERCRED and only accepts the
    UID recorded at install time, so the socket itself can be world-reachable
    without letting other local users change fan speeds.
    """

    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = socket_path or os.environ.get("DELL_G15_HELPER_SOCKET", DEFAULT_SOCKET)
        self._available: bool | None = None
        self._last_ping = 0.0
        self.info: dict = {}

    def _call(self, payload: dict, timeout: float = 2.5) -> dict:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(self.socket_path)
                sock.sendall(data)
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > 1_000_000:
                        break
        except (OSError, socket.timeout) as exc:
            self._available = False
            raise HelperError(f"无法连接风扇控制服务：{exc}") from exc
        if not buf:
            raise HelperError("风扇控制服务没有返回数据")
        try:
            reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HelperError("风扇控制服务返回了无效数据") from exc
        if not reply.get("ok"):
            raise HelperError(reply.get("error") or "风扇控制服务拒绝了请求")
        return reply

    def available(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and self._available is not None and now - self._last_ping < 3.0:
            return self._available
        self._last_ping = now
        try:
            reply = self._call({"op": "ping"}, timeout=1.2)
        except HelperError:
            self._available = False
            self.info = {}
            return False
        self._available = True
        self.info = reply
        return True

    def refresh(self) -> bool:
        return self.available(force=True)

    def state(self) -> dict:
        return self._call({"op": "get"})

    def set_boosts(self, cpu: int | None, gpu: int | None, profile: str | None = "custom") -> dict:
        payload = {"op": "set_boosts", "cpu": cpu, "gpu": gpu}
        if profile:
            payload["profile"] = profile
        return self._call(payload)

    def set_profile(self, profile: str) -> dict:
        return self._call({"op": "set_profile", "profile": profile})


@dataclass
class Settings:
    sync_fans: bool = True
    protect: bool = True
    last_preset: str = "balanced"
    window_width: int = 1280
    window_height: int = 800

    @classmethod
    def path(cls) -> Path:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(base) / "dell-g15-thermal" / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        try:
            path = self.path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


def power_profiles_available() -> bool:
    return shutil.which("powerprofilesctl") is not None


def get_ppd_profile() -> str | None:
    try:
        res = subprocess.run(
            ["powerprofilesctl", "get"], capture_output=True, text=True, timeout=2.5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def set_preset(preset: str, helper: HelperClient | None = None) -> tuple[bool, str]:
    """Switch one of the three Dell/AWCC presets.

    Prefers power-profiles-daemon because it is allowed for the active local
    session without a password.  Falls back to the root helper if PPD is not
    installed or fails.
    """
    from .sensors import PPD_NAMES, PRESET_TO_PROFILE

    ppd = PPD_NAMES.get(preset)
    profile = PRESET_TO_PROFILE.get(preset)
    ppd_ok = False
    detail = "未找到 power-profiles-daemon"

    if ppd and power_profiles_available():
        try:
            res = subprocess.run(
                ["powerprofilesctl", "set", ppd],
                capture_output=True,
                text=True,
                timeout=8.0,
            )
            if res.returncode == 0:
                ppd_ok = True
            else:
                detail = (res.stderr or res.stdout or "").strip()
        except (OSError, subprocess.SubprocessError) as exc:
            detail = str(exc)

    # PPD may no-op if its active profile already matches the requested one
    # (which happens when the AWCC profile was switched to "custom" manually).
    # Force the AWCC profile through the helper so "恢复预设" always leaves
    # manual fan mode.
    if helper is not None and profile and helper.available():
        try:
            helper.set_profile(profile)
            return True, f"已切换到{preset_name(preset)}模式"
        except HelperError as exc:
            if not ppd_ok:
                detail = str(exc)

    if ppd_ok:
        return True, f"已切换到{preset_name(preset)}模式"
    return False, f"切换失败：{detail}"


def preset_name(preset: str | None) -> str:
    return {"performance": "性能", "balanced": "平衡", "saver": "节电", "custom": "手动"}.get(
        preset or "", "未知"
    )


def no_new_privileges() -> bool:
    try:
        text = Path("/proc/self/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("NoNewPrivs:"):
            return line.split(":", 1)[1].strip() == "1"
    return False


def autostart_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "dell-g15-thermal.desktop"


def set_autostart(enabled: bool, exec_cmd: str, icon: str = "dell-g15-thermal") -> bool:
    path = autostart_path()
    if not enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Dell G15 温控中心\n"
        "Comment=实时温度、风扇与性能模式控制\n"
        f"Exec={exec_cmd}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError:
        return False
    return True


def install_command(script: Path, user: str | None = None) -> list[str] | None:
    if not script.exists():
        return None
    args = ["bash", str(script)]
    if user:
        args += ["--user", user]
    if shutil.which("pkexec"):
        return ["pkexec", *args]
    if shutil.which("sudo"):
        return ["sudo", *args]
    return None
