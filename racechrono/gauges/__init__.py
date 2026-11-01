"""Element registry.

CONFIG['elements'] is data: names map to classes here, so enabling, disabling,
moving or resizing an element never touches render code. Unimplemented slots are
registered as None so config validation can reject enabling them with a clear
message instead of failing obscurely at draw time.
"""
from __future__ import annotations

from .base import Gauge, Layout, TEXT, TextCache        # noqa: F401
from .dial import Dial, Speedo, Tacho, Throttle         # noqa: F401
from .gmeter import GMeter                              # noqa: F401
from .map_gauge import MapGauge                          # noqa: F401

REGISTRY = {
    "track_map": MapGauge,
    "speedo": Speedo,
    "tacho": Tacho,
    "gmeter": GMeter,
    "throttle": Throttle,
    # Reserved for future use; the reference image shows lap timer boxes across
    # the top, but v1 draws no lap timing.
    "lap_timer": None,
    "sector_delta": None,
    "speed_bar": None,
    "coolant_temp": None,
}


def build(cfg, layout, context):
    """Instantiate every enabled element, in draw order.

    The speedo is built first because the tacho and G-meter position themselves
    relative to it, in speedo diameters.
    """
    order = ["speedo", "tacho", "gmeter", "throttle", "track_map"]
    order += [n for n in cfg.elements if n not in order]

    ctx = dict(context)
    ctx.setdefault("skipped", [])
    built = []
    for name in order:
        el = cfg.elements.get(name)
        if el is None or not el.enabled:
            continue
        if name == "track_map" and ctx.get("trackmap") is None:
            # No map for this venue -- an autocross site, or a track no map has
            # been built for yet. Dropping one element beats refusing to render
            # the session, but it is never silent: the caller reports it.
            ctx["skipped"].append(
                "track_map: no map covers this location, so the map element was "
                "left out. Build one with tools/generate_map.py, or set "
                "elements.track_map.enabled = False to stop asking.")
            continue
        klass = REGISTRY.get(name)
        if klass is None:
            raise ValueError("element %r is registered but not implemented" % name)
        gauge = klass(el, layout, ctx)
        if name == "speedo":
            ctx["reference_center_x"] = gauge.cx
            ctx["reference_diameter"] = gauge.d
        built.append(gauge)
    return built
