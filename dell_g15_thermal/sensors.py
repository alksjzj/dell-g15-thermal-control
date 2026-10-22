"""Sensor access layer for the Dell G15 thermal control app.

Everything here is read-only and works as an ordinary user.  Privileged fan
writes live in :mod:`dell_g15_thermal.control` / ``helper.py``.
"""

from __future__ import annotations

import ctypes
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

# Allow tests / demos to point at a fake sysfs tree.
HWMON_ROOT = Path(os.environ.get("DELL_G15_HWMON_ROOT", "/sys/class/hwmon"))
PROFILE_ROOT = Path(os.environ.get("DELL_G15_PROFILE_ROOT", "/sys/class/platform-profile"))
THERMAL_ROOT = Path(os.environ.get("DELL_G15_THERMAL_ROOT", "/sys/class/thermal"))
DMI_ROOT = Path(os.environ.get("DELL_G15_DMI_ROOT", "/sys/class/dmi/id"))
PROC_ROOT = Path(os.environ.get("DELL_G15_PROC_ROOT", "/proc"))

PRESET_TO_PROFILE = {
    "performance": "performance",
    "balanced": "balanced",
    "saver": "quiet",
}
PROFILE_TO_PRESET = {
    "performance": "performance",
    "balanced-performance": "performance",
    "balanced": "balanced",
    "quiet": "saver",
    "low-power": "saver",
}
PRESET_NAMES = {
    "performance": "性能",
    "balanced": "平衡",
    "saver": "节电",
    "custom": "手动",
}
PPD_NAMES = {
    "performance": "performance",
    "balanced": "balanced",
    "saver": "power-saver",
}


