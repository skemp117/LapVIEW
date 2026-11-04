"""Does the web app come up, answer, and look right?

    python tools/smoke.py                 # scratch library built from tests/data
    python tools/smoke.py --library Laps  # your real library, read-only
    python tools/smoke.py --keep          # leave the server up afterwards

This replaces a 739-line suite that drove the page's JavaScript against a stub
DOM. That harness never found a bug -- it could not: a stub DOM has no layout, and
layout is where the page's bugs live. The first screenshot taken with a real
browser found three (a lap whose video would not decode hung the page on
"loading...", the overlay sized itself to an undecoded 300x150 video element, and
a tall track map made a card with an empty half beside it).

So this does three cheap things instead:

  1. `node --check` on the page's script -- catches the syntax half of an edit.
  2. Every route, once, against a real server: status, content type, and a shape
     assertion thin enough not to encode a product decision.
  3. A screenshot of each screen at two window sizes, for a human to look at.

It asserts nothing about how the page looks. Looking is the human's job, and the
files are named on the way out.
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome", "chromium", "chromium-browser",
]

fails = 0
checks = 0


def ok(name, cond, detail=""):
    global fails, checks
    checks += 1
    if cond:
        print("  ok   %s%s" % (name, " -- " + detail if detail else ""))
    else:
        fails += 1
        print(" FAIL  %s%s" % (name, " -- " + detail if detail else ""))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(base, route, want_json=True):
    with urllib.request.urlopen(base + route, timeout=120) as r:
        body = r.read()
        return r.status, r.headers.get("Content-Type", ""), (
            json.loads(body) if want_json else body)


def call(base, method, route, body=None, header=True):
    """(status, headers, JSON or None) for any method, errors included."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + route, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if header and method == "POST":
        req.add_header("X-LapVIEW", "1")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            st, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        st, hdrs, raw = exc.code, exc.headers, exc.read()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return st, hdrs, parsed


def node_check(path):
    node = shutil.which("node")
    if node is None:
        print("  skip node --check (no node on PATH)")
        return
    out = subprocess.run([node, "--check", str(path)], capture_output=True,
                         text=True)
    ok("%s parses" % path.name, out.returncode == 0,
       (out.stderr or "").strip().splitlines()[-1] if out.returncode else "")


def find_browser():
    for cand in BROWSERS:
        if Path(cand).is_file():
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


