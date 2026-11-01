"""A minimal preview server: the second consumer of the overlay contract.

Not the UI. This exists to prove the seam end to end -- that the same
`RegionCompositor` an export uses can serve overlay frames to a browser over a
socket, at interactive rates, before any interface is written.

    python tools/preview_server.py --run session_20260819_181728 --port 8765

    GET /                        a scratch page: video + overlay, scrubbable
    GET /api/plan                the job plan as JSON
    GET /api/layout             frame size, regions, atlas geometry
    GET /api/overlay?t=123.5    the regions for that moment, as raw RGBA
    GET /api/frames?t=..&n=..   timing only, for a throughput measurement
    GET /video                  the source clip, with range support
"""
from __future__ import annotations

import argparse
import io
import json
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as config_mod                                    # noqa: E402
from racechrono import csv_io, job as job_mod, render, sync, trackmap, video, window  # noqa: E402

STATE = {}


def build_state(args):
    import main as main_mod

    cfg = config_mod.load(main_mod.CONFIG)
    runs = [csv_io.read_run(p)
            for p in sorted(Path(args.source).rglob("*.csv"))
            if not args.run or args.run in p.stem]
    if not runs:
        raise SystemExit("no run matching %r" % args.run)
    run = runs[0]

    clips = video.discover(args.source, cfg.ffmpeg.ffprobe, recursive=True)
    pairings, _ = video.pair([run], clips, video.load_manifest(cfg.manifest))
    pairing = pairings[0]
    if not pairing.clips:
        raise SystemExit("no footage paired with %s" % run.stem)

    cache = sync.load_cache(Path(cfg.cache_dir) / "sync.json")
    for clip in pairing.clips:
        sync.sync_clip(clip, run, cfg.sync.as_dict(), cache, cfg.ffmpeg.ffmpeg,
                       verbose=False)
    if cfg.sync.consensus and len(pairing.clips) > 1:
        sync.reconcile_run(pairing.clips, cfg.sync.min_prominence, verbose=False)

    cfg = config_mod.bind(cfg, run)
    tm, _ = job_mod._select_map(cfg, run)
    target = window.session_target(run, cfg)
    clip = pairing.clips[0]
    fps = float(cfg.encode.fps or clip.fps)
    width, height = clip.display_size

    # for_preview forces the region path: the payload is what matters here.
    comp = render.make_compositor(cfg, width, height, fps, tm, for_preview=True)
    lookup, grid_t0, n = render.row_source(run, cfg, fps, run.t0, run.t1,
                                           comp.channels)

    STATE.update(cfg=cfg, run=run, pairing=pairing, clip=clip, comp=comp,
                 lookup=lookup, grid_t0=grid_t0, n=n, fps=fps,
                 target=target, width=width, height=height)
    print("run      %s" % run.stem)
    print("clip     %s (%dx%d @ %.3f fps)" % (clip.path.name, width, height, fps))
    print("window   t+%.1f .. t+%.1f s of data" % (target.t0 - run.t0,
                                                   target.t1 - run.t0))
    print("overlay  %d region(s), atlas %dx%d, %.2f MB/frame (%.1f%% coverage)"
          % (len(comp.regions), comp.atlas_w, comp.atlas_h,
             comp.bytes_per_frame / 1e6, 100 * comp.coverage))
    return STATE


def overlay_at(t_data):
    """Draw the overlay for a data timestamp and return the packed payload.

    Wire format, little-endian: a header of (n_regions), then per region
    (x, y, w, h) as int32, then the raw BGRA rows back to back. One request,
    one frame, no negotiation -- the point is to show the same bytes an export
    would composite.
    """
    st = STATE
    i = int(round((t_data - st["grid_t0"]) * st["fps"]))
    st["comp"].draw(st["lookup"](i))
    views = st["comp"].region_views()
    head = struct.pack("<i", len(views))
    for r, _, _ in views:
        head += struct.pack("<4i", r.x, r.y, r.w, r.h)
    return head + b"".join(buf for _, buf, _ in views)


