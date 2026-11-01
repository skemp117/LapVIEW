"""The overlay contract: dirty regions, packed into one atlas surface.

The overlay is mostly empty. On the default layout the gauges cover 18.8% of a
1080p frame, so shipping a full 1920x1080 RGBA frame per output frame moves
8.29 MB where 2.84 MB would do -- 498 MB/s versus 170 MB/s at 60 fps.

That matters much less for export than you would think (measured ~6% faster;
the pipeline is capped by per-frame full-frame filter cost, not by overlay area)
and much more for preview, where the same frames have to reach a UI over a
socket. Either way there is one producer and the consumers differ:

    RegionCompositor.draw(row)  ->  atlas surface
                                     |
                     +---------------+---------------+
                     |                               |
              ffmpeg: crop each region        preview: send regions
              and overlay at its              to a client and draw
              destination                     them over a video element

Because both consume the same bytes from the same renderer, a preview cannot
drift from an export. That is the whole point of the seam.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cairo

# Regions closer than this are merged rather than kept separate: each extra
# region costs a crop and an overlay in the filter graph, and two boxes a few
# pixels apart are not worth that.
DEFAULT_MERGE_GAP = 24

# Past this coverage the atlas stops paying for itself and one full-frame region
# is simpler and no slower.
FULL_FRAME_ABOVE = 0.70

# Each region is a separate ffmpeg `overlay`, and each overlay costs a pass over
# the whole base frame however small the region is. Two is the measured optimum
# for a map-plus-gauge-row layout.
DEFAULT_MAX_REGIONS = 2


@dataclass
class Region:
    """A rectangle of the frame that the overlay touches.

    ``x, y, w, h`` is where it belongs in the output frame; ``ax, ay`` is where
    it sits in the packed atlas.
    """

    x: int
    y: int
    w: int
    h: int
    ax: int = 0
    ay: int = 0
    gauges: list = field(default_factory=list)

    @property
    def area(self):
        return self.w * self.h

    def as_dict(self):
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "ax": self.ax, "ay": self.ay,
                "gauges": [g.name for g in self.gauges]}


def _rect_of(gauge):
    x, y, w, h = gauge.bbox
    return (int(math.floor(x)), int(math.floor(y)),
            int(math.ceil(x + w)), int(math.ceil(y + h)))


def _near(a, b, gap):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (bx0 - ax1 > gap or ax0 - bx1 > gap
                or by0 - ay1 > gap or ay0 - by1 > gap)


def _area(r):
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def plan_regions(gauges, frame_size, merge_gap=DEFAULT_MERGE_GAP,
                 full_frame_above=FULL_FRAME_ABOVE,
                 max_regions=DEFAULT_MAX_REGIONS):
    """Group the gauges' boxes into as few regions as are worth having.

    Boxes that overlap or sit within `merge_gap` of each other are merged. Then
    the count is forced down to `max_regions` by repeatedly merging whichever
    pair wastes the least extra area.

    That cap is not cosmetic. Every region becomes its own `overlay` filter in
    ffmpeg, and each one costs a pass over the *whole* base frame regardless of
    how small the region is -- measured at 2.33x realtime for one region, 1.87x
    for three. So the payload saving from slicing finely is swamped by the extra
    compositing passes, and two regions beats three.
    """
    fw, fh = int(frame_size[0]), int(frame_size[1])
    boxes = []
    for g in gauges:
        r = _rect_of(g)
        if r[2] <= r[0] or r[3] <= r[1]:
            continue
        boxes.append([r, [g]])

    if not boxes:
        return [], (0, 0)

    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if _near(boxes[i][0], boxes[j][0], merge_gap):
                    boxes[i][0] = _union(boxes[i][0], boxes[j][0])
                    boxes[i][1].extend(boxes[j][1])
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break

    # Force the count down to the cap, merging the pair that wastes least.
    while max_regions and len(boxes) > max_regions:
        best, best_waste = None, None
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                u = _union(boxes[i][0], boxes[j][0])
                waste = (_area(u) - _area(boxes[i][0]) - _area(boxes[j][0]))
                if best_waste is None or waste < best_waste:
                    best, best_waste = (i, j), waste
        i, j = best
        boxes[i][0] = _union(boxes[i][0], boxes[j][0])
        boxes[i][1].extend(boxes[j][1])
        del boxes[j]

    regions = []
    for (x0, y0, x1, y1), members in boxes:
        x0 = max(0, min(x0, fw))
        y0 = max(0, min(y0, fh))
        x1 = max(0, min(x1, fw))
        y1 = max(0, min(y1, fh))
        if x1 > x0 and y1 > y0:
            regions.append(Region(x=x0, y=y0, w=x1 - x0, h=y1 - y0,
                                  gauges=list(members)))

    if not regions:
        return [], (0, 0)

    # Draw order must survive the grouping, or a gauge that legitimately draws
    # over another would end up underneath it.
    order = {id(g): i for i, g in enumerate(gauges)}
    for r in regions:
        r.gauges.sort(key=lambda g: order[id(g)])
    regions.sort(key=lambda r: min(order[id(g)] for g in r.gauges))

    total = sum(r.area for r in regions)
    if total > full_frame_above * fw * fh:
        # Not worth slicing up; one region covering everything.
        regions = [Region(x=0, y=0, w=fw, h=fh, gauges=list(gauges))]

    ay = 0
    for r in regions:
        r.ax, r.ay = 0, ay
        ay += r.h
    atlas = (max(r.w for r in regions), ay)
    return regions, atlas


class RegionCompositor:
    """Draws the enabled gauges into a packed atlas surface.

    Gauges keep drawing in frame coordinates; each region is drawn under a
    translation that lands it in its atlas slot, so nothing in the gauges has to
    know the atlas exists.
    """

    def __init__(self, cfg, width, height, fps, trackmap, gauge_list=None,
                 merge_gap=DEFAULT_MERGE_GAP, max_regions=None):
        from . import gauges as gauges_mod

        self.cfg = cfg
        self.frame_w, self.frame_h = int(width), int(height)
        ctx = {"trackmap": trackmap, "fps": fps,
               "dim_alpha": cfg.quality.dim_alpha, "skipped": []}
        self.gauges = (gauge_list if gauge_list is not None
                       else gauges_mod.build(
                           cfg, gauges_mod.Layout(self.frame_w, self.frame_h),
                           ctx))
        self.skipped = ctx["skipped"]

        if max_regions is None:
            max_regions = int(getattr(cfg.encode, "max_overlay_regions",
                                      DEFAULT_MAX_REGIONS))
        self.regions, (self.atlas_w, self.atlas_h) = plan_regions(
            self.gauges, (self.frame_w, self.frame_h), merge_gap,
            max_regions=max_regions)
        if not self.regions:
            raise ValueError("no enabled elements, so there is no overlay to draw")

        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                          self.atlas_w, self.atlas_h)
        self.ctx = cairo.Context(self.surface)
        # Warm the static layers so frame 0 is not charged for them and a layout
        # error surfaces before any encoding starts.
        for g in self.gauges:
            g.static()

    # -- introspection ----------------------------------------------
    @property
    def coverage(self):
        """Fraction of the frame the overlay actually touches."""
        return sum(r.area for r in self.regions) / float(self.frame_w * self.frame_h)

    @property
    def bytes_per_frame(self):
        return self.atlas_w * self.atlas_h * 4

    @property
    def channels(self):
        out = set()
        for g in self.gauges:
            for c in g.channels:
                out.add(c)
        return sorted(out)

    def describe(self):
        return {"frame": [self.frame_w, self.frame_h],
                "atlas": [self.atlas_w, self.atlas_h],
                "coverage": round(self.coverage, 4),
                "bytes_per_frame": self.bytes_per_frame,
                "regions": [r.as_dict() for r in self.regions]}

    # -- drawing ----------------------------------------------------
    def draw(self, row):
        ctx = self.ctx
        for region in self.regions:
            ctx.save()
            ctx.translate(region.ax - region.x, region.ay - region.y)
            ctx.rectangle(region.x, region.y, region.w, region.h)
            ctx.clip()
            for g in region.gauges:
                # Per-gauge clip as well: a gauge that drew outside its own box
                # would never have those pixels cleared again.
                gx, gy, gw, gh = g.bbox
                if gw <= 0 or gh <= 0:
                    continue
                ctx.save()
                ctx.rectangle(gx, gy, gw, gh)
                ctx.clip()
                g.draw(ctx, row)
                ctx.restore()
            ctx.restore()
        self.surface.flush()

    def buffer(self):
        return self.surface.get_data()

    def region_views(self):
        """Per-region ``(Region, bytes, stride)`` for a consumer that wants the
        pieces separately rather than the packed atlas."""
        self.surface.flush()
        data = self.surface.get_data()
        stride = self.surface.get_stride()
        out = []
        for r in self.regions:
            rows = []
            for row_i in range(r.h):
                start = (r.ay + row_i) * stride + r.ax * 4
                rows.append(data[start:start + r.w * 4])
            out.append((r, b"".join(rows), r.w * 4))
        return out

    def write_region_pngs(self, prefix):
        """One PNG per region -- the simplest possible preview consumer, and a
        way to eyeball what the atlas holds."""
        paths = []
        for i, r in enumerate(self.regions):
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, r.w, r.h)
            c = cairo.Context(surf)
            c.set_source_surface(self.surface, -r.ax, -r.ay)
            c.paint()
            surf.flush()
            p = "%s_r%d_%dx%d_at_%d_%d.png" % (prefix, i, r.w, r.h, r.x, r.y)
            surf.write_to_png(p)
            paths.append(p)
        return paths


def filter_graph(regions, fps, width, height, unpremultiply=True):
    """The ffmpeg filter_complex that composites an atlas onto the source.

    Input 0 is the source video, input 1 the atlas. Each region is cropped out
    of the atlas and overlaid at its destination.
    """
    base = ("[0:v]fps=%.6f,scale=%d:%d,setsar=1[base]" % (fps, width, height))
    parts = [base]

    pre = "[1:v]"
    if unpremultiply:
        # Cairo's ARGB32 is premultiplied; ffmpeg's bgra is straight alpha.
        pre += "unpremultiply=inplace=1,"
    else:
        pre += "null,"

    n = len(regions)
    if n == 1:
        r = regions[0]
        if (r.x, r.y, r.w, r.h) == (0, 0, width, height):
            parts.append(pre.rstrip(",") + "[ov]")
            parts.append("[base][ov]overlay=0:0:format=auto:eof_action=endall[v]")
            return ";".join(parts)
        parts.append("%scrop=%d:%d:%d:%d[r0]" % (pre, r.w, r.h, r.ax, r.ay))
        parts.append("[base][r0]overlay=%d:%d:format=auto:eof_action=endall[v]"
                     % (r.x, r.y))
        return ";".join(parts)

    labels = "".join("[a%d]" % i for i in range(n))
    parts.append("%ssplit=%d%s" % (pre, n, labels))
    for i, r in enumerate(regions):
        parts.append("[a%d]crop=%d:%d:%d:%d[r%d]" % (i, r.w, r.h, r.ax, r.ay, i))
    cur = "[base]"
    for i, r in enumerate(regions):
        out = "[v]" if i == n - 1 else "[b%d]" % i
        parts.append("%s[r%d]overlay=%d:%d:format=auto:eof_action=endall%s"
                     % (cur, i, r.x, r.y, out))
        cur = out
    return ";".join(parts)
