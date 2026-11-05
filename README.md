# LapVIEW

> **`DESIGN.md` is the design of the product**: the Track / Day / Session / Lap /
> Split hierarchy, the lap as the atomic stored unit, the ingest pipeline, the
> library layout, and how a track is identified. This file records how the pieces
> that exist were built and what was measured while building them.

Regenerates RaceChrono-style overlaid track videos on the desktop from **CSV
telemetry + raw video only**. No RaceChrono app, no VBO, no `.rcz`.

Two deliverables: a render pipeline (`main.py`) and a track-map generator
(`tools/generate_map.py`).

---

## Quick start

### Start the server

```bash
python -m webui.server
```

It serves <http://127.0.0.1:8770> and opens a browser there. Ctrl-C stops it.
Two tabs: **laps** plays and compares what the library holds, **library** renames,
tags, deletes and imports.

```bash
python -m webui.server --port 8790            # another port
python -m webui.server --no-open              # do not launch a browser
python -m webui.server --quiet                # no request log
python -m webui.server --library "D:/laps"    # serve a library elsewhere
python -m webui.server --maps Maps            # override CONFIG['maps_dir']
python -m webui.server --host 0.0.0.0         # reachable from the network
```

Defaults are `--port 8770`, `--host 127.0.0.1`, and the `library_dir` and
`maps_dir` from `CONFIG` in `main.py`. `--host 0.0.0.0` serves the library to
anything that can reach this machine and there is no authentication, so keep it
for a LAN you trust.

Deep links: `/#library` opens the library tab, `/#compare` the laps tab with a
comparison already up.

### Fill the library

Open the **library** tab, choose a folder of RaceChrono CSVs and footage, name the
event and the track, and press Import. Nothing in that folder is copied, moved or
deleted — the import says when the library holds a verified copy and leaves the
originals to you.

The same import from a terminal, which is what the tests drive:

```bash
# --track and --event are what the import screen requires; --dry-run first
python tools/ingest.py "D:/track day" --track "AMP Full" --event "Track Night" --dry-run
python tools/ingest.py "D:/track day" --track "AMP Full" --event "Track Night"
python tools/ingest.py "D:/day" --track "GRIP Season 5 - Event 6" --event "GRIP Season 5 - Event 6" --skip-edge-laps --keep-best 3

python tools/library.py                        # what the library holds
python tools/smoke.py --keep                   # a server on a throwaway library
```

### Export an overlaid video

In the app, from the library — no folder of originals needed:

- **laps tab** → **export** renders the lap you are watching.
- **library tab** → select laps, or a whole event, → **export video**.
- Shift-click either button for a six-second preview instead of the whole lap.

Finished files land in `Output/` and are listed under **exports** with their size
and render speed, each with **show in folder**.

The older pipeline still renders from a folder of original sessions and footage,
which is what to reach for when the laps were never imported:

```bash
# Build a track map from every run in a folder (once per track)
python tools/generate_map.py --runs "D:/track day/*.csv" \
    --name "AMP Full" --out Maps/amp_full.json --points 720

python main.py "D:/track day" --dry-run                    # pairing and sync
python main.py "D:/track day" --sync-check                 # contact sheet
python main.py "D:/track day" --preview 20 --at 600 --overwrite
python main.py "D:/track day"                              # render everything
python main.py "D:/track day" --concat                     # join a run's parts
```

`--dry-run` is the fastest way to catch a bad pairing: it prints, per run, the
CSV path, every detected channel with its units, the lap count, the matched
clips with resolved offsets and sync confidence, the selected map, and the
estimated output duration.

## Requirements

- Python 3.11+ with `numpy`, `pandas`, `pycairo`, `scipy`
- `ffmpeg` and `ffprobe` on `PATH`, or set `CONFIG["ffmpeg"]`

```bash
pip install -r requirements.txt
```

If ffmpeg is not on `PATH` the pipeline also looks in a few common Windows
install locations before giving up. Point `CONFIG["ffmpeg"]["ffmpeg"]` at a
binary or a directory to be explicit.

## Running on a server (Docker)

The web app also runs as a container on a Linux box and is used from a phone
or another computer's browser. The `Dockerfile` builds it (Debian 13, Python
3.12, Debian's ffmpeg, which has `h264_nvenc`/`hevc_nvenc`):

```bash
docker build -t lapview .
docker run -d --name lapview --restart unless-stopped --gpus all \
    -p 8770:8770 -v /mnt/bulk/lapview:/data \
    -e LAPVIEW_NVENC_PROFILE=pascal lapview
```

`--gpus all` needs the NVIDIA driver and `nvidia-container-toolkit` on the
host. Without it the app still runs and encodes on the CPU. The container runs
as uid/gid 1000, so the host folder has to be writable by 1000.

Everything it keeps is under `/data`:

| path | what |
|---|---|
| `/data/library/` | the lap library: `library.db`, `laps/<id>/`, `tracks/<id>/` |
| `/data/output/` | exported videos, downloadable from the library tab |
| `/data/inbox/` | uploads, one folder per upload (`2026-11-03_141502_name/`); `.partial/` holds files still arriving |
| `/data/cache/` | scratch for exports |
| `/data/maps/` | optional; without it the maps shipped in `Maps/` are used |