def shoot(browser, base, out_dir, name, hash_, size):
    """One screenshot. Chrome exits on its own once the page has settled."""
    dst = out_dir / ("%s-%dx%d.png" % (name, size[0], size[1]))
    subprocess.run([browser, "--headless=new", "--disable-gpu",
                    "--hide-scrollbars", "--no-first-run",
                    "--window-size=%d,%d" % size,
                    "--screenshot=%s" % dst,
                    "--virtual-time-budget=8000",
                    base + "/" + hash_],
                   capture_output=True, text=True, timeout=180)
    return dst if dst.is_file() else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", help="an existing library to serve; the default "
                                      "builds a throwaway one from tests/data")
    ap.add_argument("--source", default="tests/data",
                    help="folder to build the throwaway library from")
    ap.add_argument("--shots", default="", help="where to write screenshots "
                                                "(default: a temp folder)")
    ap.add_argument("--no-shots", action="store_true")
    ap.add_argument("--keep", action="store_true",
                    help="leave the server running when done")
    args = ap.parse_args()

    node_check(ROOT / "webui/static/app.js")

    tmp = None
    library = args.library
    if library is None:
        tmp = tempfile.mkdtemp(prefix="smoke-")
        library = str(Path(tmp) / "lib")
        print("building a throwaway library from %s" % args.source)
        built = subprocess.run(
            [sys.executable, str(ROOT / "tools/ingest.py"), args.source,
             "--library", library, "--maps", "tests/maps", "--no-encode"],
            cwd=ROOT, capture_output=True, text=True)
        if built.returncode != 0:
            print(built.stdout[-2000:])
            print(built.stderr[-2000:])
            raise SystemExit("could not build a library to serve")

    port = free_port()
    base = "http://127.0.0.1:%d" % port
    server = subprocess.Popen(
        [sys.executable, "-m", "webui.server", "--port", str(port),
         "--library", library, "--maps", "tests/maps", "--no-open", "--quiet"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/api/library", timeout=5).read()
                break
            except (urllib.error.URLError, socket.timeout, ConnectionError):
                time.sleep(0.25)
        else:
            raise SystemExit("the server never answered on %s" % base)

        print("serving %s on %d" % (library, port))
        st, ctype, tree = get(base, "/api/library")
        ok("the library answers", st == 200 and "tracks" in tree,
           "%d track(s), %d lap(s)"
           % (len(tree["tracks"]), tree["summary"]["laps"]))

        st, ctype, page = get(base, "/", want_json=False)
        ok("the page is served", st == 200 and b"app.js" in page,
           "%d B of HTML" % len(page))
        st, ctype, css = get(base, "/static/app.css", want_json=False)
        ok("the stylesheet is served", st == 200 and "css" in ctype, ctype)

        st, _, laps = get(base, "/api/laps")
        ok("laps list", st == 200 and laps["laps"], "%d lap(s)" % len(laps["laps"]))
        if not laps["laps"]:
            raise SystemExit("nothing in the library to exercise")
        lap = laps["laps"][0]
        with_video = next((l for l in laps["laps"] if l["has_video"]), None)

        st, _, one = get(base, "/api/lap/" + lap["id"])
        ok("one lap describes itself",
           st == 200 and one["channels"] and "map" in one,
           "%d channel(s), %s"
           % (len(one["channels"]), "map" if one["map"] else "no map"))

        ch = one["channels"][0]["name"]
        st, _, tr = get(base, "/api/trace/%s?ch=%s&n=200" % (lap["id"], ch))
        ok("a trace decimates", st == 200 and tr["t"] and len(tr["t"]) <= 400,
           "%s in %d point(s)" % (ch, len(tr["t"])))

        st, _, lay = get(base, "/api/layout/" + lap["id"])
        ok("the overlay has a layout", st == 200 and lay["regions"],
           "%d region(s), frame %s" % (len(lay["regions"]),
                                       "x".join(map(str, lay["frame"]))))

        st, ctype, buf = get(base, "/api/overlay/%s?t=1" % lap["id"],
                             want_json=False)
        want = 4 + len(lay["regions"]) * 16 + sum(
            r["w"] * r["h"] * 4 for r in lay["regions"])
        ok("the overlay renders", st == 200 and len(buf) == want,
           "%d B, header describes %d B" % (len(buf), want))

        st, _, cmp_list = get(base, "/api/comparable/" + lap["id"])
        ok("comparable laps", st == 200 and "laps" in cmp_list,
           "%d lap(s) at this track" % len(cmp_list["laps"]))
        if len(cmp_list["laps"]) >= 2:
            ids = ",".join(l["id"] for l in cmp_list["laps"][:2])
            st, _, cmp_got = get(base, "/api/compare?laps=" + ids)
            ok("two laps compare",
               st == 200 and len(cmp_got["traversals"]) == 2,
               "%.0f m of centreline" % cmp_got["length_m"])
        else:
            print("  skip compare (one lap at this track)")

        if with_video:
            req = urllib.request.Request(base + "/video/" + with_video["id"])
            req.add_header("Range", "bytes=0-2047")
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            ok("video serves a byte range",
               r.status == 206 and len(body) == 2048,
               "%s, %d B" % (r.status, len(body)))
        else:
            print("  skip video (no lap here has footage)")

        st, _, sur = get(base, "/api/survey?path=" + args.source)
        ok("survey counts a folder", st == 200 and sur["csvs"] > 0,
           "%d csv, %d video" % (sur["csvs"], sur["videos"]))
        st, _, job = get(base, "/api/import/progress")
        ok("the import job reports", st == 200 and "state" in job, job["state"])

        # The routes that change something are POSTs with the header. The
        # arguments are ones that would change nothing even if a route were
        # still a GET -- `--library` may be someone's real library -- and the
        # folder chooser is left out, because a GET that still opened it would
        # leave a dialog on this desktop.
        harmless = {
            "/api/rename": {"kind": "lap", "id": "nope", "name": "x"},
            "/api/notes": {"kind": "lap", "id": "nope", "text": "x"},
            "/api/event": {"day": "nope", "name": ""},
            "/api/delete": {"what": "lap", "ids": ""},
            "/api/import/run": {"source": ""},
            "/api/export/run": {"laps": ""},
            "/api/reveal": {"path": ""},
            "/api/library/reset": {"confirm": "no"},
            "/api/rescan": {},
        }
        got = {}
        for route, params in harmless.items():
            q = "?" + urllib.parse.urlencode(params) if params else ""
            st, hdrs, _ = call(base, "GET", route + q)
            st2, _, _ = call(base, "POST", route, params, header=False)
            got[route] = (st, hdrs.get("Allow"), st2)
        bad = {r: g for r, g in got.items() if g != (405, "POST", 403)}
        ok("changes refuse a GET (405) and a POST without the header (403)",
           not bad, "%d route(s)" % len(got) if not bad else str(bad))
        st, _, body = call(base, "POST", "/api/rename",
                           harmless["/api/rename"])
        ok("a POST with the header reaches the route", st == 404,
           "%s %s" % (st, (body or {}).get("error", "")))
        if tmp:
            # The throwaway library only: a rename and back, as the page does.
            name = lap["name"]
            st, _, body = call(base, "POST", "/api/rename",
                               {"kind": "lap", "id": lap["id"],
                                "name": "smoke test"})
            _, _, now = get(base, "/api/laps")
            renamed = next(l for l in now["laps"] if l["id"] == lap["id"])
            ok("a lap renames by POST",
               st == 200 and renamed["name"] == "smoke test", str(body))
            if name:
                call(base, "POST", "/api/rename",
                     {"kind": "lap", "id": lap["id"], "name": name})

        for route in ("/api/lap/nope", "/api/nonsense", "/static/../secrets"):
            try:
                with urllib.request.urlopen(base + route, timeout=30) as r:
                    st = r.status
            except urllib.error.HTTPError as exc:
                st = exc.code
            ok("%s is refused" % route, st >= 400, str(st))

        if not args.no_shots:
            browser = find_browser()
            if browser is None:
                print("  skip screenshots (no Chrome or Edge found)")
            else:
                out = Path(args.shots) if args.shots else Path(
                    tempfile.mkdtemp(prefix="smoke-shots-"))
                out.mkdir(parents=True, exist_ok=True)
                shots = []
                for name, hash_ in (("session", ""), ("compare", "#compare"),
                                    ("import", "#import")):
                    for size in ((1366, 768), (900, 700)):
                        got = shoot(browser, base, out, name, hash_, size)
                        if got:
                            shots.append(got)
                ok("screenshots written", len(shots) == 6,
                   "%d of 6 in %s" % (len(shots), out))
                for s in shots:
                    print("       %s" % s)

        if args.keep:
            print("\nserver left running on %s -- ctrl-c to stop" % base)
            server.wait()
    finally:
        if not args.keep:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("%d of %d smoke checks failed" % (fails, checks))
        raise SystemExit(1)
    print("all %d smoke checks passed" % checks)


if __name__ == "__main__":
    main()