def read_text(path: Path | str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def read_int(path: Path | str | None) -> int | None:
    raw = read_text(path)
    if raw is None:
        return None
    m = re.search(r"-?\d+", raw)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def read_millideg(path: Path | str | None) -> float | None:
    value = read_int(path)
    if value is None:
        return None
    # Sanity check: 0..150 C
    if value < -20000 or value > 150000:
        return None
    return value / 1000.0


def _hwmon_by_name(name: str) -> Path | None:
    try:
        entries = sorted(HWMON_ROOT.glob("hwmon*"))
    except OSError:
        return None
    for entry in entries:
        if read_text(entry / "name") == name:
            return entry
    return None


def _platform_profile_by_name(name: str) -> Path | None:
    try:
        entries = sorted(PROFILE_ROOT.glob("platform-profile-*"))
    except OSError:
        return None
    for entry in entries:
        if read_text(entry / "name") == name:
            return entry
    return None


def _thermal_temp(zone_type: str) -> float | None:
    try:
        zones = sorted(THERMAL_ROOT.glob("thermal_zone*"))
    except OSError:
        return None
    for zone in zones:
        if read_text(zone / "type") == zone_type:
            value = read_int(zone / "temp")
            if value is not None and 0 <= value <= 150000:
                return value / 1000.0
    return None


@dataclass
class Snapshot:
    ts: float = 0.0
    cpu_temp: float | None = None
    cpu_max_core: float | None = None
    cpu_usage: float = 0.0
    cpu_per_core: list[float] = field(default_factory=list)
    cpu_freq_mhz: float | None = None
    cpu_name: str = ""
    gpu_temp: float | None = None
    gpu_usage: float | None = None
    gpu_mem_used: float | None = None
    gpu_mem_total: float | None = None
    gpu_power: float | None = None
    gpu_clock: float | None = None
    gpu_name: str = ""
    gpu_source: str = ""
    fan_cpu_rpm: int | None = None
    fan_gpu_rpm: int | None = None
    fan_cpu_boost: int | None = None
    fan_gpu_boost: int | None = None
    fan_cpu_label: str = "CPU Fan"
    fan_gpu_label: str = "GPU Fan"
    profile_raw: str | None = None
    profile_choices: list[str] = field(default_factory=list)
    preset: str | None = None
    manual: bool = False
    model: str = ""
    backend_error: str | None = None


class NvmlGpu:
    """Tiny ctypes binding for the handful of NVML calls we need."""

    class Utilization(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    class Memory(ctypes.Structure):
        _fields_ = [
            ("total", ctypes.c_ulonglong),
            ("free", ctypes.c_ulonglong),
            ("used", ctypes.c_ulonglong),
        ]

    def __init__(self) -> None:
        self._lib = None
        self._handle = None
        self._ok = False
        self._name: str | None = None
        self._last_attempt = 0.0

    @property
    def name(self) -> str:
        return self._name or "NVIDIA GPU"

    def _init(self) -> None:
        self._last_attempt = time.monotonic()
        try:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
        except OSError:
            self._lib = None
            return

        c_void_p = ctypes.c_void_p
        c_uint = ctypes.c_uint
        try:
            lib.nvmlInit_v2.restype = ctypes.c_int
            rc = lib.nvmlInit_v2()
            if rc != 0:
                return
            lib.nvmlDeviceGetHandleByIndex_v2.argtypes = [c_uint, ctypes.POINTER(c_void_p)]
            lib.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
            handle = c_void_p()
            rc = lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle))
            if rc != 0:
                return

            lib.nvmlDeviceGetName.argtypes = [c_void_p, ctypes.c_char_p, c_uint]
            lib.nvmlDeviceGetName.restype = ctypes.c_int
            buf = ctypes.create_string_buffer(128)
            if lib.nvmlDeviceGetName(handle, buf, 128) == 0:
                self._name = buf.value.decode("utf-8", "replace")

            lib.nvmlDeviceGetTemperature.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
            lib.nvmlDeviceGetTemperature.restype = ctypes.c_int
            lib.nvmlDeviceGetUtilizationRates.argtypes = [c_void_p, ctypes.POINTER(self.Utilization)]
            lib.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
            lib.nvmlDeviceGetMemoryInfo.argtypes = [c_void_p, ctypes.POINTER(self.Memory)]
            lib.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
            lib.nvmlDeviceGetPowerUsage.argtypes = [c_void_p, ctypes.POINTER(c_uint)]
            lib.nvmlDeviceGetPowerUsage.restype = ctypes.c_int
            lib.nvmlDeviceGetClockInfo.argtypes = [c_void_p, c_uint, ctypes.POINTER(c_uint)]
            lib.nvmlDeviceGetClockInfo.restype = ctypes.c_int
            lib.nvmlShutdown.argtypes = []
            lib.nvmlShutdown.restype = ctypes.c_int
        except (AttributeError, OSError):
            return

        self._lib = lib
        self._handle = handle
        self._ok = True

    def initialize(self) -> None:
        if not self._ok:
            self._init()

    def read(self) -> dict | None:
        now = time.monotonic()
        if not self._ok:
            if now - self._last_attempt < 20.0:
                return None
            self._init()
            if not self._ok:
                return None

        lib = self._lib
        handle = self._handle
        out: dict = {"source": "NVML", "name": self._name or "NVIDIA GPU"}

        temp = ctypes.c_uint()
        if lib.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(temp)) == 0:
            out["temp"] = float(temp.value)

        util = self.Utilization()
        if lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(util)) == 0:
            out["usage"] = float(util.gpu)
            out["mem_usage"] = float(util.memory)

        mem = self.Memory()
        if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) == 0:
            out["mem_used"] = mem.used / (1024 * 1024)
            out["mem_total"] = mem.total / (1024 * 1024)

        power = ctypes.c_uint()
        if lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(power)) == 0:
            out["power"] = power.value / 1000.0

        clock = ctypes.c_uint()
        if lib.nvmlDeviceGetClockInfo(handle, 1, ctypes.byref(clock)) == 0:
            out["clock"] = float(clock.value)

        return out

    def close(self) -> None:
        if self._ok and self._lib is not None:
            try:
                self._lib.nvmlShutdown()
            except OSError:
                pass
        self._ok = False


