"""Custom Cairo DrawingArea widgets and reusable GTK pieces."""

from __future__ import annotations

import math
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

FONT = "Noto Sans CJK SC"
MONO = "DejaVu Sans Mono"

CYAN = "#22d3ee"
VIOLET = "#a78bfa"
AMBER = "#fbbf24"
CORAL = "#fb7185"
RED = "#ef4444"
GREEN = "#34d399"
MUTED = "#8091a9"
TEXT = "#e8eff9"


# --------------------------------------------------------------------------- color
def rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def mix(c1: tuple[float, float, float], c2: tuple[float, float, float], t: float) -> tuple[float, float, float]:
    t = max(0.0, min(1.0, t))
    return (c1[0] + (c2[0] - c1[0]) * t, c1[1] + (c2[1] - c1[1]) * t, c1[2] + (c2[2] - c1[2]) * t)


def temp_rgb(temp: float | None, accent: str = CYAN) -> tuple[float, float, float]:
    if temp is None:
        return rgb("#475569")
    base = rgb(accent)
    if temp <= 55:
        return base
    if temp <= 72:
        return mix(base, rgb(AMBER), (temp - 55) / 17.0)
    if temp <= 86:
        return mix(rgb(AMBER), rgb(CORAL), (temp - 72) / 14.0)
    return mix(rgb(CORAL), rgb(RED), min(1.0, (temp - 86) / 14.0))


def _layout(cr: cairo.Context, text: str, size: float, weight: str = "normal", family: str = FONT):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription()
    desc.set_family(family)
    desc.set_absolute_size(size * Pango.SCALE)
    desc.set_weight(Pango.Weight.BOLD if weight in ("bold", "600", "700") else Pango.Weight.NORMAL)
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    return layout


def measure(cr: cairo.Context, text: str, size: float, weight: str = "normal", family: str = FONT) -> tuple[int, int]:
    return _layout(cr, text, size, weight, family).get_pixel_size()


def text(
    cr: cairo.Context,
    content: str,
    x: float,
    y: float,
    size: float,
    color: str,
    weight: str = "normal",
    align: str = "left",
    alpha: float = 1.0,
    family: str = FONT,
) -> tuple[int, int]:
    layout = _layout(cr, content, size, weight, family)
    w, h = layout.get_pixel_size()
    if align == "center":
        x -= w / 2
    elif align == "right":
        x -= w
    cr.move_to(x, y)
    r, g, b = rgb(color)
    cr.set_source_rgba(r, g, b, alpha)
    PangoCairo.show_layout(cr, layout)
    return w, h


