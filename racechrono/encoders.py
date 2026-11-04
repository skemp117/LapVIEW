"""Which video encoder this machine can actually run, decided once.

The CONFIG names an encoder; the machine decides whether it opens. Three things
get in the way, and each one fails at encoder initialisation rather than
mid-stream, which is what makes a one-second test encode an honest check:

* **No NVIDIA GPU, or none visible.** A container started without `--gpus`, a
  laptop on its iGPU. NVENC cannot be opened at all, so the encode falls back to
  the CPU: libx265 for HEVC, libx264 for H.264 (`encode.cpu_fallback`).
* **An older NVENC.** Pascal (GTX 10-series, Quadro P, Tesla P) encodes HEVC but
  has no HEVC B-frames, and no generation before Turing can use B-frames as
  references. Asking for either makes the driver refuse the session. The
  `pascal` profile leaves both out. `auto` reads the card's name from
  nvidia-smi; `LAPVIEW_NVENC_PROFILE=pascal` says so outright.
* **Temporal AQ.** Not offered for HEVC before Turing, and treated here as
  something to try rather than assume: when the configured arguments fail, the
  same encoder is tried again without it before anything else is given up.

`resolve(cfg)` runs that ladder and writes the winner into `cfg.encode`, so
ingest and export afterwards use whatever was proven to work. `cpu_equivalent`
is the same fallback for a render that fails later, e.g. when every NVENC
session on a consumer card is already taken.
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import video

# NVENC generations before Turing: Maxwell (GTX 9xx, Quadro M, Tesla M), Pascal
# (GTX 10xx, TITAN X/Xp, Quadro P, Tesla P) and Volta (TITAN V, V100), which
# shares Pascal's encoder. The GTX 16-series is Turing and is left to the
# default; if one of those refuses HEVC B-frames anyway, `resolve` finds out.
_PRE_TURING = re.compile(
    r"GTX\s*(9\d\d|10\d\d)\b|TITAN\s*(X|Xp|V)\b|Quadro\s*([MP]\d|GV100)"
    r"|Tesla\s*[MPV]\d|\b(P4|P6|P40|P100|M4|M6|M10|M40|M60|V100)\b", re.I)

# What a failed NVENC session tends to say. Matched case-insensitively against
# ffmpeg's stderr to decide whether a failed render is worth retrying on the CPU.
NVENC_FAILURES = ("nvenc", "cuda", "openencodesessionex", "no capable devices",
                  "out of memory", "sessions", "device creation failed",
                  "hwaccel", "libnvidia-encode", "b-frames", "temporal aq")

_resolved = {}


def nvidia_smi():
    """The nvidia-smi binary, or None. PATH first, then where Windows puts it."""
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        p = (Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
             / "nvidia-smi.exe")
        if p.exists():
            return str(p)
    return None


def gpus(timeout=15):
    """[{name, driver, memory}] for each NVIDIA GPU nvidia-smi can see."""
    exe = nvidia_smi()
    if not exe:
        return []
    try:
        q = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total",
                            "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return []
    if q.returncode != 0:
        return []
    out = []
    for line in q.stdout.splitlines():
        bits = [b.strip() for b in line.split(",")]
        if len(bits) >= 3 and bits[0]:
            out.append({"name": bits[0], "driver": bits[1], "memory": bits[2]})
    return out


def profile_for(gpu_name):
    """`pascal` for a pre-Turing NVENC, `turing` for anything newer."""
    return "pascal" if gpu_name and _PRE_TURING.search(gpu_name) else "turing"


def cpu_encoder(enc):
    """The CPU encoder a failed NVENC encode should fall back to, or None."""
    if enc.cpu_fallback == "none":
        return None
    if enc.cpu_fallback in ("libx264", "libx265"):
        return enc.cpu_fallback
    return "libx265" if "hevc" in enc.encoder or "265" in enc.encoder \
        else "libx264"


def cpu_equivalent(enc):
    """A copy of `enc` that encodes on the CPU, or None when that is off."""
    name = cpu_encoder(enc)
    if name is None:
        return None
    fb = copy.deepcopy(enc)
    fb.encoder = name
    fb.preset = "medium"
    fb.hwaccel_decode = False
    return fb


def looks_like_nvenc_failure(text):
    low = (text or "").lower()
    return any(k in low for k in NVENC_FAILURES)


def test_cmd(ffmpeg, enc, out="-"):
    """A one-second encode with exactly the arguments a real one would use."""
    from .render import encoder_args
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
           "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
           "-c:v", enc.encoder] + encoder_args(enc) + ["-pix_fmt", enc.pix_fmt]
    if out == "-":
        return cmd + ["-f", "null", "-"]
    return cmd + [str(out)]


def _run(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return r.returncode == 0, (r.stderr or "").strip()[-600:]


def _candidates(enc):
    """What to try, best first. Each is (label, EncodeCfg)."""
    out = [("as configured", copy.deepcopy(enc))]
    if enc.is_nvenc:
        if enc.temporal_aq:
            c = copy.deepcopy(enc)
            c.temporal_aq = False
            out.append(("without temporal AQ", c))
        if enc.nvenc_profile != "pascal":
            c = copy.deepcopy(enc)
            c.nvenc_profile = "pascal"
            c.temporal_aq = False
            out.append(("with the pascal profile", c))
        fb = cpu_equivalent(enc)
        if fb is not None:
            out.append(("on the CPU", fb))
            if fb.encoder != "libx264":
                last = copy.deepcopy(fb)
                last.encoder = "libx264"
                out.append(("on the CPU, as H.264", last))
    return out


def resolve(cfg, log=None, runner=None, gpu_list=None, ffmpeg=None):
    """Settle `cfg.encode` on something this machine can run, and say what.

    Mutates `cfg.encode` and returns a report: the encoder chosen, the NVENC
    profile, the GPU, whether a fallback was taken and why. `runner(cmd)` and
    `gpu_list` exist for the tests; normally they are a real ffmpeg and a real
    nvidia-smi. The answer is cached per process -- the test encode is a second
    or so, and nothing about the machine changes while the server is up.
    """
    enc = cfg.encode
    say = log or (lambda _msg: None)
    report = {"requested": enc.encoder, "encoder": enc.encoder,
              "profile": enc.nvenc_profile, "gpu": None, "fallback": False,
              "tested": False, "notes": []}
    try:
        exe = ffmpeg or video.find_binary("ffmpeg", cfg.ffmpeg.ffmpeg)
    except FileNotFoundError as exc:
        report["notes"].append(str(exc))
        say("encoder  %s (untested: %s)" % (enc.encoder, exc))
        return report

    found = (gpus() if gpu_list is None else gpu_list) if enc.is_nvenc else []
    if found:
        report["gpu"] = found[0]["name"]
    if enc.is_nvenc and enc.nvenc_profile == "auto":
        if found:
            enc.nvenc_profile = profile_for(found[0]["name"])
            report["notes"].append("nvenc profile %s, from %s"
                                   % (enc.nvenc_profile, found[0]["name"]))
        else:
            # Unknown card: start from the full settings; the ladder below
            # drops to the pascal profile if the driver refuses them.
            enc.nvenc_profile = "turing"
            report["notes"].append("no GPU name from nvidia-smi; trying the "
                                   "turing profile first")

    key = (exe, enc.encoder, enc.nvenc_profile, enc.temporal_aq, enc.bframes,
           enc.b_ref_mode, enc.preset, enc.cpu_fallback, enc.hwaccel_decode)
    if runner is None and key in _resolved:
        chosen, notes = _resolved[key]
    else:
        chosen, notes = None, []
        run = runner or _run
        for label, cand in _candidates(enc):
            ok, err = run(test_cmd(exe, cand))
            if ok:
                chosen = (label, cand)
                break
            last = (err or "no message").strip().splitlines() or [""]
            notes.append("%s %s failed: %s" % (cand.encoder, label, last[-1]))
        if chosen and chosen[1].is_nvenc and chosen[1].hwaccel_decode:
            ok, err = (True, "") if runner else _hwaccel_works(exe, chosen[1])
            if not ok:
                chosen[1].hwaccel_decode = False
                notes.append("CUDA decode unavailable, decoding on the CPU: %s"
                             % err)
        if runner is None:
            _resolved[key] = (chosen, notes)

    report["tested"] = True
    report["notes"].extend(n if isinstance(n, str) else str(n) for n in notes)
    if chosen is None:
        report["notes"].append("no encoder could be opened; keeping %s"
                               % enc.encoder)
        say("encoder  %s -- NOTHING WORKED: %s"
            % (enc.encoder, "; ".join(report["notes"])))
        return report

    label, cand = chosen
    for f in ("encoder", "preset", "nvenc_profile", "temporal_aq",
              "hwaccel_decode"):
        setattr(enc, f, getattr(cand, f))
    report["encoder"] = enc.encoder
    report["profile"] = enc.nvenc_profile if enc.is_nvenc else None
    report["fallback"] = label != "as configured"
    report["chosen"] = label
    report["temporal_aq"] = bool(enc.temporal_aq) if enc.is_nvenc else None
    report["hwaccel_decode"] = bool(enc.hwaccel_decode) if enc.is_nvenc \
        else False
    say("encoder  %s" % describe(enc, report))
    return report


def describe(enc, report=None):
    """One line for a log: what encodes, with which concessions."""
    if enc.is_nvenc:
        hevc = "hevc" in enc.encoder
        bits = ["%s profile" % enc.nvenc_profile]
        if enc.nvenc_profile == "pascal":
            bits.append("no HEVC B-frames" if hevc else "no B-frame references")
        bits.append("temporal AQ %s" % ("on" if enc.temporal_aq else "off"))
        bits.append("CUDA decode" if enc.hwaccel_decode else "CPU decode")
        text = "%s (%s)" % (enc.encoder, ", ".join(bits))
    else:
        text = "%s (CPU, preset %s)" % (enc.encoder, enc.preset)
    if report:
        if report.get("gpu"):
            text += " on %s" % report["gpu"]
        if report.get("fallback"):
            text += " -- fallback from %s: %s" % (
                report["requested"],
                next((n for n in report["notes"] if "failed" in n), "")[:300])
    return text


def _hwaccel_works(ffmpeg, enc):
    """Decode a tiny clip with -hwaccel cuda. NVENC working does not prove it."""
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "probe.mp4"
        ok, err = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i",
                        "testsrc2=size=640x360:rate=30:duration=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
        if not ok:
            return True, ""           # nothing to test with; leave it as asked
        return _run([ffmpeg, "-hide_banner", "-loglevel", "error",
                     "-hwaccel", "cuda", "-i", str(clip), "-f", "null", "-"])
