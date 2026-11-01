"""Round dials: speedometer and tachometer.

Both share one implementation because the reference layout draws them
identically -- an open arc of fine ticks, sparse numerals, a thin red needle, a
large value below centre and a small unit label above it. Only the range,
channel and label divisor differ.
"""
from __future__ import annotations

import math

import cairo

from .base import (Gauge, TEXT, dial_angles, new_surface, value_angle)

MPS_TO_MPH = 2.2369362920544
MPS_TO_KPH = 3.6


class Dial(Gauge):
    name = "dial"

    def place(self):
        cfg, lay = self.cfg, self.layout
        self.d = lay.size(cfg.get("diameter", 0.26))
        self.r = self.d / 2.0

        x_frac = cfg.x
        if x_frac is None:
            # Positioned relative to the reference gauge, in its diameters.
            ref = self.context.get("reference_center_x")
            ref_d = self.context.get("reference_diameter", self.d)
            if ref is None:
                ref, ref_d = lay.sx(0.5), self.d
            self.cx = ref + float(cfg.get("offset_d", 0.0)) * ref_d
        else:
            self.cx = lay.sx(x_frac)
        # Every gauge in the row shares a bottom edge.
        self.bottom = lay.sy(cfg.y)
        self.cy = self.bottom - self.r

        self.vmin = float(cfg.get("min", 0))
        self.vmax = float(cfg.get("max", 100))
        self.major = float(cfg.get("major", (self.vmax - self.vmin) / 6.0))
        self.minor = float(cfg.get("minor", self.major / 4.0))
        self.start, self.sweep = dial_angles(cfg.get("sweep_deg", 270))
        self.divisor = float(cfg.get("label_divisor", 1) or 1)
        self.decimals = int(cfg.get("decimals", 0))
        self.unit = str(cfg.get("unit", ""))
        # `unit` selects the conversion; `label` is what gets printed. They are
        # usually the same, but a throttle dial wants to say "throttle" while
        # doing no conversion at all.
        self.label = str(cfg.get("label", self.unit))
        self.channel = str(cfg.get("channel", ""))

        # Optional linear rescale of the raw channel onto the dial's range,
        # for a sender whose usable travel is not its full numeric range.
        self.input_min = cfg.get("input_min")
        self.input_max = cfg.get("input_max")
        self._rescales = self.input_min is not None and self.input_max is not None
        # Out-of-range readings are meaningless once rescaled -- they are just
        # the sender overshooting its rails -- so clamp by default when a
        # rescale is configured, and never otherwise: a speedo should be free to
        # print 125 mph even if its dial only sweeps to 120.
        self.clamp = bool(cfg.get("clamp", self._rescales))

        self.needle_colour = cfg.get("needle_color", (0.88, 0.09, 0.09, 1.0))
        self.text_colour = cfg.get("text_color", (1.0, 1.0, 1.0, 1.0))
        self.backing_alpha = float(cfg.get("backing_alpha", 0.35))
        self.redline = cfg.get("redline")
        self.redline_colour = cfg.get("redline_color", (0.85, 0.1, 0.1, 0.85))

        # Size the value readout so the widest figure it can ever show still
        # clears the numerals flanking the bottom gap.
        widest = max(len(self._format(self.vmax, self.decimals)),
                     len(self._format(self.vmin, self.decimals)))
        self.value_size = min(self.r * 0.36, self.r * 0.95 / (0.60 * widest))
        # "throttle" is far longer than "mph"; shrink the centre label to fit
        # rather than letting it run over the tick ring.
        self.label_size = min(self.r * 0.165,
                              self.r * 1.15 / (0.55 * max(1, len(self.label))))

        # The value text hangs below centre; give the box room for it.
        pad = self.r * 0.18
        self._bbox = (self.cx - self.r - pad, self.cy - self.r - pad,
                      2 * (self.r + pad), 2 * (self.r + pad))
        self._ox = self._bbox[0]
        self._oy = self._bbox[1]

    # -- static ------------------------------------------------------
    def build_static(self):
        x, y, w, h = self._bbox
        surf = new_surface(math.ceil(w), math.ceil(h))
        ctx = cairo.Context(surf)
        cx, cy, r = self.cx - x, self.cy - y, self.r

        if self.backing_alpha > 0:
            ctx.set_source_rgba(0.05, 0.06, 0.08, self.backing_alpha)
            ctx.arc(cx, cy, r * 1.005, 0, 2 * math.pi)
            ctx.fill()

        if self.redline is not None:
            a0 = value_angle(self.redline, self.vmin, self.vmax,
                             self.start, self.sweep)
            a1 = self.start + self.sweep
            ctx.set_source_rgba(*self.redline_colour)
            ctx.set_line_width(r * 0.045)
            ctx.arc(cx, cy, r * 0.955, a0, a1)
            ctx.stroke()

        # Fine tick marks, drawn inward from the rim. No full ring: the
        # reference dial is open-ended.
        n_minor = max(1, int(round((self.vmax - self.vmin) / self.minor)))
        for i in range(n_minor + 1):
            v = self.vmin + i * self.minor
            if v > self.vmax + 1e-9:
                break
            is_major = abs((v - self.vmin) % self.major) < 1e-6 or \
                abs((v - self.vmin) % self.major - self.major) < 1e-6
            a = value_angle(v, self.vmin, self.vmax, self.start, self.sweep)
            r0 = r * 0.985
            r1 = r * (0.885 if is_major else 0.930)
            lw = r * (0.022 if is_major else 0.011)
            ctx.set_line_width(lw)
            ctx.set_line_cap(cairo.LINE_CAP_BUTT)
            ctx.set_source_rgba(1, 1, 1, 0.95 if is_major else 0.75)
            ctx.move_to(cx + r0 * math.cos(a), cy + r0 * math.sin(a))
            ctx.line_to(cx + r1 * math.cos(a), cy + r1 * math.sin(a))
            ctx.stroke()

        # Sparse numerals, inside the tick ring. They sit at 0.78 R rather than
        # tighter in, so the ones nearest the bottom gap clear the large value
        # readout -- a four-digit RPM figure otherwise runs straight into them.
        label_size = r * 0.185
        n_major = max(1, int(round((self.vmax - self.vmin) / self.major)))
        for i in range(n_major + 1):
            v = self.vmin + i * self.major
            if v > self.vmax + 1e-9:
                break
            a = value_angle(v, self.vmin, self.vmax, self.start, self.sweep)
            rr = r * 0.780
            label = self._format(v / self.divisor, 0)
            TEXT.draw(ctx, label,
                      cx + rr * math.cos(a), cy + rr * math.sin(a),
                      label_size, self.text_colour,
                      align="center", valign="middle")

        # Label above centre ("mph", "rpm", "throttle").
        if self.label:
            TEXT.draw(ctx, self.label, cx, cy - r * 0.26, self.label_size,
                      self.text_colour, align="center", valign="middle")
        surf.flush()
        return surf

    # -- dynamic -----------------------------------------------------
    def draw(self, ctx, row):
        self.blit_static(ctx)
        raw = row.get(self.channel)
        v = self._display_value(raw)

        cx, cy, r = self.cx, self.cy, self.r
        if v is not None:
            a = value_angle(v, self.vmin, self.vmax, self.start, self.sweep)
            # Thin straight needle from the centre, no tail.
            ctx.save()
            ctx.set_line_width(r * 0.038)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.set_source_rgba(*self.needle_colour)
            ctx.move_to(cx, cy)
            ctx.line_to(cx + r * 0.845 * math.cos(a),
                        cy + r * 0.845 * math.sin(a))
            ctx.stroke()
            ctx.restore()

        text = "--" if v is None else self._format(v, self.decimals)
        TEXT.draw(ctx, text, cx, cy + r * 0.60, self.value_size, self.text_colour,
                  align="center", valign="middle", bold=True)

    def _display_value(self, raw):
        if raw is None or raw != raw:      # NaN
            return None
        v = float(raw)
        if self._rescales:
            span = float(self.input_max) - float(self.input_min)
            if span == 0:
                return None
            frac = (v - float(self.input_min)) / span
            v = self.vmin + frac * (self.vmax - self.vmin)
        if self.unit == "mph":
            v *= MPS_TO_MPH
        elif self.unit in ("kph", "km/h"):
            v *= MPS_TO_KPH
        if self.clamp:
            v = min(max(v, self.vmin), self.vmax)
        return v

    @property
    def channels(self):
        return (self.channel,)

    @staticmethod
    def _format(v, decimals):
        if decimals <= 0:
            return "%d" % int(round(v))
        return "%.*f" % (decimals, v)


class Speedo(Dial):
    name = "speedo"


class Tacho(Dial):
    name = "tacho"


class Throttle(Dial):
    """Throttle position, same dial style, small like the G-meter.

    The pedal sender does not use its full 0-100 range: on this car it rests at
    11.37% and pegs at 85.49% wide open, both hard plateaus in the data. Set
    `input_min`/`input_max` to those rails and the dial reads a true 0-100.
    """

    name = "throttle"