**Uploading.** In server mode the upload card is the first thing on the
library tab (`/#add` opens it directly). Choose files (on an iPhone, from
Photos or Files; a camera's `.insv`/`.lrv` may need "choose any file type"),
or drag them in on a desktop. Choosing again adds to the same upload, which is
how a phone sends the RaceChrono CSV from Files and the video from Photos. Each
file goes up in 8 MB chunks that are retried if the connection drops, and lands
in the folder only when all of it has arrived. Keep the page open and the
screen on while it uploads (the page asks the phone to keep the screen awake).
Uploads that were imported can be deleted from the same card.

**One-tap import.** When an upload finishes, the server plans the import
(writing nothing) and the card shows the result: the track (a library track the
sessions matched by shape, or the CSV's `Track name` for a new one), the laps,
how many have video, and an event name. The event is the one already filed on
that date if there is one, otherwise the CSV's `Session title` with the date.
Both names can be edited. **Import** runs it with the import form's current
options, and **more options** opens the full form. A session with no matching
video, or a session that fits no track, is reported on the card. With **import
by itself** ticked (remembered per browser), an upload that has a CSV, its
video and no problems is imported with no tap at all.

**Exports** have a **download** link (byte ranges, so a dropped phone download
resumes), and files from earlier sessions are listed too. "Show in folder" and
the folder chooser only exist on a desktop.

**Server mode** is on whenever `LAPVIEW_DATA_DIR` is set, on Linux with no
display, with `--headless`, or with `LAPVIEW_HEADLESS=1` (`0` turns it off). It
hides the desktop-only controls and confines browsing and importing to the
inbox, the library and any `LAPVIEW_BROWSE_ROOTS` -- the browser cannot read
anything else on the box. There is no login: keep the port on a private
network (Tailscale, a LAN), not the internet.

**Other sites cannot drive it.** No login also means any page open in the same
browser can send requests to the app's address. Everything that changes
something (renaming, tagging, deleting, resetting, importing, exporting, the
folder chooser, uploads) is a `POST` with a JSON body and an `X-LapVIEW: 1`
header. Another site can make a browser send a `GET` (a link, an image) or a
plain form `POST`, but it can't add that header without a CORS preflight, and
the server never answers one. A `GET` of one of those routes answers
`405 Method Not Allowed` and does nothing. From a terminal:

```bash
curl -X POST -H 'X-LapVIEW: 1' -H 'Content-Type: application/json' \
     -d '{"confirm": "yes"}' http://<server>:8770/api/library/reset
```

### Add to Home Screen

On an iPhone, open the app in Safari (`http://<server>:8770`), tap **Share**,
then **Add to Home Screen**. The icon opens LapVIEW full screen, with no Safari
toolbar. The header and footer keep clear of the notch and the home indicator,
in portrait and in landscape. If the server is on a tailnet, Tailscale has to be
on, as it does in Safari.

It is still the live page on the server, not an offline copy. Working offline
would need a service worker, and browsers only allow those over HTTPS, while
this app is plain HTTP. So there is no service worker. To reload the app, close
it in the app switcher and open it again. The icons are drawn by
`tools/make_icons.py`.

**Health.** `GET /api/health` reports the encoder that was chosen and why, the
ffmpeg it found, whether each data folder is writable, and free space. It
answers 503 if ffmpeg is missing or a folder is not writable; the image's
`HEALTHCHECK` uses it.

### Environment variables

All optional. Unset, nothing changes from the desktop behaviour.

| variable | default | |
|---|---|---|
| `LAPVIEW_DATA_DIR` | unset (`/data` in the image) | root for `library/`, `output/`, `cache/`, `inbox/`, `maps/` |
| `LAPVIEW_LIBRARY_DIR`, `_OUTPUT_DIR`, `_CACHE_DIR`, `_INBOX_DIR`, `_MAPS_DIR` | under the data dir | one folder each, overriding the data dir |
| `LAPVIEW_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | address to listen on |
| `LAPVIEW_PORT` | `8770` | port |
| `LAPVIEW_HEADLESS` | see above | `1` server mode, `0` desktop |
| `LAPVIEW_BROWSE_ROOTS` | none | extra folders a server may import from, `:`-separated (`;` on Windows) |
| `LAPVIEW_ENCODER` | `hevc_nvenc` | `hevc_nvenc`, `h264_nvenc`, `libx265` or `libx264`, for import and export |
| `LAPVIEW_ENCODER_PRESET` | `p7` for NVENC, `medium` for x264/x265 | |
| `LAPVIEW_NVENC_PROFILE` | `auto` | `pascal` for a GTX 10-series, `turing` for RTX and newer; `auto` asks nvidia-smi |
| `LAPVIEW_NVENC_TEMPORAL_AQ` | `1` | `0` turns temporal AQ off (always off for HEVC on Pascal) |
| `LAPVIEW_HWACCEL_DECODE` | `1` | `0` decodes on the CPU in exports |
| `LAPVIEW_CPU_FALLBACK` | `auto` | encoder when NVENC will not open: `auto` (same codec), `libx264`, `libx265`, `none` |
| `LAPVIEW_FFMPEG`, `LAPVIEW_FFPROBE` | PATH | explicit binaries |
| `LAPVIEW_FONT` | Segoe UI on Windows, else DejaVu Sans | gauge font |
| `LAPVIEW_UPLOAD_MAX_GB` | `64` | largest single upload |

The command line still wins over the environment (`--host`, `--port`,
`--library`, `--maps`).

## Configuration

`main.py` opens with a single `CONFIG` dict — edit that file directly. There is
no YAML and no CLI flag for layout. `config.py` validates it on load and
**rejects unknown keys and out-of-range values**, so a misspelled layout key is
reported rather than silently ignored.

All geometry is in fractions of frame **height**, except horizontal anchors
which are fractions of frame **width**, so a layout survives a resolution
change. `tools/selftest.py` asserts this by comparing 720p against 4K.

Elements are registered by name in `racechrono/gauges/__init__.py`, so enabling,
disabling, moving or resizing one never touches render code. `lap_timer`,
`sector_delta`, `speed_bar` and `coolant_temp` are registered stubs: the slots
exist, nothing is drawn, and enabling one fails with a clear message rather than
obscurely at draw time.

### The gauge row

Left to right: **G-meter, speedometer, tachometer, throttle**. That is
symmetric, so the frame's centreline belongs *between* the speedo and the tacho
rather than through either of them.

Only the speedo carries an absolute position, and it is expressed as an offset
from frame centre in its own diameters; everything else is placed relative to
the speedo. So the whole group stays centred and mirrored at any resolution and
any aspect ratio — verified at 1080p, 720p, 4K and 2560x1080 ultrawide, where
the midpoints land on the centreline to within half a pixel.

```
gmeter    0.5W - 1.60 D        speedo   0.5W - 0.65 D
tacho     0.5W + 0.65 D        throttle 0.5W + 1.60 D      (D = speedo diameter)
```

Gauges clear their own bounding box, so a box overlapping a neighbour's wipes
part of it depending on draw order. The speedo and G-meter boxes do overlap, by
14x229 px, but only across padding — `tools/selftest.py` draws each gauge alone
and asserts nothing it draws goes missing from the combined frame, so tightening
the spacing until the overlap eats real pixels fails the suite rather than
shipping.

### Rescaling a sender that does not use its full range

The throttle pedal sender on this car does not swing 0-100. Across all four
sessions (203k samples) it rests at **11.37%** and pegs at **85.49%** wide open,
and both are hard plateaus — p95, p99 and p99.9 are all exactly 85.49, with 6.2%
of the session sitting there.

`input_min` and `input_max` name those rails and the dial rescales linearly onto
its own `min`..`max`, so closed reads 0 and wide open reads a true 100:

```python
"throttle": {..., "min": 0, "max": 100,
             "input_min": 11.37, "input_max": 85.49},
```

They are generic dial options, not throttle-specific. Readings outside the rails
are clamped, since once rescaled they are just the sender overshooting — but
clamping only turns on when rails are configured, so a speedo is still free to
print 134 mph on a dial that sweeps to 120.

A dial's `label` is separate from its `unit`: `unit` selects the conversion
(m/s to mph), `label` is the word printed in the middle. The throttle dial says
"throttle" and converts nothing.

## Verifying a change

```bash
python tools/selftest.py             # 98 checks, no server needed, 1m45
python tools/selftest.py storage     # just one area, 27s -- run what you touched
python tools/selftest.py --list      # the areas
python tools/smoke.py                # server up, every route, 6 screenshots, 30s
```

98 checks covering config validation, the element registry (disable/move/
resolution independence/in-frame bounds), channel-set robustness, unit
validation, circular interpolation of `bearing`, map-transform stability,
marker closure over a lap, start/finish recovery, that a session pause is never
interpolated across, that the trim never cuts inside the driving, that segments
tile their window, that a lap spanning a camera split produces two parts, that
the gauge row stays symmetric about the frame centreline, that the throttle
rescale hits 0 and 100 at the measured rails, that no gauge erases a neighbour,
that the region atlas is pixel-identical to the full frame, and that the text
report renders the same numbers the job API returns.

---

## Results on the AMP session

Four CSVs and seven camera clips in; four videos out.

| session | output | source clips | size | bitrate |
|---|---|---|---|---|
| 15:58 | 1463.0 s | 2, joined | 2.49 GB | 14.6 Mbps |
| 16:54 | 1131.8 s | 2, joined | 2.04 GB | 15.5 Mbps |
| 18:17 | 940.7 s | 1 | 1.65 GB | 15.1 Mbps |
| 19:14 | 1079.0 s | 2, joined | 1.83 GB | 14.5 Mbps |

4614.5 s of video, **8.60 GB**, in 19.4 min wall (3.96x realtime aggregate).
Trimming removed 1039 s of dead air and the encoder change did the rest: the same
footage previously came to 19.2 GB across seven files.

Verified on the finished outputs, against telemetry computed independently of the
render:

| output | offset | telemetry | overlay |
|---|---|---|---|
| 18:17 | 0.5 s | 0.0 mph, 917 rpm, 0.00 G | 0 / 917 / 0.0 |
| 18:17 | 500 s | 49.8 mph, 5891 rpm, 0.89 G | 50 / 5891 / 0.9 |
| 19:14 | 0.5 s | 0.0 mph, 901 rpm, 0.00 G | 0 / 900 / 0.0 |
| 19:14 | 900 s | 83.2 mph, 4993 rpm, 0.58 G | 83 / 4992 / 0.6 |

That is the check that matters for trimming: the 19:14 output seeks 350.6 s into
its first clip and is joined from two, so a frame-rounding error anywhere in the
seek or the join would show up here as a shifted needle.

## Notes from building this against the AMP session

Things the data turned out to require that were not obvious up front.

### The CSV has a third header row

The documented layout is metadata, column names, units, data. Every v10.2.4
export here also carries a **device row** between the units and the data:

```
timestamp,fragment_id,...,rpm,speed,throttle_pos
unix time,,,...,rpm,m/s,%
,,,,,100: gps,...,calc,...,200: obd,200: obd
1787169539.807,0,,...
```

That row is more useful than a nuisance: it says authoritatively which device
block each duplicated channel name belongs to, so `speed` → `speed_gps` /
`speed_calc` / `speed_obd` is resolved by lookup rather than by counting
occurrences positionally. The loader finds the header by scanning for
`timestamp`, treats the row after units as a device row when its first cell is
not numeric, and skips any further non-numeric preamble.

Names that appear once keep their bare form, so the tacho's configured
`rpm_obd` resolves through an alias to the actual `rpm` column.

### The accuracy channel floors at exactly 10.0 m

`accuracy` never reports better than 10.0, so the quality gate has to be `<= 10`,
not `< 10`. With `<=` it keeps 92.5–98.0% of rows per run; the rejected tail is
p99 ≈ 27–45 m, which is the paddock. `fix_type` is a constant 1 and `altitude`
a constant 0 in this dataset, so `accuracy` does all the work — `--dry-run`
lists constant channels explicitly for that reason.

### `distance_traveled` accumulates, so `lap_number` is not the only clock

It is monotonic across the whole session (~2929 m per lap, 21–27 km per run),
not reset per lap. Start/finish is still recovered from `lap_number`
increments, but note that `lap_number` is **NaN until the first crossing** — the
NaN → 1 transition is itself a crossing and must be counted, or the first lap
is silently lost. Laps are the spans *between* consecutive crossings, which
excludes the out-lap and the unterminated in-lap by construction.

Averaging 31 crossings across four runs, then intersecting each straddling
sample pair with the line implied by that first estimate, puts the trap well
below single-sample resolution.

### `lean_angle` is derived, not measured

It correlates −0.96 to −0.99 with `lateral_acc`. It is a computed motorcycle
channel with no meaning on a car; ignore it.

### G-meter polarity, measured rather than guessed

Regressing each acceleration channel against its GPS-derived expectation over
all four runs:

| channel | expectation | slope | r |
|---|---|---|---|
| `longitudinal_acc` | `(dv/dt) / g` | **+0.83** | +0.78 … +0.88 |
| `lateral_acc` | `v · ω / g` | **−1.00** | −0.94 … −0.99 |

So positive `longitudinal_acc` is acceleration (no inversion needed), but
positive `lateral_acc` is a **left** turn — `invert_lateral` must be `True` for
the dot to swing right in a right-hander.

Getting the yaw-rate comparison to work needs a wide stencil. GPS `bearing` is
interpolated up from roughly 1 Hz, so an adjacent-sample derivative at 20 Hz is
pure interpolation noise: it gives r ≈ −0.05 and looks like "no correlation".
Differencing over ±0.5 s or more gives r ≈ −0.95.

### Audio sync: correlate against throttle, not speed

The spec's `audio_rpm` strategy works, but two details decide whether it
produces a real answer or a confident wrong one.

**The reference channel must have no intrinsic lag against audio energy.**
Correlating the envelope against `speed_gps` peaks at **+3.1 … +4.0 s** on every
long clip — consistently, with a sharp peak, looking exactly like a real clock
offset. It is not one. It is the physical throttle → speed delay of the car.
`throttle_pos` is instantaneous against exhaust note and peaks at **−0.12 …
+0.24 s**. `rpm` sits in between (+1.1 … +2.4 s) because engine speed lags
throttle through the drivetrain.

**The lap-scale trend has to be high-passed out.** Audio energy and engine load
both rise and fall once per lap, giving a correlation hump over 100 s wide —
useless for locating a one-second offset, and it makes peak-to-second-peak
meaningless because every neighbouring lag is nearly as high. High-passing both
signals at 30 s (subtracting a centred moving average) leaves a peak about 3 s
wide with a prominence of 3.5–4.9 sd.

Confidence is therefore reported as **prominence in standard deviations** over a
wide (±60 s) lag range, with the spec's peak-to-second-peak ratio kept as a
second gate against a doubled lock. The accepted peak is still confined to
±`search_window_s`.

Result for this session — the camera clock was already good:

| clip | offset | prominence | ratio |
|---|---|---|---|
| WGLZ0137 | −0.091 s | 4.9 sd | 2.51 |
| WYZY6324 | −0.030 s | 4.2 sd | 2.08 |
| NGRX6938 | +0.243 s | 4.1 sd | 2.85 |
| MOLE9023 | −0.107 s | 4.2 sd | 2.62 |
| KRVJ7224 | +0.595 s | 4.2 sd | 2.09 |
| CJHC9467 | +0.095 s | 3.5 sd | 2.38 |
| IXRX2443 | +0.095 s | (consensus) | — |

A NaN trap is worth knowing about: OBD channels drop out for a few samples at a
time, `np.interp` propagates those NaNs through the whole correlation, and
`np.argmax` of an all-NaN array returns index 0 — a wrong answer that looks like
an answer. Non-finite samples are dropped before interpolation.

**Consensus.** Every clip in a run comes off one camera in one sitting, so their
true offsets are identical to well within a frame. IXRX2443 is 43 s long and has
no hope of locking on its own; it adopts the median of its siblings' good locks,
and a disagreement wider than 1 s refuses to share rather than propagating a bad
lock.

Manual offsets always win and are written back to `cache/sync.json` in the same
format, so one hand correction stays corrected — `--refresh-sync` will not
override it. Use `--clear-sync` to forget a cached offset and go back to auto.

### Map orientation is north-up, not PCA

The spec calls for PCA-orienting the long axis horizontally. Doing that to AMP
gives an aspect ratio of 2.43 — a wide, short map that does not resemble the
reference screenshot at all. The reference is portrait, i.e. true GPS
orientation, at aspect 0.90. North-up is the default; `--orient pca` still
exists, and `--both-orientations` writes both with SVG previews so they can be
compared.

Map quality on this session: 15 laps of 27 survive filtering (the first run's
157–212 s laps are rejected against a 120 s median), lap-to-lap spread from the
median line is 2.6 m at p50 and 9.9 m at p95, and a live marker tracks the
stored line to within 7.3 m while closing a lap to 1–2 m against a marker
footprint of 54 m.

### A gauge must never draw outside the rectangle it clears

The frame loop clears each gauge's bounding box and redraws it, rather than
clearing the whole frame. That makes the invariant load-bearing: anything drawn
outside a gauge's own box is never cleared again and stays burnt into every
later frame.

It was violated by the map marker. The map is the median racing line, so the pit
lane sits just outside its extent -- and for the 8-21 s of pit exit at the start
of a session the marker projected up to 26 px past the map, overshooting the
cleared box by 18 px. The tip of the arrow stayed on screen below the map's
bottom-right corner for the rest of the video.

Fixing it needed two things, and measuring showed both are load-bearing:

| | residue left behind |
|---|---|
| neither | 180 px |
| clip drawing to the box | 27 px |
| snap boxes to whole pixels | 153 px |
| **both** | **0** |

Clipping alone is not enough because the boxes had fractional bounds: a fill or
a clip on a fractional edge is antialiased, so the boundary row is only partly
covered and the old frame bleeds through it. Snapping every box outwards to
whole pixels makes the clear, the clip and the static blit land on the same
pixels.

Clipping is applied by the compositor to every gauge, so the invariant holds for
elements added later without them having to think about it. Separately, the
marker is clamped to the map rectangle (`clamp_marker`), which is cosmetic
rather than correctness -- it keeps a whole arrow pinned at the edge instead of
half of one sliding off. During a lap the marker is far inside the box and
neither ever applies.

### Premultiplied alpha

Cairo's `ARGB32` is premultiplied; ffmpeg's `bgra` is straight alpha. Handing
one to the other as-is darkens every antialiased edge — a visible halo around
white text over bright video. The overlay is un-premultiplied inside the filter
graph (`unpremultiply=inplace=1`) so the correction costs nothing in Python.

### Clip gaps are real

The camera splits at about 2 GB, leaving 1.2–1.9 s between files. Those gaps are
reported and never papered over: stretching data across one would desynchronise
everything after it. Output is per-clip, with `--concat` to join finished parts.

---

## Layout and the reference screenshot

CONFIG ships the spec's defaults: `diameter` 0.26 of frame height, tacho at
+1.30 diameters from the speedo, G-meter at −0.95.

Measuring the reference screenshot's own video area gives noticeably tighter
numbers — diameter ≈ 0.196 H, tacho ≈ +1.43 D, G-meter ≈ −1.16 D. The reference
is a phone screenshot of RaceChrono rendering to a portrait phone screen, so its
overlay scale is relative to that screen rather than to the video frame, and the
two need not agree. If you want the tighter look:

```python
"speedo": {..., "diameter": 0.196},
"tacho":  {..., "diameter": 0.196, "offset_d": 1.43},
"gmeter": {..., "diameter": 0.098, "offset_d": -1.16},
```

Check either with `--preview`, never a full render.

## Measured throughput

Two different things limit this pipeline depending on how many outputs run at
once, and they pull in opposite directions.

**One output at a time: the Python frame loop is the limit.** Every NVENC setting
tried -- presets `p4` through `p7`, multipass off / `qres` / `fullres` -- rendered
at 1.9-2.4x realtime and produced the same 74.2 MB. The encoder was never the
constraint, so there is no reason not to use the slowest, best preset.

**Four outputs at once: NVENC is the limit.** Four concurrent 1080p60 HEVC `p7`
encodes push the encoder to ~78% while the GPU's compute units sit at ~19%, and
per-output throughput falls from 2.2x to 1.05x. Aggregate still improves (about
4.2x), but not fourfold.

| configuration | per output | aggregate |
|---|---|---|
| 1 worker, hevc p7 | 2.2-2.4x realtime | 2.3x |
| 4 workers, hevc p7 | 1.05x realtime | ~4.2x |
| 7 workers, h264 p5 *(older run)* | 0.6-0.96x realtime | 3.9x |

If you want more throughput the lever is the preset, not the worker count: `p5`
or `p6` cost nothing measurable on a single output but free real encoder capacity
when several run together. Reducing `CONFIG["workers"]` raises per-output speed
without helping the total.

Beyond that, the frame loop's own ceiling comes from pushing a full 1920x1080
RGBA frame down a pipe -- 8.3 MB, 60 times a second. The overlay only covers the
map box and the gauge row, together under a fifth of the frame, so sending those
as two small overlay inputs instead of one full-frame stream is the obvious next
optimisation if it is ever needed.

## Encoding

Measured on a 30 s window of a fast lap, every candidate scored against a
visually lossless `libx264 -crf 12` encode of the same overlaid frames:

| setting | Mbps | PSNR | SSIM |
|---|---|---|---|
| h264 cq21 p5, no tuning *(the old default)* | 37.5 | 44.51 | 0.9881 |
| h264 cq21 p7 tuned | 31.8 | 44.06 | 0.9870 |
| h264 cq25 p7 tuned | 19.6 | 41.96 | 0.9804 |
| h264 cq28 p7 tuned | 13.4 | 40.33 | 0.9726 |
| **hevc cq26 p7 tuned (default)** | **15.0** | **42.14** | **0.9812** |
| hevc cq28 p7 tuned | 12.0 | 41.27 | 0.9777 |
| hevc cq31 p7 tuned | 8.2 | 39.77 | 0.9697 |

Two things come out of that. The tuning alone -- the slowest preset, a 32 frame
lookahead, spatial *and* temporal AQ, and B-frames usable as references -- saves
15% at identical quality, so it is on by default. And HEVC is strictly better per
bit: `hevc cq26` beats `h264 cq25` on quality while being 23% smaller.

`multipass` is **off**, against the usual advice. Measured across `p4`..`p7` with
multipass off, `qres` and `fullres`, every combination produced the same 74.2 MB
at the same quality: in constant-quality mode there is no bitrate target to hit,
so the extra pass has nothing to do. It is the most encoder-expensive option
NVENC offers, and turning it off frees the encoder for parallel outputs. Turn it
on only if you also set `maxrate`.

The default is **`hevc_nvenc` at `cq 26`, about 15 Mbps**. That is above the
source footage's own 13.2 Mbps and above YouTube's 12 Mbps recommendation for
1080p60, indistinguishable from the reference at 1:1, and 2.5x smaller than the
previous default. For this session it turns 19.2 GB of output into about 8.5 GB.

To go smaller, `cq 28` (12 Mbps) is still visually clean and `cq 31` (8 Mbps) is
usable; both are in the table above so the trade is a known quantity rather than
a guess. For maximum compatibility use `"encoder": "h264_nvenc"`. For a CPU
encode use `libx265`/`libx264`, which read `crf` instead of `cq`.

HEVC output is tagged `hvc1` rather than ffmpeg's default `hev1`, which is what
players and YouTube expect.

Audio is copied untouched when a target starts at a clip boundary. When a target
seeks into a clip -- which trimming and lap export both do -- the audio is
re-encoded to AAC at `audio_bitrate` instead: a copied AAC stream can only begin
on a frame boundary, which would leave up to 21 ms of A/V skew per part, and that
would accumulate at every join.

GeForce cards cap concurrent NVENC sessions (8 on current drivers). If an output
fails at encoder init because the sessions are exhausted, it is retried on the
CPU encoder and the substitution is reported rather than the output being lost.

### Older GPUs, and none at all

The tuning above is Turing-and-newer. A Pascal card (GTX 10-series) refuses to
open an HEVC session that asks for B-frames, B-frames as references or temporal
AQ -- measured on a GTX 1070 Max-Q, driver 580: each one alone fails at init
with `-22`, while `p7`, `cq 26`, spatial AQ and a 20-frame lookahead are fine.
`encode.nvenc_profile` (`LAPVIEW_NVENC_PROFILE`) handles that: `pascal` drops
all three for HEVC, keeps B-frames (but not as references) for H.264, and caps
the lookahead at 20. `auto` picks `pascal` or `turing` from the card name
nvidia-smi reports. The import's re-encode gets `-bf 0` on Pascal for the same
reason.

The web server does not trust any of that blindly. At startup it encodes one
second of test pattern with the exact arguments a real export would use, and
walks down until something opens: as configured, without temporal AQ, the
pascal profile, then the CPU (`libx265` for HEVC, `libx264` for H.264, or
`encode.cpu_fallback`). The choice is logged and shown by `/api/health`. If an
NVENC export or import still fails later, it is retried once with the
pascal-safe arguments and then on the CPU.

Imported clips are tagged `hvc1` as well. ffmpeg's default `hev1` is the same
bitstream, but Safari -- every iPhone -- will not play it. Clips imported before
this change can be re-tagged without re-encoding:
`ffmpeg -i video.mp4 -c copy -tag:v hvc1 -movflags +faststart fixed.mp4`.

## Raw IMU: vehicle-frame acceleration

When a session logs the phone's accelerometer and gyroscope, `racechrono/imu.py`
rotates those axes into the car's frame and adds `imu_lateral_acc`,
`imu_longitudinal_acc`, `imu_combined_acc`, `imu_vertical_acc` and
`imu_yaw_rate`. Everything is solved from the data, per run:

- **Up** from the accelerometer during the standstill every run starts with.
- **Gyro bias** from the same window.
- **Forward** -- the one remaining unknown -- by orthogonal Procrustes against
  physics: longitudinal must match `dv/dt`, lateral must match `v * yaw_rate`.

### Why not just use RaceChrono's own channels

The `calc` block is GPS-derived. On a circuit with long corners that is
excellent -- on the AMP sessions `lateral_acc` matched `v * omega` at slope
**-1.00, r -0.99**. On autocross it breaks, because differentiating a 1 Hz
position cannot follow a slalom. Scored against an independent reference on ten
autocross runs:

| | agreement with physics | peak reported |
|---|---|---|
| **raw IMU** | **r = 0.74 … 0.90** | 1.22 – 1.66 G |
| `calc` | r = 0.07 … 0.61 | **2.22, 4.79, 5.93 G** |

No street car pulls 5.93 G. Low-pass filtering the IMU barely changed its
correlation (0.257 to 0.358 even at 0.5 Hz), so vibration was not the
explanation -- `calc` was simply wrong.

### The calibration must be per run

Across ten runs of one session the mount tilt varied 85.5–89.9 deg and gyro bias
0.28–1.31 deg/s, so one calibration cannot serve them all. The solved heading
offset, on the other hand, agreed to within **3 deg** across every trusted run --
independent confirmation that the fit finds a real quantity.

One run of ten is rejected: its "standstill" was not still, giving a gravity
vector of 1.137 g and a heading 85 deg from the others. Being *stopped* is not
being *still*, so the standstill test also requires the accelerometer magnitude
to be steady. A calibration is only trusted when `|g|` is plausible, the fit
clears `min_fit_r`, **and** both axis signs come out physical.

### A reflection, not a rotation

Restricting the fit to rotations produced a frame whose lateral axis matched
physics at r = +0.99 while its longitudinal axis matched at **-0.62** -- correct
cornering, inverted braking, which no rotation can do. The basis is built by
crossing an arbitrary seed with gravity, and whether that lands right- or
left-handed relative to the car depends on how the phone happens to be lying.
Allowing the determinant to be -1 fixed both axes at once: lateral r = +0.991,
longitudinal r = +0.738, joint fit 0.71 to **0.94**. Every run in this session
came out reflected.

Because the calibration anchors both axes to physics it resolves sign itself, so
`invert_lateral` is no longer needed for IMU channels.

### Channel preference

A channel option may name a list, resolved per run against what that session
actually carries:

```python
"lateral_channel": ["imu_lateral_acc", "lateral_acc"],
```

The fallback matters -- the AMP sessions predate the IMU being logged -- and
resolution has to happen per run, because two sessions of the same car can carry
different channel sets. If no candidate is present, binding fails naming the
option rather than rendering something wrong.

## Two loader bugs the new channel set exposed

**Duplicate columns.** RaceChrono renumbered OBD from `200:` to `300:` and added
`200: acc`, `201: gyro`, `202: magn`. Matching device tags against a hard-coded
list of three names sent the new ones to a positional fallback, collapsing three
of the five `device_update_rate` columns onto one name. A duplicated column makes
`values()` return a 2-D frame instead of a series, which crashed `resample()`
outright. The block name is now parsed off the tag, so an unseen device still
gets a usable suffix, and uniqueness is enforced regardless.

**Silent step-filling.** `resample()` listed the continuous channels and
step-filled everything else, so nine genuinely continuous 100 Hz channels were
held constant between samples. The discrete channels are the small closed set --
`lap_number`, `fragment_id`, `fix_type` -- so that is what is named now, and
continuous is the default.

## Open courses

An autocross course starts and finishes in different places and each run crosses
the start line once, so there are no laps to average. `tools/generate_map.py`
detects that -- no run completing a lap means an open course -- and averages
whole runs instead. `--course` and `--circuit` override the guess.

```bash
python tools/generate_map.py --runs "Raw Data/*.csv" --name "Grip 8-22"     --out Maps/grip_8_22.json --points 480
```

### Align by projection, not by fraction of length

Laps are averaged at matching fractions of their own length. That quietly fails
for open courses, because two runs of the same course differ in path length by
about 8%, so the yardstick stretches and the runs slide out of step. Measured on
ten runs, the deviation from the averaged line grew smoothly from 2.5 m at the
start to 17.6 m at the end -- which reads like the course falling apart when it
is really the parameterisation drifting.

Projecting each run onto a reference polyline gives every sample a
distance-along-the-reference coordinate instead, which does not drift:

| alignment | deviation p50 | p95 |
|---|---|---|
| fraction of each run's length | 3.74 m | 16.5 m |
| **projection onto a reference** | **0.81 m** | **3.13 m** |

Two or three passes converge: project, resample on the shared coordinate, take
the median, repeat. `geo.project_to_polyline` is the same map-matching primitive
a delta-to-best trace will need.

#### Closed circuits had the same problem, hidden at the line

The circuit path kept the fraction-of-length method on the assumption that laps
of the same circuit are close enough in length for it not to matter. They are
not: nineteen AMP laps spanned 2858.6 to 2942.3 m, 2.86%. Both methods anchor at
the start line, so the error is smallest exactly where it is easiest to check,
and worst in the middle of the lap:

| deviation from the median line, p95 | at the line | 1/4 round | 1/2 | 3/4 |
|---|---|---|---|---|
| fraction of each lap's length | 3.4 m | **24.5 m** | 15.4 m | 11.0 m |
| projection onto a reference | 3.6 m | 1.7 m | 2.2 m | 3.4 m |

Over the whole lap, on the map actually shipped (15 laps of 27 after filtering):

| alignment | spread p50 | p95 |
|---|---|---|
| fraction of each lap's length | 2.57 m | 9.91 m |
| **projection onto a reference** | **0.96 m** | **3.73 m** |

`build()` now defaults to `align="projection"`; `--align fraction` keeps the old
method for comparison. Phase alignment (`_align_phase`, which cross-correlated
each lap against the first to undo the one-sample lateness of crossing
detection) is not needed in the projection branch -- the reference lap starts at
its own crossing, so the arc-length coordinate is already shared.

### Finding the two ends

The run is not the course. The car approaches the start line and rolls out past
the finish, and those parts are neither timed nor consistent -- the end points
scatter over 33.9 m against 0.6 m at the start.

- **The head** comes from the start-line crossing. `lap_number` goes NaN to 1
  exactly once per run, which marks it precisely. Including the approach dragged
  the first points of the averaged line 13.6 m off, because approach samples
  project onto the reference's first point and pile up there.
- **The tail** is cut where the runs stop agreeing within `--max-spread-m`
  (default 5 m). On this session that trimmed the last 2.3%.

### Smoothing must not wrap

The Savitzky-Golay pass padded the ends by wrapping, which is correct for a lap
and wrong for an open course: it splices the finish onto the start, mixing finish
coordinates into the first points and start coordinates into the last. That put a
**14 m spike on each end** of a line whose points are 1.06 m apart, and inflated
the reported course length by **59 m**. Open courses get edge padding, and a
selftest checks the resulting spacing stays uniform.

Result on this session: a 494 m course from all ten runs, every traversal within
3.72 m of the averaged line at p90, ends 58.4 m apart, step spacing 1.06 m median
and 1.16 m worst.

## Sessions without laps

An autocross run crosses the line once, so `laps()` is legitimately empty and
`--export laps` produces nothing; the session target still works. Where no map
covers the venue at all, the map element is **left out with a note** rather than
the whole run being refused -- one missing element beats no video.

Short runs also broke audio sync: the +/-60 s noise-floor window does not fit
inside a 43 s run, so it could never run at all. The window now scales to the
overlap. On these runs it still cannot find a confident lock -- 22 s of
correlation gives 1.8–2.7 sd against a 3.5 threshold -- so the gate correctly
falls back to container time rather than applying one of the inconsistent
offsets it found (+1.9, -2.8, -9.9 s).

## Headless job API

The CLI is one caller; a UI is another, and it needs the same information as
data rather than as printed text.

```bash
python -m racechrono.job probe                # what this machine can do
python -m racechrono.job plan   job.json      # the dry-run, as JSON
python -m racechrono.job report job.json      # the same plan, as text
python -m racechrono.job render job.json      # execute
```

A job:

```json
{
  "config":  {},
  "select":  {"runs": ["session_..."], "export": "sessions", "laps": [4, 6]},
  "preview": {"seconds": 20, "at": 600},
  "overwrite": true
}
```

`plan()` returns the whole dry-run: every CSV found, every channel with units
and range, how clips were paired, each clip's sync offset and confidence, the
selected map, and exactly which files would be written from which source
segments, with an estimated size. `main.py --dry-run` now *formats that dict*
rather than computing its own report, so the terminal output and the API can
never disagree -- and a selftest asserts the report's numbers are the plan's
numbers.

`probe()` reports platform, ffmpeg, GPU and — importantly — which encoders
actually work, by running a one-frame encode with each. Compiled-in is not the
same as usable: this build advertises QSV and AMF, and neither functions on this
machine. A UI needs that distinction before it offers a choice, since the answer
decides whether a render takes 20 minutes or 3 hours.

## The overlay contract

The overlay is mostly empty -- the gauges cover about 21% of a 1080p frame -- so
`racechrono/overlay.py` can produce just the dirty regions, packed into one
atlas surface, instead of a whole frame. One producer, two consumers:

- **export**: ffmpeg crops each region out of the atlas and overlays it at its
  destination.
- **preview**: the same regions go to a client over a socket.

Because both consume the same bytes from the same renderer, a preview cannot
drift from an export. That is the entire point, and a selftest asserts the atlas
is pixel-identical to the full-frame reference; an exported segment comes out
bit-identical either way (PSNR = inf).

### Which mode is faster depends on concurrency

Measured on 30 s segments:

| workers | full frame | 2 regions | 3 regions |
|---|---|---|---|
| 1 | **2.35x** | 2.24x | 1.86x |
| 4 | 3.83x | **4.50x** | 3.92x |

Alone, the extra `overlay` filter each region adds costs more than the smaller
payload saves -- every overlay is a pass over the whole base frame however small
the region. Concurrently, the streams contend for memory bandwidth and cutting
each one's payload to 26% wins by 17%. `overlay_regions: "auto"` picks regions
only when more than one render is in flight.

This was worth measuring rather than assuming: the region split was supposed to
speed up export and it does the opposite at one worker.

### Where the time actually goes

| stage | rate |
|---|---|
| Gauge drawing (cairo) | 0.24 ms/frame — **69x realtime** |
| HEVC decode | 13–17x realtime |
| hevc_nvenc encode, p7 | 2.89x realtime |
| hevc_nvenc encode, p5 | 5.62x realtime, **identical file size** |
| Full pipeline, 1 worker | ~2.4x realtime |

Drawing is nowhere near the constraint, so there is no reason to make the gauges
cheaper. The encoder preset is nearly free speed in principle -- p5 encodes
twice as fast as p7 for the same 54.0 MB -- but it barely moves the full
pipeline (3.76x to 3.92x aggregate at 4 workers), because compositing and memory
bandwidth dominate before the encoder does.

## Preview

`tools/preview_server.py` is not the UI. It exists to prove the seam end to end
before any interface is written: a localhost page that plays the source clip with
the overlay drawn on a canvas above it, fed by the same `RegionCompositor` an
export uses.

```bash
python tools/preview_server.py --run session_20260819_181728
python tools/preview_server.py --run session_20260819_181728 --bench 600
```

Measured **831 overlay frames per second** -- 13.9x the 60 fps a realtime
preview needs, at 1.82 MB per frame. The frame loop has ample headroom for an
interactive editor.

## The session view

`webui/` is the app: a localhost page that plays a session with the overlay on
it, beside the track map, the lap list and channel traces on one shared playhead.

```bash
python -m webui.server                       # opens a browser at 127.0.0.1:8770
python -m webui.server --library "D:/laps"   # a library somewhere else
python -m webui.server --port 8790 --no-open --quiet
```

Stdlib only -- `http.server`, no framework, no build step, no package manager.
This has to run for someone who unzipped a folder and typed one command, and
every dependency is a support burden before the tool has done anything useful.
The page is three files: `index.html`, `app.css`, `app.js`.

### One clock

Every time the page handles is **seconds since the run's first sample**. Not unix
time, which is 1.79e9 here and loses precision where a slider and a JSON float
both want a small number; and not video time, because a session is often several
files. The only conversion is at the video element:

```
video_time = t - clip.t0        # clip.t0 already carries the sync offset
```

`webui/session.py` builds the model and does the arithmetic; `webui/server.py`
does routing and bytes. Keeping them apart means the model is testable without a
socket, which is where eight of the checks below live.

| route | what it returns |
|---|---|
| `GET /api/library` | tracks, their days, and the laps under each |
| `GET /api/lap/<id>` | one lap: channels, map geometry, the driven line, its video |
| `GET /api/trace/<id>?ch=&n=` | one channel of a lap, decimated to `n` points |
| `GET /api/comparable/<id>` | every lap at the same track, across every day |
| `GET /api/compare?laps=&ref=` | delta traces, sector table, GPS gaps |
| `GET /api/survey?path=` | how many data and video files a folder holds, and their size |
| `GET /api/browse?path=` | folders under a path, for a picker that cannot use the dialog |
| `GET /api/import?source=&...` | what importing that folder would do, without doing it |
| `GET /api/import/progress` | phase, laps done, bytes written, ETA |
| `GET /api/import/review` | what the last import produced: tracks, maps, laps |
| `GET /api/export?laps=` | what exporting those laps would write |
| `GET /api/export/progress` | how the render is going, and the finished files |
| `GET /api/layout/<id>` | overlay frame size and region geometry |
| `GET /api/overlay/<id>?t=` | the overlay at that moment, packed BGRA |
| `GET /video/<id>` | that lap's video, byte-range served |
| `POST /api/rename` `{kind, id, name}` | rename a track, day, session or lap |
| `POST /api/notes` `{kind, id, text}` | its tags or notes |
| `POST /api/event` `{day, name}` | put a day under an event, creating it if new |
| `POST /api/delete` `{what, ids}` | delete laps (`lap`), their video (`video`), or whole tracks (`track`) |
| `POST /api/pick` `{}` | opens the OS folder chooser and returns what was chosen |
| `POST /api/import/run` `{source, event, track` or `track_id, ...}` | do the import, in the background |
| `POST /api/export/run` `{laps, preview, overwrite}` | render those laps with the overlay burned in |
| `POST /api/reveal` `{path}` | show an export in the desktop's file browser |
| `POST /api/library/reset` `{confirm: "yes"}` | empty the library |
| `POST /api/rescan` `{}` | forget loaded laps and overlays, and return the tree |

The `POST` routes take a JSON body whose names are the query parameters they
used to take, need an `X-LapVIEW: 1` header, and answer `405` to a `GET`; see
"Other sites cannot drive it" above. `ids` and `laps` are a list or a
comma-separated string. `webui/server.py` has the full table, uploads included.

The screen is in the URL — `/#library` for the library tab, `/#compare` for the
laps tab with a comparison already up — so a reload keeps it and a link can point
at one. That is also what makes the page screenshottable from
headless Chrome, which is how the import screen's layout was checked at 560, 760
and 1366 px wide.

`/api/overlay` sends the same bytes an export composites, through the same
`RegionCompositor`. That is the point of the seam: a preview that disagreed with
an output would be a bug in one consumer rather than a difference in what was
drawn. Measured **227 overlay frames per second over HTTP** at 2.84 MB/frame --
3.8x what a 60 fps preview needs.

### Decimation keeps the envelope, not every nth sample

A 21827-sample run drawn into an 800 px trace has to be reduced. Subsampling is
the obvious way and it loses the peaks, which on a brake or throttle channel is
the whole signal. The server sends a min/max pair per bucket instead, so what is
drawn is never quieter than the data: measured on `speed_gps`, the envelope keeps
the maximum to within 0.00% while plain subsampling loses 1.99% of the range.

Traces are scaled to p1..p99 rather than min..max, for the same reason the
G-meter is: one bad GPS sample puts `accuracy` at 48 m and squashes the rest of
the channel into a band.

### The session list has to be cheap

It is the first thing the page asks for, and loading every run the way an export
does took 4.07 s on a fourteen-session folder -- all of it for sessions nobody
had opened.

| | fourteen sessions |
|---|---|
| loaded the way an export does | 4.07 s |
| `derive=False`, so no IMU calibration solved | 1.95 s |
| served from `cache/sessions.json` | 0.08 s |

`derive=False` is safe for a summary because it reports only whether a run
*carries* raw IMU, not what the calibration came out as, and solving one is
0.57 s of the 0.71 s an autocross session takes to read. The cache is keyed on
size and mtime, so replacing one CSV invalidates its own entry and nothing else.
Clip counts are recomputed every time -- footage arrives long after the CSV stops
changing.

### Two bugs the page's own checks found

**Subpixel font antialiasing was breaking premultiplied alpha.** cairo's default
resolved to `ANTIALIAS_SUBPIXEL`, which rasterises glyphs against an assumed RGB
stripe: coverage is computed per colour channel, so the result is not a
premultiplied ARGB pixel at all. 3540 pixels of a 1080p frame had a colour
channel above their own alpha, by up to 75.

That is not cosmetic and it was not only a preview problem. The export pipes BGRA
to ffmpeg through `unpremultiply=inplace=1`, which divides by alpha and saturates
wherever a channel exceeds it -- so glyph edges in every video shipped so far were
too bright. Subpixel AA is also wrong on principle for an overlay: it is only
correct over a known background, and this sits on arbitrary video, where the
per-channel weighting reads as coloured fringing.

`ANTIALIAS_GRAY` gives one coverage value per pixel, which premultiplies and
composites correctly over anything. Cost: nothing measurable, and 20 fewer ink
pixels on the same glyph. `tools/selftest.py` now samples 121 frames and asserts
every pixel is premultiplied.

**A config with no enabled elements rendered a video with no overlay.** The
region path refused it; the full-frame path accepted it and wrote a plain
re-encode of the source. A partially-specified config dict defaults every element
to off, so `job.render({"config": {"input_data": ...}})` silently produced
overlay-free output that looks like a successful render until someone watches it.
Both compositors now raise.

**A session switch could interleave.** `selectSession` awaits several requests;
a second switch landing mid-flight left the model from one session with the map
and traces from another -- an AMP run drawn on the autocross map. Every await that
writes shared state now checks a generation token first.

**A seek across a camera split snapped the playhead back.** Loading a new file
leaves `video.currentTime` at 0 for a moment, and a clock that read it reported
the new clip's start rather than where the seek went. `S.seekTo` holds the
requested moment until the element is within a few frames of it.

### Testing a page from a terminal

There used to be a 739-line harness here that loaded `app.js` into a stub DOM
under node and asked it questions. It was deleted, and this is the reason it is
worth writing down: **it never found a bug, and it could not.** A stub DOM has no
layout, and layout is where a page's bugs are. Maintaining it also meant fixing
bugs in the fake DOM itself -- `className` not syncing `classList`, `innerHTML`
not clearing children, events being swallowed -- each of which produced a check
that passed while proving nothing.

The first screenshot taken with a real browser found three bugs in one shot: a
lap whose video would not decode hung the page on "loading..." forever, the
overlay sized itself to an undecoded 300x150 video element, and a tall track map
made a review card with an empty half beside it.

```bash
python tools/smoke.py                    # scratch library, every route, screenshots
python tools/smoke.py --library Laps     # against the real library
python tools/smoke.py --keep             # leave the server up to click around
```

`tools/smoke.py` does three cheap things: `node --check` on `app.js`, one request
to every route with a thin shape assertion, and a screenshot of each screen at
1366x768 and 900x700 written to a folder it names on the way out. It asserts
nothing about how the page looks -- looking is the human's job, and the pictures
are there to look at. Chrome or Edge is used if present and skipped if not.

## Why this kept breaking, and what changed

Three datasets in a row broke something that had been quietly wrong all along.
That is a pattern, not three accidents, and it has one cause: **code selected
data by an incidental property instead of by a stated requirement.**

| what arrived | what broke | the incidental property |
|---|---|---|
| autocross replacing circuit sessions | ten checks became silent skips | "laps exist" |
| a phone with more sensors | the loader returned a 3-column frame as 1 | "column names are unique" |
| six sessions from a third venue | two checks switched to a run with no map; one pooled geometry 117 km apart | `_all_runs()[0]`, "the map is open" |
| a second open course as a fixture | a course check reported 166375 m | "the map is open" again |

None of those assumptions was written down, so nothing could notice when one
stopped holding. Being more careful is not a fix for that. Three things are.

### 1. The assumptions live in one place, as data

`racechrono/shapes.py` classifies a session into a `Shape` -- venue, circuit or
course, whether its laps are corroborated, how many runs it holds, raw IMU,
footage, sample rate -- and declares, per capability, whether that shape supports
it and if not **why not**. The reason string is the point: "not applicable" with
no explanation is how a silent skip starts.

```
$ python tools/dataset_report.py
20 session(s) in Raw Data
session                           dur  laps  runs   IMU clips  venue  shape
session_20260809_121241_scca_a    740    1?     2     -     1 SCCA A  course / no-laps / multi-run / no-imu / footage
session_20260819_191442_amp_fu   1453     8     1     -     2 AMP Fu  circuit / laps / single-run / no-imu / split
session_20260822_164207_grip_8     42    0?     1   yes     1 Grip 8  course / no-laps / single-run / imu / footage

shapes present:
  circuit / laps / single-run / no-imu / footage    1 session(s)   <- no fixture covers this
  course / no-laps / multi-run / no-imu / footage   1 session(s)   <- no fixture covers this
  ...
byte-identical files (one of each pair is a duplicate export):
  session_20260809_125705_scca_autocross_1_v3.csv  ==  session_20260809_125705_scca_autocross_v3.csv
every session supports at least one capability.
```

Run it when a dataset arrives. A session nothing can use is reported as
**unhandled** rather than skipped, and a shape the fixtures have never seen is
reported as untested, because a suite that has not seen a shape does not cover it.

### 2. The declaration is checked against what the code does

Six checks in `tools/selftest.py` assert the matrix rather than whatever data
happens to be present:

- every session classifies, and its shape is self-consistent
- a capability declared applicable **actually runs**; one declared blocked
  **refuses** rather than producing something (44 runs and 16 refusals exercised
  on the working set)
- `laps_usable` is corroborated -- see below
- run detection gives the same answer at 0.5x, 2x and 5x scale
- the distance matcher never reverses, and no traversal starts past 0.3% of its
  line
- which shapes the fixtures do not cover, reported by name

The scale-invariance check is the one that directly answers "will this survive
the next dataset": it fails if anyone reintroduces a metres threshold fitted to
these courses.

### 3. Two implicit rules made explicit

**A lap count needs corroborating.** `laps.laps()` reads `lap_number`, which
RaceChrono increments on its own terms: on SCCA session 121241 -- an autocross
course with no start/finish trap -- it reports exactly one lap. `compare` branched
on `if laps:` and would have called that session a circuit. The rule is now
stated once: with a map, laps count when the map is closed; without one, when
there are at least two, since a single boundary is not a lap.

**A session is not a run.** Ten autocross sessions held one run each, four
circuit sessions held a whole evening, and six SCCA sessions held one or two with
staging between them. `racechrono/runs.py` finds them, and the rule needs no
tuned constant: a run is a movement segment whose length is comparable to the
longest in the session. Measured across all twenty sessions, the smallest real
second run was 95% of its session's longest and the largest piece of non-run
movement was 6% -- 81 m of staging against a 1349 m run.

## Matching a path to a centreline

`geo.match_polyline` walks a path along the line forward only, matching each
sample within a window ahead of the previous one -- the window being the distance
the car actually moved between the two samples, times four, floored at 25 m.

The obvious alternative is what was there before: project every sample
independently with `project_to_polyline`, then force the result monotonic with
`np.maximum.accumulate`. That works whenever the geometry is convenient, and the
SCCA course is not. Staging there sits beside the **finish**, 131 m from the
start, so the first sample of a run projected to 1097 m of a 1107 m line -- and
the running maximum then pinned every later sample there too:

| | before | after |
|---|---|---|
| traversals built from 5 sessions | 4 | 6 |
| common overlap | 5.6 m of 1107 | 1103 m of 1107 |
| worst off-line p90 | 57.6 m | 6.9 m |
| ideal lap | 1.6 s, 82.75 s under the fastest | 1m17.7s, 1.70 s under |

A car cannot advance 1100 m along the line between two samples 30 ms apart, so
it no longer can here. The circuit path is unchanged by it -- lap times still
match raw timestamps to 0.000000 s, sector sums to 0.000000 s, and no traversal
reverses in 32822 steps -- and `closed=True` lets `s` run past the end and
continue, which removed the separate unwrap-the-lap step entirely.

## Lap comparison

`racechrono/compare.py`, the `compare` tab in the session view, and a terminal
view:

```bash
python -m racechrono.compare --runs "Raw Data/*amp*.csv" --exclude-edge
python -m racechrono.compare --runs "Raw Data/*grip*.csv" --sectors 6
python -m racechrono.compare --runs "Raw Data/*amp*.csv" \
        --losses "session_20260819_165433_amp_full_v3#6"
```

```
AMP Full  circuit  2921 m of line, 8 sector(s)
14 of 27 traversals have a GPS gap; worst is 302 m (90% of the line measured)
                                        0-365 365-730 730-1095 ... 2555-2920
session_20260819_191442_amp_full_v3#6  1m43.0s  14.16* 15.39* 15.60  ...  8.22*
session_20260819_191442_amp_full_v3#5  1m43.8s  14.42  15.60  15.12  ...  8.29
session_20260819_181728_amp_full_v3#4  1m44.4s  14.59  15.40  15.03  ...  8.64
best sector                            1m41.6s  14.16  15.39  14.86  ...  8.22
ideal 1m41.6s, from 4 traversal(s) -- 1.34 s under the fastest real one
```

### Distance, not time

Two laps cannot be compared against elapsed time: four seconds into the lap the
two cars are in different places, so the difference between their speeds means
nothing. The coordinate has to be **distance along the centreline**, so every
comparison is between two moments at the same point on the track.

That distance comes from projecting each lap onto the map polyline --
`geo.project_to_polyline`, the same primitive that averages traversals when a map
is built -- and not from `distance_traveled`, which is the distance *that car*
covered. Across nineteen AMP laps, path length spans 2858.6 to 2942.3 m, 2.86%;
parameterising by it would slide two laps out of step by exactly the difference
in their lines, and the delta trace would be a picture of the parameterisation.

Two things fight monotonicity, and both silently corrupt every trace downstream
because `np.interp` needs an increasing x:

- On a closed circuit the polyline's ends are the same place, so a sample a metre
  before the line projects to 2929 m and the next to 0 -- a full lap of backwards
  motion mid-lap. Any step longer than half the lap is a wrap, not movement.
- Standing still puts a dozen samples within a metre of each other in whatever
  order the noise decides. A running maximum, then keep only strict advances.

An autocross course has no laps, so the run itself is the traversal, trimmed to
the driving. One type covers both, and a course session gets run-to-run
comparison without a special case.

### What the delta trace says, and what people read into it

`delta(s)` is time lost to the reference by the point `s`. The **slope** is the
part that localises a mistake; the offset is the part that gets misread as one. A
delta that runs flat through a corner means that corner was equal, however far
below zero the line happens to sit. Both are in the payload -- `delta` and
`rate`, seconds per 100 m -- and `losses()` reports the worst stretches rather
than the worst point, because "1.2 s down at 1400 m" is true and useless while
"0.83 s went between 187 and 286 m" is something to go and look at.

Verified against the source rather than asserted:

| check | result |
|---|---|
| lap time from raw timestamps vs the model | 0.000000 s |
| sector times sum to the span time | 0.000000 s |
| final delta = difference in span times | 0.001 s (the payload's own rounding) |
| delta at distance *d* = t(*d*) difference | 0.001 s |
| ideal lap = sum of column minima | exact, and never slower than a real lap |
| distance monotonic across 27 traversals | 0 backwards steps in 32822 |

Sectors are equal **distance**, not equal time: time sectors would move between
laps, so two laps could not be compared sector by sector. A fixed division of the
centreline is also stable across every session at that venue, which is what makes
a session-to-session table mean anything. The ideal lap is reported with the
number of traversals it was assembled from -- one built from six different laps is
a much weaker claim than one built from two.

### The tail of a lap is not comparable to the tail of another

Across nine AMP laps under 110 s, speed at the very end of the lap spread from
15.6 to 48.6 m/s. That is not misalignment: a lap that precedes an in-lap has the
driver braking before the line, and the lap still ends at the line. It does make
any spread statistic over the full lap misleading, so `--exclude-edge` drops the
first and last complete lap of each run, and the UI preselects representative
laps while still offering the rest.

### The GPS shadow

A comparison interpolates between samples, and at AMP that invents a real piece
of every lap. Fourteen of twenty-seven laps have a single gap of 250 to 300 m,
and **every one is in the same place** -- roughly 360 m to 690 m along the line,
where `accuracy` leaves its 10.0 m floor and reaches 49 m. It recurs across all
four sessions, 7 to 19 s each time. The ten autocross runs at the other venue
have no gap over 2 m, so this is the circuit, not the phone.

250 m of a 2929 m lap is 9% of a delta trace that looks exactly as authoritative
as the measured part. So every traversal reports its `gaps`, `gap_m` and
`measured_frac`; the delta and speed plots draw those stretches dashed at 28%
alpha; the map leaves them uncoloured, because the rate there is the slope of a
straight line drawn between two fixes; and `losses()` reports what fraction of
each stretch was interpolated. It matters: the fourth-worst 100 m stretch for one
lap was 365-465 m at +1.225 s, and it is 100% interpolated.

### The venue is the unit, not the session

An autocross session is one or two runs, so "compare my runs" always means
comparing across sessions. A circuit day is the same question in a different
shape -- was I quicker in the evening session. So the compare sidebar lists every
traversal at the **venue**, grouped by which map covers the session, which is the
same decision the renderer makes when it picks a map. Ten autocross sessions
become ten runs in one ranked list; four AMP sessions become 27 laps.

Loading is the cost, and splitting it was worth measuring:

| listing every traversal at AMP | |
|---|---|
| through the full session loader | 21.7 s |
| telemetry only, no footage paired or synced | 4.4 s |
| served from the per-session listing cache | 0.002 s |

Comparison needs no video at all -- the delta, the sectors and the distance
coordinate come from the CSV, and the seek target is a data timestamp -- so
`Library.telemetry()` loads the run, its config and its map and stops there.
Audio-correlating clips nobody is about to watch was almost all of that 21.7 s.
Selecting three laps out of 27 also used to project all 27 onto the centreline,
so `traversals(keys=...)` now filters before projecting rather than after.

The venue list arrives *after* the first comparison is on screen. Blocking the
tab on 4.4 s of loading to show something that this session's own laps could
already have shown is the wrong trade.

Out-laps and in-laps are listed but dimmed, and the initial selection skips them.
Of the 27 AMP laps, 19 are representative; the slowest is 3m35 against a 1m43
best, and putting that in a delta trace by default makes the view look broken.

### One row in the list had no name

Every row in the compare sidebar is `HH:MM  lap N   1m43.0s`, where `HH:MM` is
the session's time of day -- the only part of `session_20260819_191442_amp_full_v3`
that distinguishes one session from another. The row for the session currently
open in the player omitted it, on the reasoning that it is the obvious one. That
is backwards: in a list of ten it made exactly one row anonymous, and it was the
row hardest to identify because it was the one being looked at. Reported as "they
all have a number next to them except one".

Every row is now session-qualified, in the sidebar, the reference picker, the
plot legends and the sector table. The current session is marked by presence -- an
accent bar and a heavier name -- rather than by the absence of a label. And
`shortStem` no longer falls back to a 35-character stem in a 280 px column: a
name it cannot parse keeps its tail, since the distinguishing part of a filename
is almost never its first twelve characters.

### Measuring the playback rate

A reported 1.497x playback turned out to be a browser extension, but chasing it
was worth the trouble: this side had no way to answer the question. The transport
bar now reports what it observes over a rolling two-second window -- the session
clock against wall time, the video element's own `currentTime` against wall time,
and dropped frames from `getVideoPlaybackQuality`. Those three localise the fault
between them: if `video` reads 1.50 the browser really is playing fast, and if
`clock` disagrees with `video` the arithmetic here is wrong.

Chasing it also found a real bug next door. `S.rate` was initialised to 1 and
never read back from the rate control, so a browser restoring that select to 2x
across a reload would show 2x while playing at 1x. The control's value is now
adopted on load.

### Three bugs in the compare tab

The lap list came up empty, for every session, and stayed empty. Three separate
causes, all of the same shape -- state cleared by one code path and never refilled
by another:

- **Clicking `compare` before the first session had loaded** hit an early return
  in `loadComparable`, and nothing retried when the session arrived. Reproduced:
  `cmpList` had 0 children six seconds later.
- **Switching session** cleared the comparison without reloading it, so the list
  died on the first switch however long you waited.
- **Two async paths shared one generation counter.** `runCompare` bumped the
  counter `loadComparable` was guarding on, so the venue-wide load that followed
  the first comparison always aborted itself. This one was introduced while
  fixing the first two, and the new tests caught it immediately -- the sidebar
  showed 1 session where it should have shown 4.

Everything that can invalidate the tab now goes through one `refreshCompare()`,
and the counters are split: `cmpGen` identifies which session the sidebar is for,
`cmpRunGen` which comparison request is current.

The test harness had hidden the whole area. Its stub defaulted every checkbox to
false, so `all sessions here` -- which the page ships ticked -- read as unticked
and the venue code never ran under test. Input defaults are now read out of the
served markup. Its `innerHTML` setter also did not clear children, so list
lengths only ever grew and a child count could not be trusted as a check.

### One click from a number to the corner

Each traversal carries where it sits on its own session's timeline, so clicking a
distance on the delta trace opens exactly that moment in the player -- switching
session first if the lap belongs to another one. That is the whole reason for
keeping a single distance coordinate: "I lost 0.8 s between 187 and 286 m"
becomes the video of that corner without anyone converting anything by hand.

The compare map colours the centreline by where time changed hands, red for lost
and green for gained, width carrying magnitude. Its distance-to-position mapping
is checked against the session view's marker, which arrives at a position a
completely different way -- projecting the run's own lat/lon. They agree to 5.4 m,
which is the car's distance off the centreline rather than an error.

### Two latent test bugs a new venue exposed

Six autocross sessions from a third site arrived mid-session and broke the suite
in two places, both of which had been wrong all along:

- **`_all_runs()[0]`** -- whichever file sorts first -- was the run behind the
  gauge, marker and overlay-region checks. The new sessions sort first and have
  no map yet, so two real checks quietly became skips. They now take
  `a_mapped_run()`, the first run some map actually covers.
- **The alignment comparison pooled every run in the folder** and used the first
  one's mean position as the origin. With two venues in the folder that is
  meaningless: both figures came out at 89 km and the assertion became a coin
  toss, which is how it failed. It now groups runs by the map covering them and
  compares within one venue -- 0.81 m projected against 3.74 m by fraction of
  length, across ten traversals of one course.

Both are the same mistake as the off-map marker check made earlier: letting the
input folder's contents decide what gets tested. Worth stating plainly, because
the suite reported "all checks passed" for a while in a state where it was
testing less than it claimed.

## Test data

The suite reads `tests/data/` and `tests/maps/`, not `Raw Data/` and `Maps/`. It
used to read the working directories, and when those sessions were swapped from
circuit to autocross, ten checks -- lap slicing, lap export, start/finish
recovery, the camera-split join, closed-circuit map building -- reported `skip`
and the suite still said `all checks passed`. Coverage cannot depend on which
files happen to be in the input folder.

```bash
python tools/make_test_data.py       # cut the fixtures from Raw Data/
python tools/dataset_report.py        # what is in a folder, and what works on it
python tools/selftest.py             # 98 checks against the fixtures
python tools/selftest.py --source "D:/day" --maps Maps
python tools/smoke.py                # the server and the page, with screenshots
```

**The telemetry is real, cut from actual sessions.** Synthetic telemetry would
encode what this code believes about the format, and nearly everything worth
knowing here came from the data disagreeing: the undocumented device row, an
accuracy channel that floors at exactly 10.0, `bearing` interpolated up from
about 1 Hz, `calc` channels spiking to 5.93 G, a sensor frame that turned out to
be reflected rather than rotated, three `device_update_rate` columns collapsing
onto one name. A generated file only ever confirms the assumptions.

| fixture | what it is | why |
|---|---|---|
| `circuit_5laps.csv` | 621 s, 5 laps cut from an AMP session | laps, lap export, start/finish, closed-circuit map |
| `autocross_run1..3.csv` | three whole autocross runs, 43-46 s | raw IMU, open-course map, alignment comparison |
| `split_a.mp4` `split_b.mp4` | 72 s + 71 s, 1.5 s gap | a lap straddling a camera split |
| `runs.json` | names the pairing explicitly | removes doubt about which run the split test uses |
| `tests/maps/test_circuit.json` | closed, 2929 m | the marker-stability checks need a pinned map |
| `tests/maps/test_course.json` | open, 501 m | same, for the open-course path |

14.2 MB total. Three autocross runs rather than one because the open-course map
is a median across traversals and the alignment comparison needs at least three
-- with one pass both are vacuous.

**The video is generated, not shipped.** The real clips are 2 GB each, and the
split tests do not care what the picture shows -- they exercise `creation_time`,
duration, frame rate, and where a lap falls relative to a split. Generating them
also means the adversarial case can be built deliberately: the split lands 60.2 s
into a 120.4 s lap. First attempt used `testsrc2` and produced 86 MB per clip; a
flat colour is 0.95 MB and tests exactly as much.

Two things the fixtures fixed beyond un-skipping:

- `_compositor()` took the first map in the directory rather than the map
  covering the run, which put an autocross run on the circuit map 180 km away.
  The off-map check then reported a marker 58670 px outside its box -- a true
  statement about clipping, but no longer the pit-lane case it claims to test,
  and the on-track comparison point was meaningless. It now selects by position,
  and the hard-coded AMP lat/lon became "the fastest sample in this run".
- The off-map search ran on the first CSV alphabetically, so the strength of the
  check depended on filename order. It now searches every run for the worst
  overshoot: 101.8 px, in an autocross staging lane.

## Layout of the repository

```
main.py                  entry point; the user-editable CONFIG
config.py                dataclasses, defaults, validation
racechrono/
  csv_io.py              CSV v3 loading, column disambiguation, resampling
  geo.py                 lat/lon -> local metres, circular stats, PCA
  laps.py                lap slicing, start/finish recovery, crossings
  runs.py                the timed runs inside a session
  shapes.py              what a session is, and which capabilities that allows
  identify.py            which track a run is on: shape, coverage, direction
  library.py             SQLite index, stable ids, editable names, deletion
  ingest.py              plan and apply: files in, lap library out
  compare.py             shared distance coordinate, deltas, sectors, losses
  window.py              driving window and per-lap windows -> render targets
  overlay.py             dirty-region planning and the packed atlas
  job.py                 headless JSON API: probe, plan, render
  trackmap.py            map load/save, GPS -> map-space transform, SVG
  video.py               ffprobe, clip discovery, pairing, segment planning
  sync.py                data <-> video alignment
  render.py              frame loop, ffmpeg pipe, compositing, joining, sheets
  gauges/
    base.py              Gauge ABC, layout maths, static-layer and text caches
    map_gauge.py  dial.py  gmeter.py
webui/
  server.py              localhost session view: routes, ranges, lazy loading
  session.py             the session model: channels, laps, map, clips, traces
  static/                index.html, app.css, app.js -- no build step
tools/
  generate_map.py        multi-run map averaging: laps, or open courses
  preview_server.py      the original overlay-seam proof, kept as a benchmark
  dataset_report.py      shapes in a folder, and what each session supports
  ingest.py              the ingest CLI: point it at a folder
  library.py             browse, rename, tag, merge, split, verify, delete
  make_test_data.py      cuts tests/data and tests/maps from real sessions
  selftest.py            checks that need no render, grouped into areas
  smoke.py               server up, every route once, screenshots to look at
tests/
  data/                  pinned fixtures: real telemetry, generated clips
  maps/                  pinned maps, one closed and one open
explore.ipynb            channel explorer: plots, lap overlays, coloured line
```

### How an output is assembled

1. `window.py` turns a run into **targets** -- one per session (trimmed to the
   driving), or one per lap.
2. `video.plan_segments` maps each target's window onto the clips that cover it.
   A window inside one clip gives one segment; a window spanning a camera split
   gives two.
3. `render.render_segment` runs the frame loop for one segment: overlay frames
   down a pipe, composited and encoded by one ffmpeg.
4. `render.render_target` joins a multi-segment target with a lossless stream
   copy and deletes the parts (`--keep-parts` retains them).

Targets are rendered in parallel; a target's own segments run sequentially,
since they have to be joined afterwards anyway.

## Non-goals

No GUI. No VBO or `.rcz` parsing. No lap timing display in v1 (the reference
image's lap-timer boxes are a reserved slot). No multi-camera PIP. No
stabilisation. No cloud anything.