class SensorHub:
    """Discovers the Dell/AWCC interfaces once and samples them on demand."""

    def __init__(self) -> None:
        self.alien = _hwmon_by_name("alienware_wmi")
        self.dell_ddv = _hwmon_by_name("dell_ddv")
        self.dell_smm = _hwmon_by_name("dell_smm")
        self.coretemp = _hwmon_by_name("coretemp")
        self.nvme = []
        try:
            for entry in sorted(HWMON_ROOT.glob("hwmon*")):
                if read_text(entry / "name") == "nvme":
                    self.nvme.append(entry)
        except OSError:
            pass

        self.profile_dir = _platform_profile_by_name("alienware-wmi")
        self.acpi_profile = Path("/sys/firmware/acpi/platform_profile")

        self.cpu_name = self._cpu_model_name()
        self.model = read_text(DMI_ROOT / "product_name") or "Dell G15"
        self.vendor = read_text(DMI_ROOT / "sys_vendor") or "Dell Inc."

        self._cpu_temp_path: Path | None = None
        self._cpu_core_paths: list[Path] = []
        self._find_cpu_temps()

        self._fan_cpu_label = read_text(self.alien / "fan1_label") if self.alien else None
        self._fan_gpu_label = read_text(self.alien / "fan2_label") if self.alien else None

        self.nvml = NvmlGpu()
        self.nvml.initialize()
        self._gpu_name = self.nvml.name
        self._gpu_smi_cache: dict | None = None
        self._gpu_smi_time = 0.0
        self._gpu_smi_name: str | None = None

        self._cpu_usage = 0.0
        self._cpu_per_core: list[float] = []
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            pass

        self.backend_error: str | None = None
        if self.alien is None:
            self.backend_error = (
                "未找到 alienware_wmi 风扇接口。请确认内核已加载 alienware_wmi 模块。"
            )
        elif self.profile_dir is None:
            self.backend_error = "未找到 Alienware WMI 性能模式接口，仍可监测温度与风扇。"

    # ------------------------------------------------------------------ setup
    def _cpu_model_name(self) -> str:
        raw = read_text(PROC_ROOT / "cpuinfo")
        if raw:
            m = re.search(r"^model name\s*:\s*(.+)$", raw, re.MULTILINE)
            if m:
                return m.group(1).strip()
        return "Intel CPU"

    def _find_cpu_temps(self) -> None:
        if self.coretemp is not None:
            try:
                for path in sorted(self.coretemp.glob("temp*_input")):
                    label = read_text(path.with_name(path.name.replace("_input", "_label")))
                    if label and "Package" in label:
                        self._cpu_temp_path = path
                    elif label and "Core" in label:
                        self._cpu_core_paths.append(path)
            except OSError:
                pass
            if self._cpu_temp_path is None:
                self._cpu_temp_path = self.coretemp / "temp1_input"

    # ------------------------------------------------------------------ helpers
    def _read_thermal_zone(self, zone_type: str) -> float | None:
        return _thermal_temp(zone_type)

    def _gpu_from_smi(self) -> dict | None:
        """Fallback used only when NVML is unavailable (cached for 3 s)."""
        now = time.monotonic()
        if self._gpu_smi_cache is not None and now - self._gpu_smi_time < 3.0:
            return self._gpu_smi_cache
        self._gpu_smi_time = now
        cmd = [
            "nvidia-smi",
            "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,clocks.sm",
            "--format=csv,noheader,nounits",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            self._gpu_smi_cache = None
            return None
        if res.returncode != 0 or not res.stdout.strip():
            self._gpu_smi_cache = None
            return None
        parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]

        def num(idx: int) -> float | None:
            if idx >= len(parts):
                return None
            try:
                return float(parts[idx])
            except ValueError:
                return None

        data = {
            "source": "nvidia-smi",
            "name": parts[0] if parts else (self._gpu_name or "NVIDIA GPU"),
            "temp": num(1),
            "usage": num(2),
            "mem_used": num(3),
            "mem_total": num(4),
            "power": num(5),
            "clock": num(6),
        }
        self._gpu_smi_cache = data
        if data.get("name"):
            self._gpu_smi_name = str(data["name"])
        return data

    def _profile(self) -> tuple[str | None, list[str]]:
        if self.profile_dir is not None:
            raw = read_text(self.profile_dir / "profile")
            choices_raw = read_text(self.profile_dir / "choices") or ""
        else:
            raw = read_text(self.acpi_profile)
            choices_raw = read_text(Path(str(self.acpi_profile) + "_choices")) or ""
        choices = choices_raw.split()
        return raw, choices

    # ------------------------------------------------------------------ sample
    def sample(self) -> Snapshot:
        snap = Snapshot(ts=time.time())
        snap.cpu_name = self.cpu_name
        snap.model = self.model

        # CPU usage / clocks -------------------------------------------------
        try:
            snap.cpu_usage = psutil.cpu_percent(interval=None)
            snap.cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            snap.cpu_usage = 0.0
            snap.cpu_per_core = []
        try:
            freq = psutil.cpu_freq()
            if freq and freq.current:
                snap.cpu_freq_mhz = float(freq.current)
        except Exception:
            pass

        # CPU temperature ----------------------------------------------------
        if self._cpu_temp_path is not None:
            snap.cpu_temp = read_millideg(self._cpu_temp_path)
        if snap.cpu_temp is None:
            snap.cpu_temp = self._read_thermal_zone("x86_pkg_temp")
        if snap.cpu_temp is None:
            snap.cpu_temp = self._read_thermal_zone("TCPU")
        core_temps = [t for t in (read_millideg(p) for p in self._cpu_core_paths) if t is not None]
        if core_temps:
            snap.cpu_max_core = max(core_temps)
        elif snap.cpu_temp is not None:
            snap.cpu_max_core = snap.cpu_temp

        # GPU ---------------------------------------------------------------
        gpu = self.nvml.read()
        if gpu is None:
            gpu = self._gpu_from_smi()
        if gpu:
            snap.gpu_temp = gpu.get("temp")
            snap.gpu_usage = gpu.get("usage")
            snap.gpu_mem_used = gpu.get("mem_used")
            snap.gpu_mem_total = gpu.get("mem_total")
            snap.gpu_power = gpu.get("power")
            snap.gpu_clock = gpu.get("clock")
            snap.gpu_name = gpu.get("name") or self._gpu_name
            snap.gpu_source = gpu.get("source", "NVML")
        else:
            snap.gpu_name = self._gpu_smi_name or self._gpu_name
            snap.gpu_source = "hwmon"

        if snap.gpu_temp is None and self.alien is not None:
            snap.gpu_temp = read_millideg(self.alien / "temp2_input")
        if snap.gpu_temp is None and self.dell_ddv is not None:
            snap.gpu_temp = read_millideg(self.dell_ddv / "temp4_input")
        if snap.gpu_temp is None:
            snap.gpu_temp = self._read_thermal_zone("VRAM") or self._read_thermal_zone("TVGA")

        # Fans --------------------------------------------------------------
        if self.alien is not None:
            snap.fan_cpu_rpm = read_int(self.alien / "fan1_input")
            snap.fan_gpu_rpm = read_int(self.alien / "fan2_input")
            snap.fan_cpu_boost = read_int(self.alien / "fan1_boost")
            snap.fan_gpu_boost = read_int(self.alien / "fan2_boost")
            snap.fan_cpu_label = self._fan_cpu_label or "CPU Fan"
            snap.fan_gpu_label = self._fan_gpu_label or "GPU Fan"
        if snap.fan_cpu_rpm is None and self.dell_smm is not None:
            snap.fan_cpu_rpm = read_int(self.dell_smm / "fan1_input")
            snap.fan_gpu_rpm = read_int(self.dell_smm / "fan2_input")
            snap.fan_cpu_label = "CPU Fan"
            snap.fan_gpu_label = "GPU Fan"
        for attr, label in (("fan_cpu_rpm", "CPU"), ("fan_gpu_rpm", "GPU")):
            value = getattr(snap, attr)
            if value is not None and not (0 <= value <= 20000):
                setattr(snap, attr, None)

        # Mode --------------------------------------------------------------
        raw, choices = self._profile()
        snap.profile_raw = raw
        snap.profile_choices = choices
        snap.preset = PROFILE_TO_PRESET.get(raw or "")
        snap.manual = raw == "custom"
        snap.backend_error = self.backend_error

        return snap

    def close(self) -> None:
        self.nvml.close()

    # ------------------------------------------------------------------ info
    def system_info(self) -> dict:
        mem = psutil.virtual_memory()
        gpu = self._gpu_smi_name or self._gpu_name
        return {
            "model": self.model,
            "vendor": self.vendor,
            "cpu": self.cpu_name,
            "cores": psutil.cpu_count(logical=False) or 0,
            "threads": psutil.cpu_count(logical=True) or 0,
            "gpu": gpu,
            "memory_gb": round(mem.total / (1024**3), 1),
            "kernel": os.uname().release,
            "fan_backend": "alienware_wmi" if self.alien else ("dell_smm" if self.dell_smm else "none"),
        }


