"""Browser uploads into an inbox folder, in chunks, onto disk.

On a server the browser is on another machine -- a phone, usually -- so "point
at a folder" has nothing to point at. Files come up over HTTP instead, into
`inbox/<when>[_<name>]/`, and that folder is then imported exactly as a local
one would be.

The shape is set by phone footage: one clip is 1-10 GB, the link is Wi-Fi or
cellular, and Safari stops when the screen locks. So:

* the page slices each file (`Blob.slice`) and sends it a chunk at a time, with
  an offset. The server writes each chunk where it says it goes, streaming to
  disk, and never holds more than a read buffer of it in memory;
* which byte ranges have arrived is recorded beside the partial file, so a
  chunk sent twice, or out of order, or after a reconnect, is simply written
  again over the same bytes. The upload id is derived from (folder, name,
  size), so asking to start the same file again returns where it got to;
* only when every byte is there, and the file on disk is exactly the size
  announced, is it moved -- one `os.replace`, on the same filesystem -- from
  `inbox/.partial/` into its folder. A half-sent clip is never in a folder an
  import could read.

Names are checked, not trusted: no separators, no `..`, nothing hidden, and
only the extensions an import reads. Every path is resolved and checked to be
inside the inbox before anything is written to it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path

from racechrono import video

ALLOWED_EXTS = frozenset(set(video.VIDEO_EXTS) | {".csv"})
CHUNK = 8 << 20                  # what the page is told to send
MAX_CHUNK = 64 << 20             # what the server will accept in one request
RESERVE = 1 << 30                # free space left over after every upload
PARTIAL = ".partial"

_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} \
    | {"LPT%d" % i for i in range(1, 10)}
_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,99}$")
_UID = re.compile(r"^[0-9a-f]{24}$")


class UploadError(ValueError):
    """A refusal with the HTTP status that describes it."""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def max_file_bytes(environ=None):
    env = os.environ if environ is None else environ
    try:
        gb = float(env.get("LAPVIEW_UPLOAD_MAX_GB") or 64)
    except ValueError:
        gb = 64.0
    return int(gb * (1 << 30))


def safe_filename(name):
    """The name a file is stored under, or UploadError. Never a path."""
    if not isinstance(name, str):
        raise UploadError("a file name has to be text")
    name = unicodedata.normalize("NFC", name).strip()
    if not name:
        raise UploadError("a file needs a name")
    if len(name) > 200:
        raise UploadError("that file name is over 200 characters")
    if "/" in name or "\\" in name:
        raise UploadError("a file name cannot hold a path: %r" % name)
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise UploadError("a file name cannot hold control characters")
    if ".." in name:
        raise UploadError("a file name cannot hold '..': %r" % name)
    if name.startswith("."):
        raise UploadError("hidden files are not accepted: %r" % name)
    # Harmless on Linux, but this also runs on Windows, where they are not.
    name = re.sub(r'[<>:"|?*]', "_", name)
    stem, ext = os.path.splitext(name)
    if ext.lower() not in ALLOWED_EXTS:
        raise UploadError("%r is not a RaceChrono CSV or a video (%s)"
                          % (name, " ".join(sorted(ALLOWED_EXTS))), 415)
    if not stem or stem.split(".")[0].upper() in _RESERVED:
        raise UploadError("%r is not a usable file name" % name)
    return name


def safe_label(text):
    """A typed upload name, cut down to something a folder can be called."""
    out = re.sub(r"[^A-Za-z0-9 _.\-]+", "_", str(text or ""))
    out = re.sub(r"\.{2,}", ".", out)
    return out.strip(" ._-")[:60]


class Inbox:
    """Upload folders under one root, and the uploads in flight into them."""

    def __init__(self, root, max_file=None):
        self.root = Path(root).resolve()
        self.partial = self.root / PARTIAL
        self.max_file = max_file or max_file_bytes()
        self._lock = threading.Lock()
        self._locks = {}            # upload id -> lock around its writes

    # ---------------------------------------------------------- folders
    def ensure(self):
        self.partial.mkdir(parents=True, exist_ok=True)
        return self

    def folder(self, name):
        """An existing folder directly under the inbox, by name."""
        if not isinstance(name, str) or not _FOLDER.match(name) or ".." in name:
            raise UploadError("not an upload folder: %r" % (name,))
        d = (self.root / name).resolve()
        if d.parent != self.root:
            raise UploadError("not an upload folder: %r" % (name,), 403)
        if not d.is_dir():
            raise UploadError("no upload folder called %r" % name, 404)
        return d

    def new_folder(self, label=""):
        """`inbox/2026-11-03_141502[_label]`, made now, never reused."""
        self.ensure()
        base = time.strftime("%Y-%m-%d_%H%M%S")
        tag = safe_label(label)
        if tag:
            base += "_" + tag
        with self._lock:
            name, n = base, 1
            while (self.root / name).exists():
                n += 1
                name = "%s-%d" % (base, n)
            (self.root / name).mkdir()
        return {"folder": name, "path": str(self.root / name)}

    def folders(self):
        """Upload folders, newest first, with what an import would find."""
        out = []
        if not self.root.is_dir():
            return out
        pending = {}
        for meta in self._metas():
            pending[meta["folder"]] = pending.get(meta["folder"], 0) + 1
        for d in self.root.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            csvs = vids = size = 0
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                ext = p.suffix.lower()
                if ext == ".csv":
                    csvs += 1
                elif ext in video.VIDEO_EXTS:
                    vids += 1
                else:
                    continue
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
            out.append({"folder": d.name, "path": str(d), "csvs": csvs,
                        "videos": vids, "bytes": size,
                        "mtime": d.stat().st_mtime,
                        "partial": pending.get(d.name, 0)})
        out.sort(key=lambda r: r["mtime"], reverse=True)
        return out

    def delete_folder(self, name):
        """Remove one upload folder and anything still arriving into it."""
        d = self.folder(name)
        for meta in self._metas():
            if meta["folder"] == name:
                self.abort(meta["id"])
        shutil.rmtree(d)
        return {"deleted": name}

    # ---------------------------------------------------------- uploads
    @staticmethod
    def upload_id(folder, name, size):
        key = "%s\0%s\0%d" % (folder, name, int(size))
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]

    def _paths(self, uid):
        if not isinstance(uid, str) or not _UID.match(uid):
            raise UploadError("not an upload id: %r" % (uid,))
        return self.partial / (uid + ".part"), self.partial / (uid + ".json")

    def _metas(self):
        if not self.partial.is_dir():
            return []
        out = []
        for p in self.partial.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def _load(self, uid):
        _part, meta = self._paths(uid)
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise UploadError("no upload %s in progress" % uid, 404) from None

    def _save(self, meta):
        _part, path = self._paths(meta["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, path)

    def _lock_for(self, uid):
        with self._lock:
            return self._locks.setdefault(uid, threading.Lock())

    def _outstanding(self, but=None):
        """Bytes promised to uploads in flight and not yet received."""
        return sum(max(0, m["size"] - _covered(m["ranges"]))
                   for m in self._metas() if m.get("id") != but)

    def begin(self, folder, name, size):
        """Start an upload, or say where one with these particulars got to."""
        d = self.folder(folder)
        name = safe_filename(name)
        try:
            size = int(size)
        except (TypeError, ValueError):
            raise UploadError("size has to be a whole number of bytes") from None
        if size <= 0:
            raise UploadError("%s is empty" % name)
        if size > self.max_file:
            raise UploadError("%s is %.1f GB; the limit is %.0f GB "
                              "(LAPVIEW_UPLOAD_MAX_GB)"
                              % (name, size / 2 ** 30, self.max_file / 2 ** 30),
                              413)
        done = d / name
        if done.is_file() and done.stat().st_size == size:
            return {"id": None, "done": True, "name": name, "folder": folder,
                    "size": size, "received": size, "ranges": [[0, size]],
                    "path": str(done)}
        self.ensure()
        uid = self.upload_id(folder, name, size)
        part, _meta = self._paths(uid)
        with self._lock_for(uid):
            try:
                meta = self._load(uid)
            except UploadError:
                meta = {"id": uid, "folder": folder, "name": name, "size": size,
                        "ranges": [], "started": time.time()}
            need = size - _covered(meta["ranges"])
            free = shutil.disk_usage(self.root).free
            if need + self._outstanding(but=uid) + RESERVE > free:
                raise UploadError(
                    "not enough space for %s: it needs %.1f GB and %.1f GB is "
                    "free" % (name, need / 2 ** 30, free / 2 ** 30), 507)
            if not part.exists():
                part.touch()
                meta["ranges"] = []
            self._save(meta)
        return {"id": uid, "done": False, "name": name, "folder": folder,
                "size": size, "received": _covered(meta["ranges"]),
                "ranges": meta["ranges"], "chunk": CHUNK, "max_chunk": MAX_CHUNK}

    def check_chunk(self, uid, offset, length):
        """Refuse a chunk before a byte of its body is read."""
        meta = self._load(uid)
        if offset < 0 or length <= 0:
            raise UploadError("a chunk needs a non-negative offset and a body")
        if length > MAX_CHUNK:
            raise UploadError("chunks are at most %d MB" % (MAX_CHUNK >> 20), 413)
        if offset + length > meta["size"]:
            raise UploadError("that chunk runs past the end of %s (%d bytes)"
                              % (meta["name"], meta["size"]), 416)
        return meta

    def write(self, uid, offset, length, stream, bufsize=1 << 20):
        """Write `length` bytes read from `stream` at `offset` of the upload."""
        self.check_chunk(uid, offset, length)
        part, _meta = self._paths(uid)
        with self._lock_for(uid):
            meta = self._load(uid)
            got = 0
            with open(part, "r+b") as f:
                f.seek(offset)
                while got < length:
                    buf = stream.read(min(bufsize, length - got))
                    if not buf:
                        break
                    f.write(buf)
                    got += len(buf)
            if got != length:
                # The bytes that did arrive are on disk but not recorded, so
                # the page's retry sends this chunk again from its start.
                raise UploadError("the chunk at %d stopped after %d of %d bytes"
                                  % (offset, got, length))
            meta["ranges"] = _merge(meta["ranges"], offset, offset + length)
            self._save(meta)
        return {"id": uid, "received": _covered(meta["ranges"]),
                "size": meta["size"], "ranges": meta["ranges"]}

    def status(self, uid):
        meta = self._load(uid)
        return {"id": uid, "name": meta["name"], "folder": meta["folder"],
                "size": meta["size"], "received": _covered(meta["ranges"]),
                "ranges": meta["ranges"]}

    def finish(self, uid):
        """Every byte here and the size right: move it into its folder."""
        part, meta_path = self._paths(uid)
        with self._lock_for(uid):
            meta = self._load(uid)
            size = meta["size"]
            if meta["ranges"] != [[0, size]]:
                missing = _gaps(meta["ranges"], size)
                raise UploadError(
                    "%s is not all here: %d byte(s) missing in %d range(s)"
                    % (meta["name"], sum(b - a for a, b in missing),
                       len(missing)), 409)
            on_disk = part.stat().st_size
            if on_disk != size:
                raise UploadError("%s is %d bytes on disk, %d were announced"
                                  % (meta["name"], on_disk, size), 409)
            with open(part, "r+b") as f:
                os.fsync(f.fileno())
            d = self.folder(meta["folder"])
            with self._lock:
                target = _unique(d / safe_filename(meta["name"]))
                if target.resolve().parent != d:
                    raise UploadError("refusing to write outside %s" % d, 403)
                os.replace(part, target)
            meta_path.unlink(missing_ok=True)
        with self._lock:
            self._locks.pop(uid, None)
        return {"id": uid, "done": True, "name": target.name,
                "folder": meta["folder"], "size": size, "path": str(target)}

    def abort(self, uid):
        part, meta = self._paths(uid)
        with self._lock_for(uid):
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
        with self._lock:
            self._locks.pop(uid, None)
        return {"id": uid, "aborted": True}

    def sweep(self, max_age_s=7 * 86400):
        """Forget uploads nobody has touched in a week. Returns how many."""
        gone = 0
        now = time.time()
        for meta in self._metas():
            part, path = self._paths(meta["id"])
            try:
                age = now - max(path.stat().st_mtime,
                                part.stat().st_mtime if part.exists() else 0)
            except OSError:
                continue
            if age > max_age_s:
                self.abort(meta["id"])
                gone += 1
        return gone


def _merge(ranges, lo, hi):
    """Add [lo, hi) to a sorted list of disjoint [a, b) and coalesce."""
    out = []
    for a, b in sorted([list(r) for r in ranges] + [[lo, hi]]):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _covered(ranges):
    return sum(b - a for a, b in ranges)


def _gaps(ranges, size):
    out, at = [], 0
    for a, b in ranges:
        if a > at:
            out.append([at, a])
        at = max(at, b)
    if at < size:
        out.append([at, size])
    return out


def _unique(path):
    """`path`, or `name (2).ext` and onwards if something is already there."""
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name("%s (%d)%s" % (path.stem, n, path.suffix))
        if not cand.exists():
            return cand
        n += 1