PAGE = """<!doctype html><meta charset=utf-8>
<title>overlay preview</title>
<style>
 body{margin:0;background:#14171c;color:#c9d1d9;font:13px/1.5 system-ui}
 #wrap{position:relative;width:min(96vw,1280px);margin:12px auto}
 video,canvas{position:absolute;inset:0;width:100%;height:auto}
 video{position:static;display:block}
 #bar{width:min(96vw,1280px);margin:8px auto;display:flex;gap:12px;align-items:center}
 input[type=range]{flex:1}
 code{color:#9ecbff}
</style>
<div id=wrap><video id=v src="/video" preload=auto></video>
<canvas id=c></canvas></div>
<div id=bar><button id=play>play</button>
<input type=range id=seek min=0 max=1000 value=0>
<span id=info></span></div>
<script>
const v=document.getElementById('v'), c=document.getElementById('c');
const ctx=c.getContext('2d'); let L=null, busy=false, stats=[];
fetch('/api/layout').then(r=>r.json()).then(l=>{
  L=l; c.width=l.frame[0]; c.height=l.frame[1];
  document.getElementById('info').textContent =
    l.regions.length+' region(s), '+(l.bytes_per_frame/1e6).toFixed(2)+' MB/frame';
});
async function paint(){
  if(!L||busy) return; busy=true;
  const t0=performance.now();
  const t = L.data_t0 + v.currentTime;
  const buf = await (await fetch('/api/overlay?t='+t.toFixed(4))).arrayBuffer();
  const dv = new DataView(buf); const n = dv.getInt32(0,true);
  let off = 4 + n*16, meta=[];
  for(let i=0;i<n;i++){ const b=4+i*16;
    meta.push([dv.getInt32(b,true),dv.getInt32(b+4,true),
               dv.getInt32(b+8,true),dv.getInt32(b+12,true)]); }
  ctx.clearRect(0,0,c.width,c.height);
  for(const [x,y,w,h] of meta){
    const px = new Uint8ClampedArray(w*h*4);
    const src = new Uint8Array(buf, off, w*h*4); off += w*h*4;
    // BGRA premultiplied -> RGBA straight, the same conversion ffmpeg does.
    for(let p=0;p<w*h;p++){
      const a=src[p*4+3]; const s=a?255/a:0;
      px[p*4  ]=Math.min(255,src[p*4+2]*s);
      px[p*4+1]=Math.min(255,src[p*4+1]*s);
      px[p*4+2]=Math.min(255,src[p*4  ]*s);
      px[p*4+3]=a;
    }
    ctx.putImageData(new ImageData(px,w,h),x,y);
  }
  stats.push(performance.now()-t0); if(stats.length>30) stats.shift();
  const avg=stats.reduce((a,b)=>a+b,0)/stats.length;
  document.getElementById('info').textContent =
    n+' region(s)  '+avg.toFixed(1)+' ms/frame  '+(1000/avg).toFixed(0)+' fps ceiling';
  busy=false;
}
document.getElementById('play').onclick=()=>{ v.paused?v.play():v.pause(); };
document.getElementById('seek').oninput=e=>{
  if(v.duration) v.currentTime = v.duration*e.target.value/1000; };
v.addEventListener('timeupdate',()=>{
  document.getElementById('seek').value = 1000*v.currentTime/(v.duration||1);});
function loop(){ paint(); requestAnimationFrame(loop); } loop();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        st = STATE
        if u.path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")

        if u.path == "/api/layout":
            d = st["comp"].describe()
            d["data_t0"] = st["clip"].data_t0
            d["fps"] = st["fps"]
            d["clip"] = st["clip"].path.name
            return self._send(200, json.dumps(d).encode(), "application/json")

        if u.path == "/api/plan":
            p = job_mod.plan({"select": {"runs": [st["run"].stem]}})
            return self._send(200, json.dumps(p).encode(), "application/json")

        if u.path == "/api/overlay":
            t = float(q.get("t", ["0"])[0])
            return self._send(200, overlay_at(t), "application/octet-stream",
                              {"Cache-Control": "no-store"})

        if u.path == "/api/frames":
            t = float(q.get("t", [str(st["clip"].data_t0)])[0])
            n = int(q.get("n", ["120"])[0])
            start = time.perf_counter()
            total = 0
            for k in range(n):
                total += len(overlay_at(t + k / st["fps"]))
            el = time.perf_counter() - start
            return self._send(200, json.dumps({
                "frames": n, "seconds": round(el, 4),
                "fps": round(n / el, 1),
                "MB_per_s": round(total / el / 1e6, 1),
                "MB_per_frame": round(total / n / 1e6, 3)}).encode(),
                "application/json")

        if u.path == "/video":
            return self._serve_file(Path(st["clip"].path))
        self._send(404, b"not found", "text/plain")

    def _serve_file(self, path):
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
        end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="a folder of sessions and footage")
    ap.add_argument("--run", default=None, help="CSV stem substring")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--bench", type=int, default=0,
                    help="render N overlay frames and exit, no server")
    args = ap.parse_args(argv)

    build_state(args)
    if args.bench:
        t0 = STATE["clip"].data_t0 + 60.0
        start = time.perf_counter()
        total = 0
        for k in range(args.bench):
            total += len(overlay_at(t0 + k / STATE["fps"]))
        el = time.perf_counter() - start
        print("\n%d overlay frames in %.2f s -> %.0f fps, %.1f MB/s (%.3f MB/frame)"
              % (args.bench, el, args.bench / el, total / el / 1e6,
                 total / args.bench / 1e6))
        return 0

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("\nopen http://127.0.0.1:%d/  (ctrl-c to stop)" % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