class DemoSensors:
    """Plausible animated data for screenshots / UI development."""

    def __init__(self) -> None:
        self.t0 = time.time()
        self.nvml = None
        try:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)
        except Exception:
            pass
        self.cpu_name = "13th Gen Intel(R) Core(TM) i7-13650HX"
        self.model = "Dell G15 5530"

    def sample(self) -> Snapshot:
        t = time.time() - self.t0
        cpu_temp = 62 + 11 * math.sin(t / 11.0) + 4 * math.sin(t / 3.1)
        gpu_temp = 57 + 9 * math.sin(t / 13.0 + 1.2)
        cpu_usage = max(3.0, min(97.0, 34 + 25 * math.sin(t / 5.0) + 8 * math.sin(t / 1.7)))
        gpu_usage = max(0.0, min(99.0, 55 + 34 * math.sin(t / 7.0 + 0.7)))
        fan_cpu = int(3000 + 1300 * (cpu_temp - 55) / 25 + 120 * math.sin(t / 2.0))
        fan_gpu = int(3000 + 1300 * (gpu_temp - 50) / 25 + 120 * math.cos(t / 2.3))
        per_core = [
            max(0.0, min(100.0, cpu_usage + 26 * math.sin(t / 2.0 + i * 0.9)))
            for i in range(20)
        ]
        return Snapshot(
            ts=time.time(),
            cpu_temp=cpu_temp,
            cpu_max_core=cpu_temp + 5,
            cpu_usage=cpu_usage,
            cpu_per_core=per_core,
            cpu_freq_mhz=3200 + 500 * math.sin(t / 4.0),
            cpu_name=self.cpu_name,
            gpu_temp=gpu_temp,
            gpu_usage=gpu_usage,
            gpu_mem_used=4300 + 800 * math.sin(t / 9.0),
            gpu_mem_total=8188,
            gpu_power=55 + 20 * math.sin(t / 6.0),
            gpu_clock=2400 + 180 * math.sin(t / 5.0),
            gpu_name="NVIDIA GeForce RTX 4060 Laptop GPU",
            gpu_source="NVML",
            fan_cpu_rpm=fan_cpu,
            fan_gpu_rpm=fan_gpu,
            fan_cpu_boost=int(max(0, min(255, (fan_cpu - 3000) / 8))),
            fan_gpu_boost=int(max(0, min(255, (fan_gpu - 3000) / 8))),
            fan_cpu_label="CPU Fan",
            fan_gpu_label="GPU Fan",
            profile_raw="performance",
            profile_choices=["low-power", "quiet", "balanced", "balanced-performance", "performance", "custom"],
            preset="performance",
            manual=False,
            model=self.model,
            backend_error=None,
        )

    def system_info(self) -> dict:
        return {
            "model": self.model,
            "vendor": "Dell Inc.",
            "cpu": self.cpu_name,
            "cores": 14,
            "threads": 20,
            "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU",
            "memory_gb": 32.0,
            "kernel": os.uname().release,
            "fan_backend": "demo",
        }

    def close(self) -> None:
        pass
