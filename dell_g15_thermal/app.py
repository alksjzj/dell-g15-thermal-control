#!/usr/bin/env python3
"""Dell G15 温控中心 — real-time thermal, fan and preset control."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dell_g15_thermal.control import (  # noqa: E402
    HelperClient,
    HelperError,
    Settings,
    autostart_path,
    install_command,
    no_new_privileges,
    preset_name,
    set_autostart,
    set_preset,
)
from dell_g15_thermal.sensors import DemoSensors, SensorHub  # noqa: E402
from dell_g15_thermal.widgets import (  # noqa: E402
    AMBER,
    CYAN,
    VIOLET,
    ArcGauge,
    CompactView,
    FanControlCard,
    Toast,
    TrendChart,
    make_card,
)

VERSION = "1.0.0"
APP_ROOT = Path(__file__).resolve().parent.parent
ASSETS = Path(__file__).resolve().parent / "assets"
ICON = ASSETS / "icon.svg"
CSS = Path(__file__).resolve().parent / "theme.css"


def short_cpu(name: str) -> str:
    m = re.search(r"(i[3579]-\d+[A-Z]*)", name)
    if m:
        return m.group(1)
    return name.replace("(R)", "").replace("(TM)", "").strip()


def short_gpu(name: str) -> str:
    m = re.search(r"(RTX|GTX)\s*\d+\s*(Ti|Super)?(\s*Laptop)?", name)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip()
    return name


def load_css() -> None:
    try:
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
    except Exception:
        pass
    try:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(CSS))
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
    except Exception as exc:  # pragma: no cover - css is validated by eye
        print(f"warning: could not load CSS: {exc}", file=sys.stderr)


class MainWindow(Gtk.Window):
    def __init__(
        self,
        demo: bool = False,
        geometry: str | None = None,
        screenshot: str | None = None,
        screenshot_wait: int = 20,
        start_compact: bool = False,
    ) -> None:
        super().__init__(title="Dell G15 温控中心")
        self.demo = demo
        self._screenshot = screenshot
        self.settings = Settings.load()
        self.sensors = DemoSensors() if demo else SensorHub()
        self.info = self.sensors.system_info()
        self.helper = HelperClient()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dellg15-sample")
        # One control worker keeps fan/mode writes in submission order, so the
        # last slider position always wins even if the user drags quickly.
        self.control_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dellg15-control")

        self.snapshot = None
        self._sample_count = 0
        self._sampling = False
        self._closing = False
        self._tick_source = None
        self._helper_ready = False
        self._last_helper_check = 0.0
        self._mode_busy = False
        self._manual_active = False
        self._protect_active = False
        self._protect_max = False
        self._last_non_manual_preset = self.settings.last_preset
        self._user_cpu_pct = 50.0
        self._user_gpu_pct = 50.0
        self._last_warning = ""
        self._backend_warned = False
        self._install_busy = False
        self._install_cancelled = False
        self._install_proc = None
        self._autostart_item = None
        self._compact = False
        self._full_size: tuple[int, int] | None = None
        self._compact_size: tuple[int, int] = (480, 260)

        self.set_wmclass("dell-g15-thermal", "Dell G15 Thermal")
        self.set_title("Dell G15 温控中心")
        try:
            self.set_icon_from_file(str(ICON))
        except Exception:
            pass

        saved_w = min(1600, int(self.settings.window_width or 1280))
        saved_h = min(1000, int(self.settings.window_height or 800))
        workarea = self._screen_workarea()
        if workarea is not None:
            max_w = max(1130, int(workarea.width * 0.90) - 20)
            max_h = max(650, int(workarea.height * 0.90) - 20)
        else:
            max_w, max_h = 1600, 1000
        width = min(max(1130, saved_w), max_w)
        height = min(max(650, saved_h), max_h)
        self._full_size = (width, height)
        self.set_default_size(width, height)
        self.set_size_request(1130, 650)
        self.set_resizable(True)
        if geometry:
            self._apply_geometry(geometry)
        else:
            self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("size-allocate", self._on_window_size_allocate)

        self._build_header()
        self._build_content()
        self.connect("destroy", self._on_destroy)

        self._helper_ready = self.helper.available(force=True)
        self._last_helper_check = time.monotonic()
        self.fan_card.set_helper_ready(self._helper_ready)
        self._update_status(None)
        self.show_all()
        self.fan_card.install_revealer.set_reveal_child(not self._helper_ready)

        GLib.idle_add(lambda: (self._force_sample(), False)[1])
        self._tick_source = GLib.timeout_add(1000, self._tick)

        self._run_autostart_state()

        if start_compact:
            GLib.idle_add(self._start_in_compact)

        if screenshot:
            self.set_keep_above(True)
            GLib.timeout_add(250, self._present_for_screenshot)
            GLib.timeout_add_seconds(max(2, screenshot_wait), self._capture_window, screenshot)

    # ------------------------------------------------------------------ layout
    def _screen_workarea(self):
        """Work area of the primary monitor, used to keep the window on-screen."""
        try:
            display = Gdk.Display.get_default()
            if display is None:
                return None
            monitor = display.get_primary_monitor()
            if monitor is None and display.get_n_monitors() > 0:
                monitor = display.get_monitor(0)
            return monitor.get_workarea() if monitor is not None else None
        except Exception:
            return None

    def _is_fullscreen_state(self) -> bool:
        win = self.get_window()
        return bool(win is not None and (win.get_state() & Gdk.WindowState.FULLSCREEN))

    def _on_window_size_allocate(self, _widget, alloc) -> None:
        # Keep the two window sizes separate so the mini window never becomes
        # the size that is restored on the next full launch.
        if self._compact:
            self._compact_size = self.get_size()
        elif not self.is_maximized() and not self._is_fullscreen_state():
            # get_size() is the client size; alloc includes CSD shadows.
            self._full_size = self.get_size()

    def _toggle_maximize(self) -> None:
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def _present_for_screenshot(self) -> bool:
        self.present()
        self.set_keep_above(True)
        return False

    def _capture_window(self, path: str) -> bool:
        try:
            win = self.get_window()
            if win is not None:
                pix = Gdk.pixbuf_get_from_window(win, 0, 0, win.get_width(), win.get_height())
                if pix is not None:
                    pix.savev(str(path), "png", [], [])
                    print(f"screenshot saved: {path}", flush=True)
        except Exception as exc:
            print(f"screenshot failed: {exc}", flush=True)
        finally:
            self.close()
        return False

    def _apply_geometry(self, geometry: str) -> None:
        m = re.match(r"(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?$", geometry.strip())
        if not m:
            return
        w, h = int(m.group(1)), int(m.group(2))
        self.resize(w, h)
        if m.group(3) is not None and m.group(4) is not None:
            self.move(int(m.group(3)), int(m.group(4)))

    def _build_header(self) -> None:
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_has_subtitle(False)
        hb.get_style_context().add_class("app-headerbar")

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = self._pixbuf_image(ICON, 32)
        if icon is not None:
            title_box.pack_start(icon, False, False, 0)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.title_label = Gtk.Label(label="Dell G15 温控中心", xalign=0)
        self.title_label.get_style_context().add_class("app-title")
        title = self.title_label
        self.header_sub = Gtk.Label(label=self._subtitle_text(), xalign=0)
        self.header_sub.get_style_context().add_class("app-sub")
        labels.pack_start(title, False, False, 0)
        labels.pack_start(self.header_sub, False, False, 0)
        title_box.pack_start(labels, False, False, 0)
        title_event = Gtk.EventBox()
        title_event.set_visible_window(False)
        title_event.add(title_box)
        title_event.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        title_event.connect("button-press-event", self._on_title_press)
        hb.set_custom_title(title_event)

        mode_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        mode_group.get_style_context().add_class("mode-group")
        self.mode_buttons: dict[str, Gtk.ToggleButton] = {}
        self._mode_handler_ids: dict[str, int] = {}
        for preset, label in (("performance", "性能"), ("balanced", "平衡"), ("saver", "节电")):
            btn = Gtk.ToggleButton(label=label)
            btn.get_style_context().add_class("mode-btn")
            btn.get_style_context().add_class(f"mode-{preset}")
            btn.set_tooltip_text(
                {
                    "performance": "Dell 性能模式：最高功耗与风扇曲线",
                    "balanced": "Dell 平衡模式：性能与噪音兼顾",
                    "saver": "Dell 节电/安静模式：降低功耗与风扇转速",
                }[preset]
            )
            handler_id = btn.connect("clicked", self._on_mode_clicked, preset)
            mode_group.pack_start(btn, False, False, 0)
            self.mode_buttons[preset] = btn
            self._mode_handler_ids[preset] = handler_id
        self.mode_group = mode_group
        hb.pack_end(mode_group)

        menu_btn = Gtk.MenuButton()
        menu_btn.set_image(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON))
        menu_btn.get_style_context().add_class("menu-button")
        menu_btn.set_tooltip_text("设置与工具")
        menu_btn.set_popover(self._build_menu())
        self.menu_btn = menu_btn
        hb.pack_end(menu_btn)

        self.compact_btn = Gtk.Button(label="迷你")
        self.compact_btn.get_style_context().add_class("ghost")
        self.compact_btn.set_tooltip_text("迷你模式：只显示温度和两个风扇转速")
        self.compact_btn.connect("clicked", lambda *_: self._toggle_compact())
        hb.pack_end(self.compact_btn)

        self.set_titlebar(hb)

    def _build_menu(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.set_border_width(4)

        def item(label: str) -> Gtk.ModelButton:
            button = Gtk.ModelButton(label=label)
            box.pack_start(button, False, False, 0)
            return button

        redetect = item("重新检测硬件")
        redetect.connect("clicked", lambda *_: self._redetect())
        service = item("风扇控制服务…")
        service.connect("clicked", lambda *_: self._install_helper())
        self._autostart_item = item("开机自启动：关")
        self._autostart_item.connect("clicked", lambda *_: self._toggle_autostart())
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        maximize = item("最大化 / 还原窗口")
        maximize.connect("clicked", lambda *_: self._toggle_maximize())
        mini = item("迷你模式（温度 / 风扇）")
        mini.connect("clicked", lambda *_: self._toggle_compact())
        minimize = item("最小化到任务栏")
        minimize.connect("clicked", lambda *_: self.iconify())
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        about = item("关于 Dell G15 温控中心")
        about.connect("clicked", lambda *_: self._show_about())
        quit_item = item("退出")
        quit_item.connect("clicked", lambda *_: self.close())

        box.show_all()
        popover.add(box)
        return popover

    def _build_content(self) -> None:
        root = Gtk.Grid()
        root.set_row_spacing(12)
        root.set_column_spacing(0)
        root.set_margin_start(16)
        root.set_margin_end(16)
        root.set_margin_top(12)
        root.set_margin_bottom(10)
        root.set_hexpand(True)
        root.set_vexpand(True)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        top.set_hexpand(True)
        root.attach(top, 0, 0, 1, 1)

        self.cpu_gauge = ArcGauge("CPU 处理器", CYAN)
        cpu_card = make_card(self.cpu_gauge, spacing=0)
        cpu_card.set_size_request(302, -1)
        top.pack_start(cpu_card, False, False, 0)

        self.gpu_gauge = ArcGauge("GPU 显卡", VIOLET)
        gpu_card = make_card(self.gpu_gauge, spacing=0)
        gpu_card.set_size_request(302, -1)
        top.pack_start(gpu_card, False, False, 0)

        self.fan_card = FanControlCard(
            self.settings,
            on_apply=self._on_fan_apply,
            on_restore=self._restore_preset,
            on_install=self._install_helper,
            on_setting_change=self._on_setting_change,
        )
        top.pack_start(self.fan_card, True, True, 0)

        charts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        charts.set_hexpand(True)
        charts.set_vexpand(True)
        root.attach(charts, 0, 1, 1, 1)

        self.temp_chart = TrendChart(
            "温度趋势 · 60 秒",
            "°C",
            [
                {"key": "cpu", "label": "CPU", "color": CYAN},
                {"key": "gpu", "label": "GPU", "color": VIOLET},
            ],
            history=60,
        )
        self.usage_chart = TrendChart(
            "占用率趋势 · 60 秒",
            "%",
            [
                {"key": "cpu", "label": "CPU", "color": CYAN},
                {"key": "gpu", "label": "GPU", "color": VIOLET},
            ],
            history=60,
        )
        temp_card = make_card(self.temp_chart, spacing=0)
        usage_card = make_card(self.usage_chart, spacing=0)
        temp_card.set_vexpand(True)
        usage_card.set_vexpand(True)
        charts.pack_start(temp_card, True, True, 0)
        charts.pack_start(usage_card, True, True, 0)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        status.get_style_context().add_class("statusbar")
        self.status_left = Gtk.Label(label="", xalign=0)
        self.status_center = Gtk.Label(label="", xalign=0.5)
        self.status_right = Gtk.Label(label="", xalign=1)
        for lbl in (self.status_left, self.status_center, self.status_right):
            lbl.get_style_context().add_class("statusbar")
        status.pack_start(self.status_left, False, False, 0)
        status.pack_start(self.status_center, True, True, 0)
        status.pack_end(self.status_right, False, False, 0)
        self.toast = Toast()
        root.attach(self.toast, 0, 2, 1, 1)
        root.attach(status, 0, 3, 1, 1)

        self.compact_view = CompactView()
        self.stack = Gtk.Stack()
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(150)
        self.stack.add_named(root, "full")
        self.stack.add_named(self.compact_view, "compact")
        self.stack.set_visible_child_name("full")
        self.add(self.stack)

    # ------------------------------------------------------------------ mini mode
    def _workarea_for_window(self):
        try:
            display = Gdk.Display.get_default()
            win = self.get_window()
            if display is None:
                return None
            monitor = None
            if win is not None:
                monitor = display.get_monitor_at_window(win)
            if monitor is None:
                monitor = display.get_primary_monitor()
            return monitor.get_workarea() if monitor is not None else None
        except Exception:
            return None

    def _ensure_on_screen(self) -> bool:
        workarea = self._workarea_for_window()
        if workarea is None or self.get_window() is None:
            return False
        try:
            x, y = self.get_position()
            w, h = self.get_size()
            max_x = workarea.x + workarea.width - w - 24
            max_y = workarea.y + workarea.height - h - 60
            nx = min(max(workarea.x, x), max(workarea.x, max_x))
            ny = min(max(workarea.y, y), max(workarea.y, max_y))
            if (nx, ny) != (x, y):
                self.move(nx, ny)
        except Exception:
            pass
        return False

    def _start_in_compact(self) -> bool:
        self._set_compact(True)
        return False

    def _toggle_compact(self) -> None:
        self._set_compact(not self._compact)

    def _set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        if compact:
            if not self.is_maximized() and not self._is_fullscreen_state():
                self._full_size = self.get_size()
            if self.is_maximized():
                self.unmaximize()
            self._compact = True
            self.stack.set_visible_child_name("compact")
            self.mode_group.hide()
            self.menu_btn.hide()
            self.header_sub.hide()
            self.title_label.set_text("Dell G15 · 迷你")
            self.compact_btn.set_label("展开")
            self.compact_btn.set_tooltip_text("展开完整界面")
            self.set_size_request(430, 220)
            self.resize(460, 230)
        else:
            self._compact = False
            self.stack.set_visible_child_name("full")
            self.mode_group.show()
            self.menu_btn.show()
            self.header_sub.show()
            self.title_label.set_text("Dell G15 温控中心")
            self.compact_btn.set_label("迷你")
            self.compact_btn.set_tooltip_text("迷你模式：只显示温度和两个风扇转速")
            self.set_size_request(1130, 650)
            w, h = self._full_size or (1280, 800)
            # Never restore larger than the monitor's usable area.
            workarea = self._workarea_for_window()
            if workarea is not None:
                w = min(w, max(1130, workarea.width - 40))
                h = min(h, max(650, workarea.height - 40))
            self.resize(w, h)
        GLib.timeout_add(120, self._ensure_on_screen)

    # ------------------------------------------------------------------ helpers
    def _pixbuf_image(self, path: Path, size: int):
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), size, size, True)
            return Gtk.Image.new_from_pixbuf(pix)
        except Exception:
            return None

    def _subtitle_text(self) -> str:
        return (
            f"{self.info.get('model', 'Dell G15')} · {short_cpu(self.info.get('cpu', ''))}"
            f" · {short_gpu(self.info.get('gpu', ''))}"
        )

    def _update_status(self, snap) -> None:
        stamp = time.strftime("%H:%M:%S")
        if self._helper_ready:
            fan = '<span foreground="#34d399">●</span> 风扇控制已授权'
        else:
            fan = '<span foreground="#fbbf24">●</span> 风扇控制只读'
        self.status_left.set_markup(
            f'<span foreground="#34d399">●</span> 实时监测中   {fan}'
        )
        src = "hwmon · NVML · AWCC" if not self.demo else "演示数据"
        cores = self.info.get("cores") or 0
        threads = self.info.get("threads") or 0
        self.status_center.set_text(f"{cores} 核 {threads} 线程 · 数据源 {src}")
        self.status_right.set_text(f"{stamp} · v{VERSION}")

    # ------------------------------------------------------------------ sampling
    def _tick(self) -> bool:
        if self._closing:
            return False
        if not self._sampling:
            self._sampling = True
            future = self.executor.submit(self.sensors.sample)
            future.add_done_callback(lambda f: GLib.idle_add(self._apply_future, f))
        return True

    def _apply_future(self, future) -> bool:
        self._sampling = False
        if self._closing:
            return False
        try:
            snap = future.result()
        except Exception as exc:
            self.toast.show(f"读取传感器失败：{exc}", error=True, timeout=5)
            return False
        self._apply_snapshot(snap)
        return False

    def _force_sample(self) -> None:
        if not self._sampling and not self._closing:
            self._tick()

    def _schedule_sample(self, delay_ms: int = 250) -> None:
        GLib.timeout_add(delay_ms, lambda: (self._force_sample(), False)[1])

    def _apply_snapshot(self, snap) -> None:
        self.snapshot = snap
        self._sample_count += 1
        # When opening on an existing manual profile, initialise the saved
        # user values from the current firmware boost.
        if snap.manual and not self._manual_active and snap.fan_cpu_boost is not None:
            self._user_cpu_pct = snap.fan_cpu_boost / 255.0 * 100.0
            self._user_gpu_pct = (snap.fan_gpu_boost or snap.fan_cpu_boost) / 255.0 * 100.0
        self._manual_active = bool(snap.manual)

        # CPU ------------------------------------------------------------
        cpu_sub = ""
        if snap.cpu_freq_mhz:
            cpu_sub = f"{snap.cpu_freq_mhz / 1000:.2f} GHz"
        if snap.cpu_max_core is not None:
            cpu_sub += f" · 峰值 {snap.cpu_max_core:.0f}°C"
        self.cpu_gauge.set_values(snap.cpu_temp, snap.cpu_usage, cpu_sub, snap.cpu_per_core)

        # GPU ------------------------------------------------------------
        if snap.gpu_usage is None:
            gpu_sub = "占用不可用 · 温度来自 hwmon"
        else:
            bits = []
            if snap.gpu_power:
                bits.append(f"{snap.gpu_power:.0f} W")
            if snap.gpu_clock:
                bits.append(f"{snap.gpu_clock:.0f} MHz")
            if snap.gpu_mem_used is not None and snap.gpu_mem_total:
                bits.append(f"{snap.gpu_mem_used / 1024:.1f}/{snap.gpu_mem_total / 1024:.1f} GB")
            gpu_sub = " · ".join(bits)
        self.gpu_gauge.set_values(snap.gpu_temp, snap.gpu_usage, gpu_sub)

        # Helper status once every few seconds.
        now = time.monotonic()
        if now - self._last_helper_check > 4.0:
            self._last_helper_check = now
            ready = self.helper.available(force=True)
            if ready != self._helper_ready:
                self._helper_ready = ready
                self.fan_card.set_helper_ready(ready)
                self._update_status(snap)
        self.fan_card.set_protection_active(self._protect_active)
        self.fan_card.set_live(snap, self._helper_ready)
        if getattr(self, "compact_view", None) is not None:
            self.compact_view.update(snap)

        # Charts start after the priming sample (psutil needs a delta).
        if self._sample_count > 1:
            self.temp_chart.push({"cpu": snap.cpu_temp, "gpu": snap.gpu_temp})
            self.usage_chart.push({"cpu": snap.cpu_usage, "gpu": snap.gpu_usage})

        if not self._mode_busy:
            self._set_mode_active(snap.preset)
        if snap.preset and not snap.manual:
            self._last_non_manual_preset = snap.preset

        if snap.backend_error and not self._backend_warned:
            self._backend_warned = True
            self.toast.show(snap.backend_error, error=True, timeout=8)

        self._maybe_protect(snap)
        self._update_status(snap)

        if self.info.get("gpu") in ("NVIDIA GPU", "") and snap.gpu_name:
            self.info["gpu"] = snap.gpu_name
            self.header_sub.set_text(self._subtitle_text())

    # ------------------------------------------------------------------ modes
    def _on_title_press(self, _widget, event) -> bool:
        if event.type == Gdk.EventType._2BUTTON_PRESS and event.button == 1:
            self._toggle_maximize()
            return True
        return False

    def _set_mode_active(self, preset: str | None) -> None:
        for key, button in self.mode_buttons.items():
            want = key == preset
            if button.get_active() != want:
                # set_active() can emit "clicked" on some GTK versions; block
                # our handler so a sensor refresh can never trigger a switch.
                handler_id = self._mode_handler_ids.get(key)
                if handler_id is not None:
                    button.handler_block(handler_id)
                button.set_active(want)
                if handler_id is not None:
                    button.handler_unblock(handler_id)

    def _force_button_active(self, button: Gtk.ToggleButton, preset: str) -> None:
        handler_id = self._mode_handler_ids.get(preset)
        if handler_id is not None:
            button.handler_block(handler_id)
        button.set_active(True)
        if handler_id is not None:
            button.handler_unblock(handler_id)

    def _on_mode_clicked(self, button: Gtk.ToggleButton, preset: str) -> None:
        if self._mode_busy:
            self._force_button_active(button, preset)
            return
        if not button.get_active():
            # Clicking the already-active button should not turn it off.
            self._force_button_active(button, preset)
            return
        self._start_mode_switch(preset)

    def _start_mode_switch(self, preset: str) -> None:
        if self._mode_busy:
            return
        if (
            self.snapshot is not None
            and self.snapshot.preset == preset
            and not self.snapshot.manual
            and not self._manual_active
        ):
            self._set_mode_active(preset)
            self.toast.show(f"当前已经是{preset_name(preset)}模式")
            return
        self._mode_busy = True
        self._set_mode_active(preset)
        for button in self.mode_buttons.values():
            button.set_sensitive(False)
        self.fan_card.set_busy(True)
        helper = self.helper if self._helper_ready else None
        future = self.control_executor.submit(set_preset, preset, helper)
        future.add_done_callback(lambda f: GLib.idle_add(self._mode_done, f, preset))

    def _mode_done(self, future, preset: str) -> bool:
        self._mode_busy = False
        for button in self.mode_buttons.values():
            button.set_sensitive(True)
        self.fan_card.set_busy(False)
        try:
            ok, message = future.result()
        except Exception as exc:
            ok, message = False, str(exc)
        if ok:
            self.settings.last_preset = preset
            self.settings.save()
            self._last_non_manual_preset = preset
            self._protect_active = False
            self._protect_max = False
            self._manual_active = False
            self.fan_card.set_protection_active(False)
            self.toast.show(message)
            self._schedule_sample(250)
        else:
            self.toast.show(message, error=True, timeout=6)
            if self.snapshot is not None:
                self._set_mode_active(self.snapshot.preset)
        return False

    def _restore_preset(self) -> None:
        preset = self._last_non_manual_preset or self.settings.last_preset
        if preset not in ("performance", "balanced", "saver"):
            preset = "balanced"
        self._start_mode_switch(preset)

    # ------------------------------------------------------------------ fans
    def _on_fan_apply(self, cpu_pct: int, gpu_pct: int) -> None:
        if not self._helper_ready:
            self.toast.show("请先启用风扇控制服务", error=True)
            self._install_helper()
            return
        if self._closing:
            return
        self._user_cpu_pct = cpu_pct
        self._user_gpu_pct = gpu_pct
        cpu = round(cpu_pct * 255.0 / 100.0)
        gpu = round(gpu_pct * 255.0 / 100.0)
        if self._protect_active:
            cpu = max(cpu, 204)
            gpu = max(gpu, 204)
        self._manual_active = True
        self._send_boosts(cpu, gpu)

    def _send_boosts(self, cpu: int, gpu: int, profile: str | None = "custom") -> None:
        if not self._helper_ready:
            return
        future = self.control_executor.submit(self.helper.set_boosts, cpu, gpu, profile)
        future.add_done_callback(lambda f: GLib.idle_add(self._boost_done, f))

    def _boost_done(self, future) -> bool:
        try:
            reply = future.result()
        except HelperError as exc:
            self.toast.show(str(exc), error=True, timeout=5)
            self._helper_ready = self.helper.available(force=True)
            self.fan_card.set_helper_ready(self._helper_ready)
            return False
        except Exception as exc:
            self.toast.show(f"风扇写入失败：{exc}", error=True, timeout=5)
            return False
        warning = reply.get("warning")
        if warning and warning != self._last_warning:
            self._last_warning = warning
            self.toast.show(warning, timeout=7)
        self._manual_active = True
        self._schedule_sample(180)
        return False

    def _maybe_protect(self, snap) -> None:
        if not self.settings.protect:
            if self._protect_active:
                self._protect_active = False
                self._protect_max = False
                self.fan_card.set_protection_active(False)
            return
        if not (self._helper_ready and self._manual_active and not self._mode_busy):
            return
        temps = [t for t in (snap.cpu_temp, snap.gpu_temp) if t is not None]
        if not temps:
            return
        hot = max(temps)
        if not self._protect_active:
            if hot >= 88.0:
                self._protect_active = True
                self._protect_max = False
                self._user_cpu_pct, self._user_gpu_pct = self.fan_card.values()
                self.fan_card.set_protection_active(True)
                self._send_boosts(204, 204)
                self.toast.show(f"温度达到 {hot:.0f}°C，已自动提升风扇转速")
        else:
            if hot <= 80.0:
                self._protect_active = False
                self._protect_max = False
                self.fan_card.set_protection_active(False)
                cpu_pct, gpu_pct = self._user_cpu_pct, self._user_gpu_pct
                self.fan_card.set_values(cpu_pct, gpu_pct)
                cpu = round(cpu_pct * 255.0 / 100.0)
                gpu = round(gpu_pct * 255.0 / 100.0)
                self._send_boosts(cpu, gpu)
                self.toast.show("温度已回落，恢复手动风扇设定")
            elif hot >= 93.0 and not self._protect_max:
                self._protect_max = True
                self._send_boosts(255, 255)
                self.toast.show(f"温度达到 {hot:.0f}°C，风扇已提升至满速", error=True)

    def _on_setting_change(self) -> None:
        self.settings.save()
        if not self.settings.protect and self._protect_active:
            self._protect_active = False
            self._protect_max = False
            self.fan_card.set_protection_active(False)
            if self._helper_ready:
                cpu_pct, gpu_pct = self._user_cpu_pct, self._user_gpu_pct
                self.fan_card.set_values(cpu_pct, gpu_pct)
                cpu = round(cpu_pct * 255.0 / 100.0)
                gpu = round(gpu_pct * 255.0 / 100.0)
                self._send_boosts(cpu, gpu)

    # ------------------------------------------------------------------ install
    def _install_helper(self) -> None:
        if self._install_busy:
            return
        script = APP_ROOT / "install.sh"
        if not script.exists():
            self._show_text_dialog(
                "找不到安装脚本",
                f"未找到 {script}。请确认应用文件完整。",
                error=True,
            )
            return
        if no_new_privileges():
            self._show_text_dialog(
                "当前启动环境不允许提权",
                "本进程被标记为 NoNewPrivs=1，无法弹出系统授权窗口。\n\n"
                "请关闭本窗口，从桌面图标或应用菜单重新打开后再试；"
                "也可以在终端中运行：\n\n"
                f"sudo bash {script}",
                error=True,
            )
            return
        if shutil.which("pkexec") is None:
            self._show_text_dialog(
                "缺少图形授权工具",
                "未找到 pkexec。请安装 policykit-1，或在终端中运行：\n\n"
                f"sudo bash {script}",
                error=True,
            )
            return

        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="启用风扇手动控制？",
        )
        dialog.format_secondary_text(
            "将安装一个仅本机可用的 root 助手服务，用于写入风扇增强值。\n"
            "服务不联网、不采集数据，只接受当前用户通过本机 Unix 套接字的请求。\n\n"
            "接下来系统会要求输入管理员密码。"
        )
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            return

        self._install_busy = True
        self._install_cancelled = False
        cmd = install_command(script, getpass.getuser())
        assert cmd is not None
        try:
            self._install_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env={**os.environ, "DELL_G15_APP": "1"},
            )
        except OSError as exc:
            self._install_busy = False
            self._show_text_dialog("无法启动安装", str(exc), error=True)
            return

        progress = Gtk.Dialog(title="启用风扇控制", transient_for=self, modal=True)
        progress.set_default_size(440, 150)
        progress.set_deletable(False)
        area = progress.get_content_area()
        area.set_spacing(10)
        area.set_border_width(18)
        spinner = Gtk.Spinner()
        spinner.start()
        spinner.set_halign(Gtk.Align.CENTER)
        label = Gtk.Label(
            label="请在系统授权窗口中输入管理员密码…\n安装完成后此窗口会自动关闭。",
            justify=Gtk.Justification.CENTER,
        )
        area.pack_start(spinner, False, False, 0)
        area.pack_start(label, False, False, 0)
        cancel = progress.add_button("取消", Gtk.ResponseType.CANCEL)
        progress.show_all()

        def on_response(dlg, response_id):
            if response_id == Gtk.ResponseType.CANCEL and self._install_proc is not None:
                self._install_cancelled = True
                try:
                    self._install_proc.terminate()
                except OSError:
                    pass
                dlg.destroy()
                self._install_busy = False

        progress.connect("response", on_response)

        future = self.control_executor.submit(self._collect_install)
        future.add_done_callback(lambda f: GLib.idle_add(self._install_done, f, progress))

    def _collect_install(self):
        proc = self._install_proc
        if proc is None:
            return -1, ""
        out, _ = proc.communicate()
        return proc.returncode, out or ""

    def _install_done(self, future, progress: Gtk.Dialog) -> bool:
        self._install_busy = False
        try:
            code, output = future.result()
        except Exception as exc:
            code, output = -1, str(exc)
        try:
            progress.destroy()
        except Exception:
            pass
        if self._install_cancelled:
            self.toast.show("已取消安装")
            return False
        if code == 0:
            self.toast.show("安装完成，正在连接风扇控制服务…")
            self._wait_for_helper(12)
        else:
            text = output.strip() or f"安装进程退出码 {code}（可能已取消授权）"
            self._show_text_dialog("风扇控制服务未启用", text, error=True)
        return False

    def _wait_for_helper(self, attempts: int) -> bool:
        if self.helper.refresh():
            self._helper_ready = True
            self.fan_card.set_helper_ready(True)
            self._update_status(self.snapshot)
            self.toast.show("风扇手动控制已启用")
            self._schedule_sample(200)
            return False
        if attempts > 0:
            GLib.timeout_add(500, self._wait_for_helper, attempts - 1)
        else:
            self.toast.show("服务已安装，但暂时无法连接。请查看 systemctl status dell-g15-thermal-helper", error=True, timeout=8)
        return False

    def _show_text_dialog(self, title: str, body: str, error: bool = False) -> None:
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.set_default_size(620, 360)
        dialog.add_button("关闭", Gtk.ResponseType.CLOSE)
        area = dialog.get_content_area()
        area.set_border_width(16)
        area.set_spacing(10)
        header = Gtk.Label(label=title, xalign=0)
        header.get_style_context().add_class("card-title")
        area.pack_start(header, False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(body)
        scroller.add(view)
        area.pack_start(scroller, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    # ------------------------------------------------------------------ misc
    def _redetect(self) -> None:
        self.toast.show("正在重新检测硬件…")
        self.control_executor.submit(self._do_redetect)

    def _do_redetect(self) -> None:
        try:
            new = DemoSensors() if self.demo else SensorHub()
        except Exception as exc:
            GLib.idle_add(self.toast.show, f"检测失败：{exc}", True)
            return
        old = self.sensors
        self.sensors = new
        self.info = new.system_info()
        try:
            old.close()
        except Exception:
            pass
        GLib.idle_add(self._after_redetect)

    def _after_redetect(self) -> bool:
        self.header_sub.set_text(self._subtitle_text())
        self._helper_ready = self.helper.available(force=True)
        self.fan_card.set_helper_ready(self._helper_ready)
        self._schedule_sample(100)
        self.toast.show("硬件检测完成")
        return False

    def _run_autostart_state(self) -> None:
        if self._autostart_item is None:
            return
        enabled = autostart_path().exists()
        self._autostart_item.set_label(f"开机自启动：{'开' if enabled else '关'}")

    def _toggle_autostart(self) -> None:
        enabled = not autostart_path().exists()
        exe = shutil.which("dell-g15-thermal")
        if exe:
            cmd = exe
        else:
            cmd = f"{sys.executable} {Path(__file__).resolve()}"
        ok = set_autostart(enabled, cmd, str(ICON))
        self._run_autostart_state()
        self.toast.show(
            ("已开启开机自启动" if enabled else "已关闭开机自启动") if ok else "无法修改自启动设置",
            error=not ok,
        )

    def _show_about(self) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name("Dell G15 温控中心")
        dialog.set_version(VERSION)
        dialog.set_comments(
            "实时监测 CPU/GPU 温度与占用率、双风扇转速，并可手动调节风扇、"
            "一键切换 Dell G15 预设的 性能 / 平衡 / 节电 模式。\n\n"
            "数据来源：coretemp、alienware_wmi、NVML、power-profiles-daemon。"
        )
        dialog.set_license_type(Gtk.License.MIT)
        dialog.set_authors(["Dell G15 Thermal Control"])
        dialog.set_copyright("© 2026")
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(ICON), 96, 96, True)
            dialog.set_logo(pix)
        except Exception:
            pass
        dialog.run()
        dialog.destroy()

    def _on_destroy(self, *_args) -> None:
        if self._closing:
            return
        self._closing = True
        if self._tick_source is not None:
            try:
                GLib.source_remove(self._tick_source)
            except Exception:
                pass
        try:
            if self._full_size is not None:
                w, h = self._full_size
            else:
                w, h = self.get_size()
            self.settings.window_width = max(1130, min(int(w), 1600))
            self.settings.window_height = max(650, min(int(h), 1000))
            if self._last_non_manual_preset in ("performance", "balanced", "saver"):
                self.settings.last_preset = self._last_non_manual_preset
            self.settings.save()
        except Exception:
            pass
        try:
            self.executor.shutdown(wait=True)
        except Exception:
            pass
        try:
            self.control_executor.shutdown(wait=True)
        except Exception:
            pass
        try:
            self.sensors.close()
        except Exception:
            pass
        Gtk.main_quit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Dell G15 温控中心")
    parser.add_argument("--demo", action="store_true", help="使用模拟数据（用于展示/调试）")
    parser.add_argument("--geometry", help="窗口几何，例如 1280x800+80+60")
    parser.add_argument("--screenshot", help="等待后把窗口截图保存到指定 PNG 并退出（用于文档）")
    parser.add_argument("--screenshot-wait", type=int, default=20, help="截图前等待秒数（默认 20）")
    parser.add_argument("--compact", action="store_true", help="直接以迷你模式启动（只显示温度和风扇转速）")
    parser.add_argument("--version", action="version", version=f"Dell G15 温控中心 {VERSION}")
    args = parser.parse_args()

    GLib.set_prgname("dell-g15-thermal")
    GLib.set_application_name("Dell G15 温控中心")
    load_css()
    window = MainWindow(
        demo=args.demo,
        geometry=args.geometry,
        screenshot=args.screenshot,
        screenshot_wait=args.screenshot_wait,
        start_compact=args.compact,
    )
    window.show_all()
    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