def rounded_rect(cr: cairo.Context, x: float, y: float, w: float, h: float, radius: float) -> None:
    radius = min(radius, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + w - radius, y + h - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + h - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


# --------------------------------------------------------------------------- gauge
class ArcGauge(Gtk.DrawingArea):
    """A 270-degree temperature gauge with a thin outer usage arc."""

    def __init__(self, title: str, accent: str = CYAN) -> None:
        super().__init__()
        self.title = title
        self.accent = accent
        self.temp: float | None = None
        self.usage: float | None = None
        self.usage_available = True
        self.subtext = ""
        self.cores: list[float] = []
        self.set_size_request(270, 305)
        self.connect("draw", self._on_draw)

    def set_values(
        self,
        temp: float | None,
        usage: float | None,
        subtext: str = "",
        cores: list[float] | None = None,
    ) -> None:
        self.temp = temp
        self.usage = usage
        self.subtext = subtext
        self.cores = cores or []
        self.queue_draw()

    def _on_draw(self, _widget, cr: cairo.Context) -> bool:
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        if w < 60 or h < 60:
            return False

        cx = w / 2
        cy = h * 0.475
        r = min(w * 0.355, h * 0.325)
        start = 0.75 * math.pi
        sweep = 1.5 * math.pi
        line = max(11.0, r * 0.135)

        # Subtle radial glow behind the value.
        glow = cairo.RadialGradient(cx, cy, 2, cx, cy, r * 1.05)
        ar, ag, ab = temp_rgb(self.temp, self.accent)
        glow.add_color_stop_rgba(0.0, ar, ag, ab, 0.09)
        glow.add_color_stop_rgba(1.0, ar, ag, ab, 0.0)
        cr.set_source(glow)
        cr.arc(cx, cy, r * 1.05, 0, 2 * math.pi)
        cr.fill()

        # Track.
        cr.set_line_width(line)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(*rgb("#1a2638"), 1.0)
        cr.arc(cx, cy, r, start, start + sweep)
        cr.stroke()

        # Thin usage ring outside the temperature ring.
        ur = r + line * 0.95
        cr.set_line_width(4.5)
        cr.set_source_rgba(*rgb("#172335"), 1.0)
        cr.arc(cx, cy, ur, start, start + sweep)
        cr.stroke()
        if self.usage is not None:
            cr.set_source_rgba(*rgb(self.accent), 0.85)
            cr.arc(cx, cy, ur, start, start + sweep * self.usage / 100.0)
            cr.stroke()

        # Soft glow + value arc.
        if self.temp is not None and self.temp > 0.2:
            end = start + sweep * self.temp / 100.0
            tr, tg, tb = temp_rgb(self.temp, self.accent)
            cr.set_source_rgba(tr, tg, tb, 0.12)
            cr.set_line_width(line + 10)
            cr.arc(cx, cy, r, start, end)
            cr.stroke()

            cr.set_source_rgba(tr, tg, tb, 1.0)
            cr.set_line_width(line)
            cr.arc(cx, cy, r, start, end)
            cr.stroke()

            # End cap dot.
            ex = cx + math.cos(end) * r
            ey = cy + math.sin(end) * r
            cr.set_source_rgba(tr, tg, tb, 0.18)
            cr.arc(ex, ey, line * 0.8, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.arc(ex, ey, max(2.2, line * 0.18), 0, 2 * math.pi)
            cr.fill()

        # Ticks inside the ring.
        for i in range(11):
            ang = start + sweep * i / 10.0
            major = i % 5 == 0
            inner = r - line - (7 if major else 5)
            outer = r - line - 1
            cr.set_source_rgba(*rgb("#4b5f7a" if major else "#334155"), 0.9 if major else 0.55)
            cr.set_line_width(1.6 if major else 1.0)
            cr.move_to(cx + math.cos(ang) * inner, cy + math.sin(ang) * inner)
            cr.line_to(cx + math.cos(ang) * outer, cy + math.sin(ang) * outer)
            cr.stroke()

        # Title above the value.
        text(cr, self.title, cx, cy - 84, 11.0, MUTED, "normal", "center")

        # Big temperature.
        if self.temp is None:
            text(cr, "--", cx, cy - 58, 48, "#64748b", "bold", "center")
            text(cr, "°C", cx + 42, cy - 18, 17, MUTED, "bold", "left")
        else:
            color = CORAL if self.temp >= 85 else "#f8fafc"
            text(cr, f"{self.temp:.0f}", cx - 12, cy - 58, 51, color, "bold", "center")
            text(cr, "°C", cx + 38, cy - 18, 17, MUTED, "bold", "left")

        # Usage line + dot.
        usage_txt = f"占用 {self.usage:.0f}%" if self.usage is not None else "占用 --"
        uw, _ = measure(cr, usage_txt, 12.0, "600")
        ux = cx - uw / 2
        cr.set_source_rgba(*rgb(self.accent), 0.95)
        cr.arc(ux - 10, cy + 44, 3.0, 0, 2 * math.pi)
        cr.fill()
        text(cr, usage_txt, ux, cy + 36, 12.0, "#cbd5e1", "600")

        # Per-core micro bars for the CPU gauge.
        if self.cores:
            n = min(len(self.cores), 24)
            bw, gap = 4.0, 2.0
            total = n * bw + (n - 1) * gap
            x0 = cx - total / 2
            base = h - 42
            for i, val in enumerate(self.cores[:n]):
                val = max(0.0, min(100.0, val))
                bh = 3 + val / 100.0 * 13
                cr.set_source_rgba(*rgb(self.accent), 0.22 + 0.68 * val / 100.0)
                rounded_rect(cr, x0 + i * (bw + gap), base + (16 - bh), bw, bh, 1.6)
                cr.fill()

        if self.subtext:
            text(cr, self.subtext, cx, h - 19, 9.5, MUTED, "normal", "center")
        return False


# --------------------------------------------------------------------------- fan
class FanIcon(Gtk.DrawingArea):
    def __init__(self, size: int = 46, accent: str = CYAN) -> None:
        super().__init__()
        self.accent = accent
        self.rpm = 0
        self.angle = 0.0
        self._source: int | None = None
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._on_draw)

    def set_rpm(self, rpm: int | None, accent: str | None = None) -> None:
        new_rpm = rpm or 0
        new_accent = accent or self.accent
        changed = new_rpm != self.rpm or new_accent != self.accent
        self.rpm = new_rpm
        self.accent = new_accent
        if self.rpm > 150:
            if self._source is None:
                self._source = GLib.timeout_add(200, self._tick)
        else:
            if self._source is not None:
                GLib.source_remove(self._source)
                self._source = None
        if changed:
            self.queue_draw()

    def _tick(self) -> bool:
        if self.rpm <= 150:
            self._source = None
            return False
        self.angle += 0.12 + min(self.rpm, 6000) / 6000.0 * 0.55
        self.queue_draw()
        return True

    def _on_draw(self, _widget, cr: cairo.Context) -> bool:
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        cx, cy = w / 2, h / 2
        radius = min(w, h) / 2 - 2
        ar, ag, ab = rgb(self.accent)

        if self.rpm > 2500:
            glow = cairo.RadialGradient(cx, cy, 1, cx, cy, radius)
            glow.add_color_stop_rgba(0, ar, ag, ab, 0.16)
            glow.add_color_stop_rgba(1, ar, ag, ab, 0.0)
            cr.set_source(glow)
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.fill()

        cr.set_source_rgba(*rgb("#22324a"), 0.9)
        cr.set_line_width(1.5)
        cr.arc(cx, cy, radius - 0.8, 0, 2 * math.pi)
        cr.stroke()

        cr.save()
        cr.translate(cx, cy)
        cr.rotate(self.angle)
        for i in range(5):
            cr.save()
            cr.rotate(i * 2 * math.pi / 5)
            cr.move_to(0, -3.2)
            cr.curve_to(6.0, -8.0, 15.0, -4.0, 13.6, 5.6)
            cr.curve_to(8.6, 3.4, 3.2, 5.6, 0.0, 2.8)
            cr.close_path()
            cr.set_source_rgba(ar, ag, ab, 0.82 if self.rpm > 150 else 0.38)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.set_line_width(0.7)
            cr.stroke()
            cr.restore()
        cr.restore()

        cr.set_source_rgba(*rgb("#0b1220"), 1.0)
        cr.arc(cx, cy, 5.2, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(ar, ag, ab, 0.55)
        cr.set_line_width(1.3)
        cr.arc(cx, cy, 5.2, 0, 2 * math.pi)
        cr.stroke()
        return False


# --------------------------------------------------------------------------- chart
class TrendChart(Gtk.DrawingArea):
    def __init__(
        self,
        title: str,
        unit: str,
        series: list[dict],
        history: int = 90,
        y_max: float = 100.0,
        fixed_zero: bool = True,
    ) -> None:
        super().__init__()
        self.title = title
        self.unit = unit
        self.series = series
        self.history = history
        self.y_max = y_max
        self.samples: list[dict] = []
        self.set_size_request(220, 150)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.connect("draw", self._on_draw)

    def push(self, values: dict) -> None:
        self.samples.append(values)
        if len(self.samples) > self.history:
            del self.samples[: len(self.samples) - self.history]
        self.queue_draw()

    def _series_value(self, key: str) -> float | None:
        for sample in reversed(self.samples):
            value = sample.get(key)
            if value is not None:
                return value
        return None

    def _on_draw(self, _widget, cr: cairo.Context) -> bool:
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        if w < 120 or h < 80:
            return False

        # Header ---------------------------------------------------------
        text(cr, self.title, 16, 13, 12.5, TEXT, "bold")
        x = w - 16
        for spec in reversed(self.series):
            val = self._series_value(spec["key"])
            label = f"{spec['label']} {val:.0f}{self.unit}" if val is not None else f"{spec['label']} --"
            tw, _ = measure(cr, label, 10.0, "600")
            x -= tw
            text(cr, label, x, 16, 10.0, "#aebdd1" if val is not None else "#64748b", "600")
            cr.set_source_rgba(*rgb(spec["color"]), 0.95 if val is not None else 0.35)
            cr.arc(x - 9, 24, 3.0, 0, 2 * math.pi)
            cr.fill()
            x -= 24

        cr.set_source_rgba(*rgb("#1a2739"), 0.8)
        cr.set_line_width(1)
        cr.move_to(16, 36.5)
        cr.line_to(w - 16, 36.5)
        cr.stroke()

        left, right, top, bottom = 40.0, 16.0, 46.0, h - 24.0
        pw, ph = w - left - right, bottom - top
        if pw <= 10 or ph <= 10:
            return False

        # Grid -----------------------------------------------------------
        for i in range(5):
            frac = i / 4.0
            y = bottom - ph * frac
            cr.set_source_rgba(*rgb("#1c293b"), 0.75 if i in (0, 4) else 0.45)
            cr.set_line_width(1)
            cr.move_to(left, y + 0.5)
            cr.line_to(w - right, y + 0.5)
            cr.stroke()
            value = self.y_max * frac
            text(cr, f"{value:.0f}", left - 8, y - 6.5, 9.0, "#64748b", "normal", "right")

        # X labels -------------------------------------------------------
        text(cr, f"-{self.history}s", left, h - 17, 9.0, "#64748b")
        text(cr, f"-{self.history // 2}s", left + pw / 2, h - 17, 9.0, "#64748b", "normal", "center")
        text(cr, "现在", w - right, h - 17, 9.0, "#64748b", "normal", "right")

        if len(self.samples) < 2:
            text(cr, "正在采集数据…", left + pw / 2, top + ph / 2 - 8, 10.5, "#52637a", "normal", "center")
            return False

        cr.save()
        cr.rectangle(left, top, pw, ph)
        cr.clip()

        for spec in self.series:
            color = rgb(spec["color"])
            segments: list[list[tuple[float, float]]] = []
            current: list[tuple[float, float]] = []
            offset = max(0, self.history - len(self.samples))
            for i, sample in enumerate(self.samples):
                val = sample.get(spec["key"])
                if val is None:
                    if len(current) > 1:
                        segments.append(current)
                    current = []
                    continue
                val = max(0.0, min(self.y_max, float(val)))
                x = left + ((offset + i) / max(1, self.history - 1)) * pw
                y = bottom - (val / self.y_max) * ph
                current.append((x, y))
            if len(current) > 1:
                segments.append(current)

            for pts in segments:
                # Area fill.
                grad = cairo.LinearGradient(0, top, 0, bottom)
                grad.add_color_stop_rgba(0.0, color[0], color[1], color[2], 0.18)
                grad.add_color_stop_rgba(1.0, color[0], color[1], color[2], 0.0)
                cr.set_source(grad)
                cr.move_to(pts[0][0], pts[0][1])
                for px, py in pts[1:]:
                    cr.line_to(px, py)
                cr.line_to(pts[-1][0], bottom)
                cr.line_to(pts[0][0], bottom)
                cr.close_path()
                cr.fill()

                cr.set_source_rgba(color[0], color[1], color[2], 0.95)
                cr.set_line_width(1.9)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.move_to(pts[0][0], pts[0][1])
                for px, py in pts[1:]:
                    cr.line_to(px, py)
                cr.stroke()

                # End glow dot.
                ex, ey = pts[-1]
                cr.set_source_rgba(color[0], color[1], color[2], 0.18)
                cr.arc(ex, ey, 6.0, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(color[0], color[1], color[2], 1.0)
                cr.arc(ex, ey, 2.6, 0, 2 * math.pi)
                cr.fill()

        cr.restore()
        return False


# --------------------------------------------------------------------------- fan card
class FanRow(Gtk.Box):
    def __init__(self, title: str, sub: str, accent: str, on_change) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        self.accent = accent
        self.on_change = on_change
        self._updating = False
        self._dragging = False
        self._last_change = 0.0
        self._pending: int | None = None
        self.set_margin_top(4)
        self.set_margin_bottom(4)

        self.icon = FanIcon(46, accent)
        self.pack_start(self.icon, False, False, 0)

        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        names.set_size_request(96, -1)
        self.name_label = Gtk.Label(label=title, xalign=0)
        self.name_label.get_style_context().add_class("fan-name")
        self.sub_label = Gtk.Label(label=sub, xalign=0)
        self.sub_label.get_style_context().add_class("fan-sub")
        names.pack_start(self.name_label, False, False, 0)
        names.pack_start(self.sub_label, False, False, 0)
        names.set_valign(Gtk.Align.CENTER)
        self.pack_start(names, False, False, 0)

        rpm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        rpm_box.set_size_request(80, -1)
        rpm_box.set_valign(Gtk.Align.CENTER)
        self.rpm_label = Gtk.Label(label="--", xalign=0)
        self.rpm_label.get_style_context().add_class("fan-rpm")
        unit = Gtk.Label(label="RPM", xalign=0)
        unit.get_style_context().add_class("fan-unit")
        rpm_box.pack_start(self.rpm_label, False, False, 0)
        rpm_box.pack_start(unit, False, False, 0)
        self.pack_start(rpm_box, False, False, 0)

        control = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        control.set_valign(Gtk.Align.CENTER)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        hint = Gtk.Label(label="手动增强", xalign=0)
        hint.get_style_context().add_class("fan-hint")
        self.pct_label = Gtk.Label(label="0%", xalign=1)
        self.pct_label.get_style_context().add_class("fan-pct")
        self.pct_label.set_size_request(38, -1)
        top.pack_start(hint, False, False, 0)
        top.pack_end(self.pct_label, False, False, 0)
        control.pack_start(top, False, False, 0)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_draw_value(False)
        self.scale.set_round_digits(0)
        self.scale.set_size_request(-1, 22)
        self.scale.get_style_context().add_class("fan-cpu" if accent == CYAN else "fan-gpu")
        self.scale.connect("value-changed", self._on_value_changed)
        self.scale.connect("button-press-event", self._on_press)
        self.scale.connect("button-release-event", self._on_release)
        control.pack_start(self.scale, False, False, 0)
        self.pack_start(control, True, True, 0)

    # ---------------------------------------------------------------- callbacks
    def _on_press(self, _widget, _event) -> bool:
        self._dragging = True
        return False

    def _on_release(self, _widget, _event) -> bool:
        self._dragging = False
        self._last_change = time.monotonic()
        return False

    def _on_value_changed(self, _scale) -> None:
        if self._updating:
            return
        value = int(round(self.scale.get_value()))
        self.pct_label.set_text(f"{value}%")
        self._last_change = time.monotonic()
        if self._pending is not None:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(280, self._fire)

    def _fire(self) -> bool:
        self._pending = None
        if self.on_change is not None:
            self.on_change(self)
        return False

    # ---------------------------------------------------------------- api
    def set_user_value(self, pct: float) -> None:
        self._updating = True
        self.scale.set_value(max(0.0, min(100.0, pct)))
        self.pct_label.set_text(f"{int(round(pct))}%")
        self._updating = False

    def set_live(self, rpm: int | None, boost: int | None, enabled: bool) -> None:
        self.rpm_label.set_text(f"{rpm:,}" if rpm is not None else "--")
        self.icon.set_rpm(rpm, self.accent)
        if enabled and boost is not None and not self._dragging:
            if time.monotonic() - self._last_change > 1.8:
                self.set_user_value(boost / 255.0 * 100.0)
        elif not enabled and boost is not None:
            self.set_user_value(boost / 255.0 * 100.0)
        self.scale.set_sensitive(enabled)
        self.icon.set_opacity(1.0 if enabled else 0.55)

    def value(self) -> int:
        return int(round(self.scale.get_value()))


class FanControlCard(Gtk.Box):
    def __init__(self, settings, on_apply, on_restore, on_install, on_setting_change) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        self.get_style_context().add_class("card")
        self.set_border_width(16)
        self.set_size_request(460, -1)
        self._busy = False
        self._helper_ready = False
        self._manual = False
        self._protect_active = False
        self.settings = settings
        self.on_apply = on_apply
        self.on_restore = on_restore
        self.on_install = on_install
        self.on_setting_change = on_setting_change

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="风扇控制", xalign=0)
        title.get_style_context().add_class("card-title")
        header.pack_start(title, False, False, 0)
        self.chip = Gtk.Label(label="正在读取…")
        self.chip.get_style_context().add_class("chip")
        header.pack_end(self.chip, False, False, 0)
        self.pack_start(header, False, False, 0)

        self.hint = Gtk.Label(
            label="拖动滑块进入手动模式 · 0% 为固件最低/自动，100% 为最大增强",
            xalign=0,
        )
        self.hint.get_style_context().add_class("card-sub")
        self.pack_start(self.hint, False, False, 0)

        self.cpu_row = FanRow("CPU 风扇", "CPU Fan", CYAN, self._row_changed)
        self.pack_start(self.cpu_row, False, False, 0)
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(2)
        sep.set_margin_bottom(2)
        self.pack_start(sep, False, False, 0)
        self.gpu_row = FanRow("GPU 风扇", "GPU Fan", VIOLET, self._row_changed)
        self.pack_start(self.gpu_row, False, False, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bottom.set_margin_top(4)
        sync_label = Gtk.Label(label="同步双风扇", xalign=0)
        sync_label.get_style_context().add_class("tiny")
        sync_label.get_style_context().add_class("muted")
        self.sync = Gtk.Switch()
        self.sync.set_active(bool(settings.sync_fans))
        self.sync.connect("notify::active", self._on_sync_toggled)
        bottom.pack_start(sync_label, False, False, 0)
        bottom.pack_start(self.sync, False, False, 0)

        protect_label = Gtk.Label(label="高温保护", xalign=0)
        protect_label.get_style_context().add_class("tiny")
        protect_label.get_style_context().add_class("muted")
        self.protect = Gtk.Switch()
        self.protect.set_active(bool(settings.protect))
        self.protect.connect("notify::active", self._on_protect_toggled)
        bottom.pack_start(protect_label, False, False, 0)
        bottom.pack_start(self.protect, False, False, 0)

        bottom.pack_start(Gtk.Box(), True, True, 0)
        self.restore_btn = Gtk.Button(label="恢复预设")
        self.restore_btn.get_style_context().add_class("ghost")
        self.restore_btn.connect("clicked", lambda *_: self.on_restore())
        self.restore_btn.set_sensitive(False)
        bottom.pack_end(self.restore_btn, False, False, 0)
        self.pack_start(bottom, False, False, 0)

        self.install_revealer = Gtk.Revealer()
        self.install_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.install_revealer.set_transition_duration(180)
        self.install_revealer.set_reveal_child(False)
        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        banner.get_style_context().add_class("install-banner")
        lock = Gtk.Image.new_from_icon_name("changes-prevent-symbolic", Gtk.IconSize.MENU)
        lock.set_opacity(0.8)
        banner.pack_start(lock, False, False, 0)
        blabel = Gtk.Label(
            label="手动调速需要一次授权安装（仅本机 root 助手，不联网）", xalign=0
        )
        blabel.get_style_context().add_class("tiny")
        blabel.set_line_wrap(True)
        banner.pack_start(blabel, True, True, 0)
        install_btn = Gtk.Button(label="启用风扇控制")
        install_btn.get_style_context().add_class("primary")
        install_btn.connect("clicked", lambda *_: self.on_install())
        banner.pack_end(install_btn, False, False, 0)
        self.install_revealer.add(banner)
        self.pack_start(self.install_revealer, False, False, 0)

    # ---------------------------------------------------------------- callbacks
    def _row_changed(self, row: FanRow) -> None:
        if self._busy or not self._helper_ready:
            return
        if self.sync.get_active():
            other = self.gpu_row if row is self.cpu_row else self.cpu_row
            other.set_user_value(row.value())
        self.on_apply(self.cpu_row.value(), self.gpu_row.value())

    def _on_sync_toggled(self, switch, _pspec) -> None:
        self.settings.sync_fans = switch.get_active()
        if switch.get_active():
            self.gpu_row.set_user_value(self.cpu_row.value())
        if self.on_setting_change:
            self.on_setting_change()

    def _on_protect_toggled(self, switch, _pspec) -> None:
        self.settings.protect = switch.get_active()
        if self.on_setting_change:
            self.on_setting_change()

    # ---------------------------------------------------------------- api
    def set_live(self, snap, helper_ready: bool) -> None:
        self._helper_ready = helper_ready
        self._manual = bool(snap.manual)
        ctx = self.chip.get_style_context()
        for cls in ("chip-ok", "chip-manual", "chip-warn"):
            ctx.remove_class(cls)
        if self._protect_active:
            self.chip.set_text("高温保护")
            ctx.add_class("chip-warn")
        elif snap.manual:
            self.chip.set_text("手动控制")
            ctx.add_class("chip-manual")
        elif snap.preset:
            name = {"performance": "性能", "balanced": "平衡", "saver": "节电"}.get(snap.preset, snap.preset)
            self.chip.set_text(f"预设 · {name}")
            ctx.add_class("chip-ok")
        else:
            self.chip.set_text("只读模式")

        self.hint.set_text(
            "拖动滑块进入手动模式 · 0% 为固件最低/自动，100% 为最大增强"
            if helper_ready
            else "安装特权助手后，即可独立调节两个风扇"
        )
        self.cpu_row.set_live(snap.fan_cpu_rpm, snap.fan_cpu_boost, helper_ready and not self._busy)
        self.gpu_row.set_live(snap.fan_gpu_rpm, snap.fan_gpu_boost, helper_ready and not self._busy)
        self.restore_btn.set_sensitive(bool(snap.manual) and not self._busy)
        self.install_revealer.set_reveal_child(not helper_ready)

    def set_protection_active(self, active: bool) -> None:
        self._protect_active = active
        ctx = self.chip.get_style_context()
        if active:
            for cls in ("chip-ok", "chip-manual"):
                ctx.remove_class(cls)
            ctx.add_class("chip-warn")
            self.chip.set_text("高温保护")
        elif self._manual:
            for cls in ("chip-warn", "chip-ok"):
                ctx.remove_class(cls)
            ctx.add_class("chip-manual")
            self.chip.set_text("手动控制")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.restore_btn.set_sensitive(self._manual and not busy)
        self.cpu_row.scale.set_sensitive(not busy and self._helper_ready)
        self.gpu_row.scale.set_sensitive(not busy and self._helper_ready)

    def set_values(self, cpu_pct: float, gpu_pct: float) -> None:
        self.cpu_row.set_user_value(cpu_pct)
        self.gpu_row.set_user_value(gpu_pct)

    def values(self) -> tuple[int, int]:
        return self.cpu_row.value(), self.gpu_row.value()

    def set_helper_ready(self, ready: bool) -> None:
        self._helper_ready = ready
        self.install_revealer.set_reveal_child(not ready)


class Toast(Gtk.Revealer):
    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(170)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.END)
        self.set_margin_bottom(20)
        self.set_reveal_child(False)
        self._timer: int | None = None
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.get_style_context().add_class("toast")
        self.label = Gtk.Label(label="")
        self.label.set_line_wrap(True)
        self.label.set_max_width_chars(70)
        box.pack_start(self.label, False, False, 0)
        self.add(box)
        self._box = box

    def show(self, message: str, error: bool = False, timeout: int = 4) -> None:
        self.label.set_text(message)
        ctx = self._box.get_style_context()
        if error:
            ctx.add_class("toast-error")
        else:
            ctx.remove_class("toast-error")
        self.set_reveal_child(True)
        if self._timer is not None:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add_seconds(timeout, self._hide)

    def _hide(self) -> bool:
        self._timer = None
        self.set_reveal_child(False)
        return False


def make_card(child: Gtk.Widget | None = None, spacing: int = 8) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    box.get_style_context().add_class("card")
    box.set_border_width(16)
    if child is not None:
        box.pack_start(child, True, True, 0)
    return box


# --------------------------------------------------------------------------- compact view
class CompactMetric(Gtk.Box):
    """A small card for the mini window: CPU/GPU temperature or fan RPM."""

    def __init__(self, label: str, accent: str, unit: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.get_style_context().add_class("compact-card")
        self.set_border_width(10)
        self.set_hexpand(True)
        self.set_vexpand(True)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dot = Gtk.Label()
        dot.set_markup(f'<span foreground="{accent}" size="small">●</span>')
        dot.set_valign(Gtk.Align.CENTER)
        top.pack_start(dot, False, False, 0)
        name = Gtk.Label(label=label, xalign=0)
        name.get_style_context().add_class("compact-label")
        top.pack_start(name, False, False, 0)
        self.pack_start(top, False, False, 0)

        self.unit_text = unit
        self.value = Gtk.Label(label="--", xalign=0)
        self.value.get_style_context().add_class("compact-value")
        self.pack_start(self.value, True, True, 0)
        self.set_value("--")

    def set_value(self, value: str) -> None:
        self.value.set_markup(
            f'{value}<span size="9000" foreground="#8091a9"> {self.unit_text}</span>'
        )


class CompactView(Gtk.Box):
    """The mini window: only live temperatures and both fan speeds."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_border_width(12)
        self.set_size_request(420, 150)
        self.get_style_context().add_class("compact-root")

        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(8)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)
        self.cpu_temp = CompactMetric("CPU 温度", CYAN, "°C")
        self.gpu_temp = CompactMetric("GPU 温度", VIOLET, "°C")
        self.cpu_fan = CompactMetric("CPU 风扇", CYAN, "RPM")
        self.gpu_fan = CompactMetric("GPU 风扇", VIOLET, "RPM")
        grid.attach(self.cpu_temp, 0, 0, 1, 1)
        grid.attach(self.gpu_temp, 1, 0, 1, 1)
        grid.attach(self.cpu_fan, 0, 1, 1, 1)
        grid.attach(self.gpu_fan, 1, 1, 1, 1)
        self.pack_start(grid, True, True, 0)

    def update(self, snap) -> None:
        self.cpu_temp.set_value("--" if snap.cpu_temp is None else f"{snap.cpu_temp:.0f}")
        self.gpu_temp.set_value("--" if snap.gpu_temp is None else f"{snap.gpu_temp:.0f}")
        self.cpu_fan.set_value("--" if snap.fan_cpu_rpm is None else f"{snap.fan_cpu_rpm:,}")
        self.gpu_fan.set_value("--" if snap.fan_gpu_rpm is None else f"{snap.fan_gpu_rpm:,}")
