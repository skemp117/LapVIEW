# Design

What this tool is, how data is organised, and why. Decisions recorded here are
settled unless revisited; open questions are marked **OPEN** and collected at the
end. Numbers in this document are measured on the twenty sessions and nineteen
clips currently in `Raw Data/` and `Raw Footage/` — three venues, two of them
parking-lot autocross courses and one a road circuit.

`README.md` records how the existing pieces were built and what was measured
along the way. This file records what is being built.

---

## 1. What the user brings, and what they get back

They arrive with **CSV telemetry from RaceChrono** and **video files** — mp4, mov,
or whatever their phone or camera produced. Nothing else: no RaceChrono project,
no VBO, no `.rcz`.

They leave with a **library of laps**. Each lap is one video file and one data
file, trimmed to that lap with a little padding, playable and comparable, with an
overlay that can be configured and exported.

Everything between those two sentences is ingest.

---

## 2. The hierarchy

```
Track
 └── Day (a date)
      └── Session   (one CSV; the unit RaceChrono recorded)
           └── Lap  (the atomic unit)
                └── Split
Event ─ a tag applied to days, not a level of the tree
```

**Every level has a stable internal id and a separate editable display name.**
Nothing references anything by name, and nothing references anything by filename.
The name RaceChrono exported and the file it came from are kept as *provenance* --
what this record was called when it arrived -- and are never the thing a lap, a
map or a comparison points at. §6 covers editing; the reason it has to work this
way is that "SCCA Autocross" is going to be renamed to the real event name, and a
rename must not detach a single lap.

### Track is the top level, and comparison never crosses it

Comparing two tracks is not useful, so the tool does not offer it. This is
already enforced: a cross-venue comparison is refused with a stated reason
rather than producing numbers.

**The same physical location can hold several tracks.** Two decisions follow:

- **AMP clockwise and AMP counter-clockwise are different tracks.** Same tarmac,
  different track. So are two autocross courses laid out in the same parking lot
  on the same day.
- Therefore a track is *not* identified by its position. It is identified by its
  **shape and direction of travel**. §5 covers how, with measurements.

### Day is the sorting level; Event is a tag

Events are usually one day, so making Event a level of the tree would add a
directory with one child in it almost every time. Event becomes a label attached
to days — "SCCA Points Event 4", "Track Night in America" — and a multi-day event
is simply the same tag on two days. Days sort; tags filter.

### Session is where the calibration lives

A session is one CSV, and it is the unit RaceChrono recorded. It is **not** the
unit of playback, but it is the unit of *calibration*, because the phone or DAQ
may be moved between sessions. Everything solved per session stays attached to
the session and travels with its laps:

- the IMU vehicle-frame solve (tilt, heading offset, gyro bias, whether it was
  trusted) — measured spread across ten sessions: heading 3.0°, tilt 4.4°, gyro
  bias 0.30 °/s, and one session in ten rejected outright with `|g|` = 1.41
- the video sync offset and its confidence
- the channel set, which differs by phone: 15 channels on the circuit sessions,
  31 on the autocross ones

So a lap's record references its session, and a lap is never re-derived using a
different session's calibration. **A lap is the atomic unit for storage and
playback; the session is the atomic unit for calibration.** That distinction is
the direct consequence of the phone being moved between runs.

### Session ↔ video is many-to-many

Common: **several videos per session** — a camera chapters every few minutes, and
a lap can straddle the split. Already handled: a lap window spanning two files is
rendered as two segments and joined.

Rare but real: **several sessions per video** — one long recording covering two
sessions. A video is therefore referenced by time range and not owned by a
session. In storage terms, ingest reads video files and writes lap clips; no lap
record points at a source file, so the rare case costs nothing extra.

### Lap is the atomic unit

For a circuit session, a lap is a lap. For an autocross run, the run *is* the lap
— one crossing of a course from start to finish. One type covers both, which is
already how comparison works.

A lap that no complete crossing produced — a walked course, an aborted run, a
crash — is not a lap and is not stored. §4 covers rejection.

### Split is a sub-division of a lap

Splits exist so that "where did the time go" has an answer coarser than a trace
and finer than a lap time. Two decisions:

- **Splits are defined per track, not per session or per lap.** Otherwise two
  laps cannot be compared split by split, and a session-to-session table means
  nothing. Splits are a property of the track's centreline: a list of distances.
- **Default to equal distance**, because that requires no input and is stable.
  Equal *time* splits would move between laps.

**RaceChrono's CSV export contains no split data.** Checked exhaustively across
all twenty sessions: the export has eight metadata keys — `Format`,
`Session title`, `Session type`, `Track name`, `Driver name`, `Created`, `Note`,
and the version banner — and thirty-one column names, none of which mentions a
split, sector, segment or intermediate. `lap_number` is the only timing structure
in the file. So splits cannot be imported and are ours to define.

Since nothing can be imported, **splits are editable on the track**: added, moved
and removed by clicking the map, stored with the track as a list of centreline
distances, and shared by every lap at that track. Equal-distance divisions are
the starting point, not the only option. §6 covers what editing them costs --
split times recompute, lap times do not move.

---

## 3. Storage

A local library, written once at ingest, read thereafter.

```
library/
  library.db                       SQLite: the index and all metadata
  tracks/<track-id>/
    map.json                       centreline, extent, transform, splits
    map.svg                        preview
  laps/<lap-id>/
    video.mp4                      trimmed, clean (no overlay burned in)
    data.csv                       the lap's rows, plus its session's calibration
```

Decisions:

- **SQLite for the index, files for the payload.** One file to back up, no
  server, and a lap's video stays a video that any player can open. The database
  holds tracks, days, events, sessions, laps, splits and lap times; it does not
  hold video or telemetry rows.
- **Stored video is clean, not overlaid.** The user wants to configure what the
  overlay shows, so the overlay has to be rendered at export from the lap's data.
  Burning it in at ingest would freeze that choice permanently. The preview
  already composites an overlay live at 227 frames/s over HTTP, which is 3.8× a
  60 fps preview, so there is no need to pre-render.
- **No raw archive.** The user's decision: originals are theirs to keep. §4.4
  records what that costs, because it is not free.

### Living with a library for years

Three things a store people keep data in needs, which a store that is only ever
written once does not:

**Migrations.** The first version of this schema had no `laps.video_t0`, and
`CREATE TABLE IF NOT EXISTS` does not add a column to a table that already
exists — so an older library opened cleanly and then failed on the next lap
written, which is worse than refusing to open. Each schema version now carries
the statements that bring the previous one forward, applied on open. Verified on
a real library: rolled back to v1, reopened, migrated, data intact, inserts work,
second open a no-op.

**Verify and repair.** Editing and deleting over months is what makes this
necessary. Three kinds of drift, all found and the safe half fixed:

| drift | how it happens | repair |
|---|---|---|
| byte count ≠ file size | recorded at write time, then something touches the file | re-read from the file |
| row points at a file that is gone | deleted by hand, or an interrupted write | clear the path |
| directory with no row | an ingest interrupted between the encode and the insert | remove it |

Rows are never invented and files are never re-encoded. Orphaned *rows* — a lap
whose session vanished — are reported and not touched, because guessing a parent
is worse than saying so.

**Merge and split.** The identifier reports an ambiguity rather than guessing
(§5), which is only useful if there is then something to do about it. Two tracks
that turn out to be one course merge: days on a shared date become one day, laps
are untouched, and the kept track absorbs the other's source names so a later
import under either name still matches. One track that turns out to be two
courses splits: sessions move to a new track which starts with **no map**,
because the old map describes the old shape.

### Reclaiming space

Deleting laps one at a time is not how anyone would do it. Selection is by rule —
slower than N seconds, the out-laps and in-laps, all but the best N per track,
laps with no footage — and **selection prints, it does not delete**. Removal is a
second command, because with no raw archive it cannot be undone.

---

## 4. Ingest

The one pipeline that runs when files are added. Every step reports what it did
and why, because a step that silently drops something is how a dataset ends up
half-imported.

### 4.0 Where the files come from

**A folder, read where it sits.** There is no `Raw Data` and no `Raw Footage`:
someone points at a folder — a card copied off a phone, a day's exports, both
kinds together — and it is scanned recursively, telemetry and video told apart by
extension rather than by which directory they are in.

Nothing is copied. Uploading 16 GB of footage into a staging area to import it
and then delete the staging area is work and disk for no gain when the server and
the browser are the same machine.

**Nothing is deleted either.** The import says when the library holds a verified
copy, and leaves the decision to the person who owns the files:

```
checked: 43 lap(s), 6.5 GB on disk.
the 39 file(s) in D:	rack day (16.1 GB) are now safe to delete.
```

That sentence is the only place this tool invites anyone to delete irreplaceable
footage, so it is earned rather than assumed: every lap written is checked for a
row, a telemetry file, and a video where one was expected, none of them empty. If
any check fails the sentence is not printed and the problems are.

Three things had to be added to that check before the sentence could be trusted:

- **A file that could not be read blocks it.** One corrupt export in a folder is
  not in the library, and the sentence used to cover the whole folder anyway.
- **Laps a filter left behind are named.** "Only the quickest 3" is a deliberate
  choice, but the folder then holds laps the library does not, and someone about
  to delete it should be told the count rather than left to remember.
- **Sessions already held are named too.** A second import writes nothing, and
  saying "safe to delete" with no explanation would read as a failure.

**On the drag-and-drop area.** A browser hands over a dropped folder's *name* and
the files inside it, never an absolute path — that is a deliberate security
boundary and not something to work around. So the drop zone fills in what it can
and says what it cannot — it names the folder that was dropped and points at the
chooser — and the operating system's own folder dialog, opened by the server, is
what actually decides. A drop zone that silently half-worked would be worse than
none.

### 4.1 Identify

Read each CSV, classify it, and pair videos to it by container timestamp. This
already exists: `racechrono/shapes.py` classifies a session and states which
capabilities its shape supports, and `tools/dataset_report.py` prints the result
for a folder. Ingest is that report plus writes.

What the classifier decides, and why each one was needed:

| fact | why it is not obvious |
|---|---|
| which track | position is not enough — §5 |
| circuit or open course | changes lap slicing, map building and smoothing |
| are the laps real | `lap_number` reports one lap on an autocross course with no start/finish trap |
| how many runs | a session held one, or a whole evening, or two with staging between |
| raw IMU present | 31 channels on one phone, 15 on another |

**Byte-identical duplicates are reported, not imported twice.** Two files in the
current set have the same md5; importing both would double that run's weight in
every average the map builder and the ideal lap take.

### 4.2 Build or refine the track map

Only when the track has no map. A map exists per track, and a track is a shape
and a direction, so a second course in the same lot gets its own.

Map building already: averages traversals by projecting them onto a shared
reference rather than by fraction of their own length (measured: 0.81 m median
deviation against 3.74 m), rejects laps outside a time window, and for an open
course trims the ends to the span every run agrees on.

**Outlier rejection at ingest.** A walked course, a crash, an incomplete lap or a
major line deviation must not enter the average. Time is the discriminator, not
shape:

- **length**: a traversal outside 0.75–1.25× the median is dropped. This catches
  an incomplete lap and a major detour. It cannot catch a walked course, which
  covers exactly the right distance.
- **time**: a traversal slower than 2.5× the median is dropped. A 1341 m course
  walked at 1.4 m/s takes 958 s against a 103 s median run — **9.3×**. The
  slowest real run measured across all twenty sessions was **1.12×** its
  session's median. The threshold sits in a gap with a factor of two of headroom
  on one side and nearly four on the other, so **a slow run is never what this
  catches**. That matters more than catching the walk: a slow lap is data.
- In practice the walk never reaches this filter. `runs.detect` gates movement at
  **3.0 m/s** and walking is about 1.4, so a walked course produces no movement
  segments and yields no runs at all. The time filter is the second line.

No shape filter. A shape test would be the thing most likely to reject a genuinely
bad-but-real lap — a spin, a wide moment, an off — and those are exactly the laps
someone wants to look at.

**Refining an existing map** re-runs the average over the union of the old
source traversals and the new ones. This is why the database records which laps
a map was built from. Replacing outright is also offered, because a map built
from a bad first day should not haunt the track forever.

**OPEN**: importing a track file. Until the format is known this is a stub. The
map format is already a stored transform plus a unit-space polyline, so an
importer needs only to produce those two things.

### 4.3 Trim, split, compress

The user is offered these three at ingest. Defaults matter more than the options,
because most people will accept them.

1. **Trim** to the driving. Not optional: a third to a half of the footage is
   paddock, grid and staging, and none of it is worth keeping. Only the padding
   is a choice.
2. **Split into laps**, with padding. This is the step that produces the atomic
   unit. Padding default 3 s either side — enough to see the corner before the
   line and the exit after it.
3. **Compress.** Discussed below, because this is where the size actually is.

### 4.3b The import screen

Three steps: **choose a folder**, **watch it work**, **look at what came of it**.
Exactly one of them costs anything, and it is the one behind a button.

**Choose.** A drop zone, the operating system's own folder chooser, and a box to
paste a path into. Choosing a folder counts what is in it and says so — *"5 data
file(s), 2 video file(s), 16.8 MB"* — and nothing more. No parsing, no probing,
no planning: those come later, when asked.

The chooser is opened by the *server*, in a throwaway process, because a page
cannot open one that reports a path — that is a security boundary, not an
oversight — and a GUI toolkit started inside a request thread of a long-running
server is a good way to lose the server. The server is on the same machine as
the person clicking, so it can open the dialog they already know.

An earlier version of this screen re-planned the whole import on every keystroke
so it could show a live estimate of laps kept and gigabytes written. That
estimate was accurate — within 5% of the real result — and it was still the wrong
design: classifying and identifying nineteen sessions is about twelve seconds, so
every checkbox felt broken, and nobody comes to this screen to read an estimate.
Compute happens when it is asked for.

**The options are on the screen**, not folded away, and each one says what it
costs, measured rather than guessed. They were behind a collapsed disclosure at
first, which put the only decisions on the screen one click out of sight and made
the page look empty; and they were laid out as plain inline labels, so on a small
display the text, checkbox and number field of one option wrapped into the middle
of the next. One option per row, a label column the controls line up against, and
number fields four digits wide:

| control | effect |
|---|---|
| skip out-laps and in-laps | 43 laps → 35 |
| only the quickest N per session | N=3 → 28 laps |
| only laps within N% of the session best | 5% → 24 laps |
| skip laps with a GPS hole over N m | 50 m → 28 laps |
| skip laps with no footage | 43 → 38 |
| CQ 22 / 24 / 28 / 30 / 32 | −20% … −65%; 28 is the default, 24 can *grow* a section |
| 720p | −52% |
| 30 fps | −44% |
| drop audio | −1% — stated because it looks like a space control and is not |
| import video at all | −100%: telemetry only |
| padding either side | 3 s by default |
| event tag | applied to every day the import touches |

**Work.** One button starts it, and from there the screen reports rather than
predicts: the phase in words (*reading the folder → building track maps →
cutting laps → checking what was written*), a bar, and the lap being cut with an
ETA once there is enough history to compute one. Reading and mapping happen
before any lap is cut, so a bar driven only by laps would sit at zero through
them; it shows a floor instead, which says something is happening without
pretending to know how far along it is. Closing the tab does not stop the
import, and coming back joins it in progress.

**Review, and then back.** What was actually produced, which is the part worth
looking at:
each track with the map that was built out of these laps — a shape can be
recognised at a glance, a length in metres cannot — and under it every lap that
was kept, with its time, whether it has footage, how big it is, and whether its
GPS had a hole. Then the counts, and the sentence about deleting the originals
(§4.0), which appears only when every lap has been read back off disk and
checked.

The review is not a dead end: it offers **import another folder**, which returns
to the chooser with the last path still in the box. A day's footage usually
arrives in more than one place — two cards, a phone, a folder someone shared —
and there was no way back to the chooser without restarting the server.

**Importing the same folder twice is a no-op.** A session is identified by when
it started and how long it ran; a phone cannot produce two different recordings
from one instant, and a re-export of the same session lands on the same pair. The
second pass reports what it skipped and writes nothing. This mattered as soon as
the review led back to the chooser: without it, pointing at the same folder again
doubled the library — every session, every lap, every byte of video. The
within-import byte-identical check cannot see across imports, because a re-export
is a different file.

### 4.4 What trimming and compression cost

Both are worth doing, and they multiply. Measured on the current set, with
padding clamped to the footage that actually exists and overlapping padding
between adjacent laps counted once:

| venue | footage held | kept after trim | **saved** |
|---|---|---|---|
| AMP (4 sessions, 27 laps) | 5653 s / 9.44 GB | 3612 s | **36%** |
| Grip (10 sessions, 10 runs) | 396 s / 0.59 GB | 228 s | **43%** |
| SCCA (6 sessions, 7 runs) | 1622 s / 2.40 GB | 758 s | **53%** |

Trimming removes a third to a half of the footage, and all of what it removes is
paddock, grid and staging. Nobody wants gigabytes of that, so **trimming is not
optional and is not offered as a choice** — only the padding is configurable.

> An earlier version of this document said trimming was "not where the saving
> is", on the strength of a figure showing Grip *growing* to 105% of its
> recording. That figure was wrong: it added 3 s of padding either side of every
> run without clamping it to the footage that exists, and without noticing that
> two adjacent laps' padding covers the same seconds. Corrected, Grip saves 43%.

Compression is the second lever, and it is the larger one. It also has to be
measured against a **stream copy of the same window**, which took two attempts to
get right: these phone files vary from **12.0 to 20.6 Mbps within themselves**,
so comparing an encode of one section against a file's mean bitrate compares two
different things.

Four 105 s windows from three phones, hevc_nvenc preset p5, against a copy of the
same window:

| window | stream copy | CQ 24 | CQ 28 | CQ 30 |
|---|---|---|---|---|
| AMP WGLZ0137 @200 s | 20.6 Mbps | −25% | −44% | −56% |
| AMP WGLZ0137 @700 s | 14.5 Mbps | **+11%** | −7% | −28% |
| AMP CJHC9467 @300 s | 12.5 Mbps | −5% | −36% | −51% |
| SCCA EYIK4461 @120 s | 12.0 Mbps | −23% | −58% | −69% |
| **overall** | **14.9 Mbps** | **−12%** | **−36%** | **−51%** |

Two decisions:

- **The default is CQ 28.** A third smaller than source, and it never grows a
  section — worst case −7%.
- **The default must not be near-source.** CQ 24 was the first choice, picked
  from a single slice that happened to be a low-bitrate section, on the reasoning
  that a permanent choice should be timid. It *grows* some sections by 11% — the
  exact failure it was chosen to avoid — and a full library ingested at CQ 24
  came out at 15.7 Mbps mean, above its own source. Timid is not the same as
  conservative. CQ 24 remains available for someone who wants near-source, with
  its behaviour stated.

Preset barely matters at a fixed CQ: p4, p5 and p7 landed within 1% of each other
(156.0, 155.6 and 154.2 MB on the same window) while taking 17, 34 and 61 s. p5
is the default for that reason.

Measured end to end on the whole working set — 20 sessions, 19 after the duplicate
is skipped, 12.43 GB of footage in:

| | laps | stored | saved |
|---|---|---|---|
| footage in | — | 12.43 GB | — |
| trimmed, CQ 24 | 43 | 9.08 GB | 27% |
| **trimmed, CQ 28** | **43** | **6.50 GB** | **48%** |

The CQ 24 library came out at a 15.7 Mbps mean — above its own source — which is
how the default got revisited. At CQ 28 the same 43 laps are a third smaller
again, and half the footage that came in.

### 4.5 The lap's data file

One CSV per lap containing that lap's rows, plus a header carrying the session's
calibration and provenance: session id, IMU solve and whether it was trusted, the
sync offset and confidence, the channel set, and the source file names. Written
so that the lap can be re-derived without the session, since the session's CSV
is not kept either.

---

## 5. Track identity, and whether matching can be automatic

The hard question, because a track is a shape and a direction rather than a
place. Three statistics, measured against every map and every session currently
held. Each is computed by matching a run's driven path to a candidate
centreline — the same machinery comparison uses.

- **fit** — 90th-percentile perpendicular distance from the centreline
- **coverage** — fraction of the centreline the run actually traverses
- **direction** — fraction of consecutive samples that advance along it

### Different location

| candidate map | AMP runs | Grip runs | SCCA runs |
|---|---|---|---|
| AMP Full | **5.6 m** | 171022 m | 117026 m |
| Grip 8-22 | 171841 m | **3.0 m** | 166365 m |
| SCCA Autocross | 117482 m | 167257 m | **57.5 m** |

Four orders of magnitude. Trivially separable. (SCCA's own fit is 57.5 m rather
than a few metres because that map is trimmed to 1107 m of a ~1330 m driven path,
so the untrimmed ends sit far from the line. Worth fixing; not a matching
problem.)

### Same location, different course

The two parking-lot courses translated onto one origin, so real driven geometry
answers a case the data does not otherwise contain:

| Grip run projected onto | fit | coverage | direction |
|---|---|---|---|
| the SCCA course (wrong) | 150–157 m | 0.00 | 14–15% |
| its own course (right) | 2.2–3.5 m | 0.99–1.00 | 90–95% |

Separated by ~50× on fit and completely on coverage. **Caveat:** the two courses
differ in size by 2.2× (494 m against 1107 m, 153×59 m against 547×144 m), so
this is an easy instance. Two *similar* courses in one lot would be closer, and
that threshold is not yet validated.

### Same course, opposite direction

Reversing a closed centreline leaves every point exactly where it was:

| AMP map | fit | coverage | direction |
|---|---|---|---|
| forward | 5.58 m | 7.99 laps | **94.2%** |
| reversed | 6.80 m | nonsense | **0.1%** |

**Position and shape cannot tell clockwise from counter-clockwise: 5.58 m against
6.80 m.** Only direction of travel separates them, and it does so completely.
For the *open* course the reversed fit is also geometrically wrong (90.78 m
against 2.98 m) because start and finish differ — but a circuit needs the
direction test, and it is the AMP CW/CCW case the user raised.

### The name in the file is a strong prior

The export carries a **`Track name`** the user typed in RaceChrono, and across
all twenty sessions it is exactly the track identity wanted here: `"AMP Full"`,
`"Grip 8-22"`, `"SCCA Autocross"`. It is also the cheapest possible match, and it
groups a day's sessions correctly with no geometry at all.

It is a prior and not the answer, for three reasons the geometry has to settle:

- One name can cover two tracks. `"AMP Full"` says nothing about direction, and
  §5 shows direction is invisible to shape.
- A name gets reused. Next month's `"SCCA Autocross"` is a different course in
  the same lot, and it will arrive with the same string in the header.
- **The user renames things.** Once `"SCCA Autocross"` has been renamed to the
  real event name, the *next* import still arrives carrying `"SCCA Autocross"` --
  RaceChrono does not know about the rename. So a track keeps the set of **source
  names it has been seen under**, separately from its display name, and matching
  uses that set. Renaming a track adds nothing to it and removes nothing from it.
  Matching by display name would break the moment the user did the thing they
  already said they intend to do.

So: **match by name to shortlist, verify by geometry, and split the name into two
tracks when the geometry says they are two.** A name that has never been seen with
this shape proposes a new track.

### The decision

**Matching is automatic with a confirmation step, not silent.** Ingest proposes a
track using the name as a shortlist, then fit, then coverage, then direction, and
shows the numbers. Accepting
is one click; overriding creates a new track or picks a different existing one.
Automatic-and-silent is wrong because the same-similar-courses threshold is
unvalidated, and manual-always is wrong because the separation is 50× in the case
that actually occurs.

A run matching no existing track proposes a **new** track, which is the correct
default for an autocross day: a new course in a known parking lot is a new track.

---

## 6. Editing and deleting

### Names and metadata are editable everywhere

Track, event, day, session and lap all carry an editable display name and free
text notes. None of it is derived from a filename at read time; the filename and
the exported `Track name` are recorded once, as provenance, and then never
consulted again except by the matcher's source-name set (§5).

What is editable, and what editing it costs:

| field | editable | effect |
|---|---|---|
| display name, notes, event tag | yes | label only; nothing is recomputed |
| a day's date | yes | re-sorts; laps unaffected |
| which track a session belongs to | yes | its laps move; split times recompute against the new track's centreline |
| splits on a track | yes | split times recompute; lap times unaffected |
| a track's map geometry | via refine or replace (§4.2) | split times and delta traces recompute; lap times unaffected |
| a lap's time | **no** | it comes from the session's own timing, not from us |

The pattern is that **labels are free, geometry recomputes, and measurements are
not editable.** A lap time came from RaceChrono's timing; nothing in this tool
should let it be typed over. Split times, delta traces and every insight are
derived on demand rather than stored, so refining a map or moving a split changes
them everywhere at once and no stored artifact goes stale.

### Laps are deletable

A user should be able to remove a lap because it is wasting space or because they
do not want it. Deleting a lap deletes its video, its data file and its record.

Because there is no raw archive, this is irreversible, so the confirmation states
what is going and how much space it frees. Consequences, decided:

- **A map is not silently changed by a deletion.** If a deleted lap was one of the
  traversals a track map was averaged from, the map stays exactly as it is. The
  map's provenance list records the deleted lap as deleted rather than dropping
  it, so "built from 12 laps, 3 since deleted" is visible and honest. A later
  refine uses whatever laps still exist.
- **Empty sessions and days are removed** once their last lap goes; there is
  nothing left to describe.
- **An empty track is kept if it has a map**, because the map is expensive to
  build and the next event at that course should reuse it. Deleting the track
  itself is a separate, explicit action.
- **Bulk deletion is the practical case.** "Delete every lap slower than N",
  "delete the out-laps and in-laps", "delete everything from this day but the
  best three" — these are how someone actually reclaims space, and they are worth
  having early. The out-lap/in-lap case is already computed: 19 of 27 AMP laps
  are representative, and the other 8 include a 3m35 in-lap.

---

## 7. Playback, comparison, overlay

**The unit is the lap.** It was the session, because that is what a folder of
CSVs contains — but a session is not a thing anyone watches. It is a recording
that happens to hold one run, or eight laps, or a whole evening. The player picks
a lap; the picker groups laps by track and day.

**One clock, and it belongs to the lap's file.** `t` is seconds from the first
row of the lap's own telemetry, which begins before the timed lap by whatever
padding was granted. Two offsets follow from that and both are stored rather than
assumed:

- `lap_start` / `lap_end` — where the timed lap sits inside its file
- `video.offset` — where the video begins. **Not** `lap_start - pad`: padding is
  clamped to the footage that exists, and a lap that began before the camera was
  rolling has an offset well into the file. One fixture lap has an offset of
  112 s, and asking for a moment before that correctly clamps to the first frame.

**The third tab is the library, not the importer.** Importing is one thing you
do to a library; the rest of the time you are looking at what is in it and fixing
it. The tab lists every track, the events under it, and the laps under those,
with a rename, a tag and a delete on each -- plus checkboxes for a bulk delete of
laps or of their video alone. Importing is the card underneath.

It is laid out as a file explorer, because everyone already knows one: column
headers (name, time, video, tags), tracks and events as folders that open and
close, click to select, ctrl-click to add, shift-click for a range, double-click
to play a lap. The tools are a fixed row at the top -- rename, edit tags, delete
video only, delete, clear selection -- greyed out when the selection gives them
nothing to do. Per-row buttons were the first attempt and they were wrong twice
over: five buttons on every line is most of the screen, and they put the verbs in
a different place on each row.

The listing is **one** CSS grid with `display: contents` rows. Each row was its
own grid at first, so a `1fr` name column resolved against that row's own text
and the times landed at a different x on every line -- a table's alignment with a
div's event handling is what this gets.

Editing is done with `prompt` and `confirm`. They are ugly, and they are the only
text input that cannot be half-committed: no field left dirty, no save button to
forget. For something that renames and deletes, that is the right trade in this
phase.

Every action does one thing, re-reads the library, and redraws from what came
back. Nothing is patched in place, so the screen cannot drift from the database --
and a delete that failed does not look like it worked.

**Naming is required at import.** The screen will not start an import without an
event name and a track -- either an existing one to add to, or a name for the new
one it is about to create. The route refuses it too, because that is what writes
to the library. A library is worth keeping because its names mean something, and
the name a phone puts in a CSV does not: a season's sixth event arrives as
"Grip 8-22". For autocross the event and the track are usually the same name; an
event that ran more than one course gets a track each, numbered from that name.

Choosing an existing track overrides identification -- the person importing knows
where they were -- but the session is still measured against that track's
centreline, and one that does not fit says so on the review (*"assigned to AMP
Full but the shape does not fit it: 171028 m off the line at p90"*) rather than
being filed quietly in the wrong place.

**The header picks a track; the sidebar picks a lap, grouped by event.** A flat
select of every lap in the library was unreadable by the second event, and it also
had the hierarchy backwards: a track is the thing you choose to look at, and laps
are what it contains.

**Comparison is between laps at a track.** The library already knows which laps
share a centreline, so there is no venue to assemble session by session: opening
a lap offers every other lap at its track, across every day.

**The selection is the mode.** There is one lap list, in one sidebar, and what is
selected in it decides what the screen shows: one lap is a player, two or more is
a comparison of exactly those laps. Comparison *is* multi-selection, so a
separate tab was two lists over one library that had to be kept in step -- which
is where two bugs came from (an empty list that never retried, and a stale one
after switching lap). The tabs are now **laps** and **library**.

What changes with the mode is small, which is the argument for merging: the band
under the transport holds the channel traces or the delta/speed plots and the
sector table; the map slot draws the driven line or where the time went; the
ideal-lap card appears. The video, the transport and the readout are the same in
both -- and keeping the video on screen is what makes *click a distance, see that
corner* work without changing screens.

- **Two gestures, one each.** Clicking a lap plays it and makes it the
  reference; ticking its checkbox puts it in the comparison. Clicking never
  changes what is compared and ticking never changes what is playing, so picking
  a different lap to watch cannot throw away the comparison you just built --
  which is exactly what a click did when it also meant "select only this".
- **The reference is the lap in the player**, marked in the sidebar. One idea
  rather than two, and the separate reference dropdown is gone. Watching a lap
  ticks it, because a comparison cannot measure against something that is not in
  it -- unless the eight are taken, in which case it plays and the comparison is
  left alone rather than dropping one of your choices.
- Shift-tick takes a range of checkboxes, anchored on the last one ticked.
- **While comparing, the time column becomes the gap to the reference.** That is
  the number being looked for; the absolute times are in the sector table.
- **A mode toggle that sets itself.** Selecting a second lap turns comparison on;
  the button can force it either way, and the next selection change re-derives
  it. A mode you must remember to set is a mode you forget.
- **Eight laps at once is the limit, enforced on the selection.** The ninth tick
  is refused with a sentence rather than accepted and quietly dropped from the
  comparison; a shift-click over a longer range keeps the eight nearest its
  anchor. Eight traces on one plot is already more than anyone reads.
- **The comparison waits a second after the last click.** Picking three or four
  laps in a row is normal, and each one is a traversal to project and
  difference.
- **The channel traces are hidden, not cleared, when comparing.** Flipping to a
  comparison and back must not throw away the traces someone opened.



Largely built; the design note is what changes when laps become the unit.

**The page is a page, not a fixed-size app.** Every screen scrolls, and nothing
inside one is squeezed to fit the window: content that does not fit makes the page
longer. The first version put everything inside `100vh` with `min-height: 0` on
each box, so the lap note lost its second line, the readout lost its last two
rows, and a fourth channel trace squashed the other three.

Sizes are relative. The sidebar is `clamp(17rem, 24vw, 26rem)` rather than
`300px`; traces are `clamp(4rem, 11vh, 9rem)` rather than leftover height divided
by their number; the one-column breakpoint is `54em` rather than `860px`. In
`rem`/`em` the whole layout follows the reader's font size and browser zoom, which
is the only user-scaling worth having for free. Padding, gaps and border radii
stay in pixels: they are the thickness of the chrome, not the size of anything.

- **Playback** is per lap: video, synchronised track map, channel traces, live
  overlay, one shared clock. Because a lap clip starts at the lap, the clock
  starts at zero and the padding is negative time. A lap with **no footage still
  gets the overlay** — half the laps of an autocross day have none, and the
  overlay is then the whole picture, letterboxed into the player. The toggle sits
  with the playback controls, where it belongs.
- **Speed reads in mph**, with no control to change it. A single "mph" checkbox
  in the corner was the wrong shape for the question: units are per channel and
  worth remembering, which is its own job rather than one global switch.
- **Comparison** is between laps at one track, across sessions and days, on a
  shared centreline distance. Built. The venue-wide list is already the default
  because an autocross session holds one run and comparing runs means comparing
  across sessions.
- **The overlay is configurable** — which elements, where, what channels, what
  units. This is why stored video is clean. The element registry and layout
  already come from configuration; what is missing is a UI for editing it and a
  place to save named layouts. **Decision:** overlay layouts are stored per user,
  not per lap or per track, and are chosen at export.
- **Insights** — minimum corner speed, throttle application point, coasting,
  braking points, gear inference, and where time was lost against a reference —
  are computed from the lap's data on demand rather than stored. They are cheap
  next to decoding video, and storing them would freeze them at the version of
  the code that wrote them.

---

## 8. Export

**From the library, not from the folder the footage arrived in.** That folder is
the user's to delete and the import says when it is safe to, so an export that
needed it would be an export that stops working -- and for a while it did: the
renderer took a source folder, and after an import you could no longer export
what you had just imported. A stored lap already holds everything the render
needs: its own telemetry slice, the clean clip, the data time of that clip's
first frame (measured at import, not assumed), and the track's map.

The one thing that has to be undone is the clip's container timestamp, which on
a stored lap is *when the import re-encoded it*, not when it was recorded.
Pairing by timestamp therefore finds nothing; the stored `video_t0` is the only
trustworthy answer and becomes the clip's offset.

**It is a button, in both places it is wanted**: on the laps tab it renders the
lap being watched, and on the library tab it renders whatever is selected --
including a whole event, since a folder counts as its laps. Shift-clicking
either one renders the first six seconds instead, which is how anyone checks a
layout change without paying for the whole lap. Progress is per frame, not per
lap, because a single lap is most of a minute.

Finished files land in `output_dir` and are listed with their size and render
speed, each with **show in folder**, which opens the desktop's own file browser
at that file. Printing a path for someone to paste somewhere is not a control.

- **Overlaid video, per lap.** The main case. Exists.
- **Overlaid video for several laps at once**, queued. Exists as a batch render.
- **Re-combining laps into a session** — laps back to back with a join. Deferred;
  noted because storing clean laps makes it possible, whereas storing overlaid
  laps would make the joins look wrong.
- **OPEN**: what else. Data export (a lap's CSV back out) is nearly free and
  probably worth having; a side-by-side comparison video is a bigger job and
  should wait for someone to ask.

---

## 9. What exists, and what this changes

| piece | state |
|---|---|
| CSV v3 loading, channel disambiguation, unit validation | built |
| IMU vehicle-frame calibration, per session | built |
| lap slicing, start/finish recovery, corroborated lap counts | built |
| run detection inside a session | built |
| session shape classification and capability declaration | built |
| track map building, circuit and open course | built |
| distance matching, deltas, splits, ideal lap | built |
| overlay rendering and video export, including camera-split joins | built |
| localhost session view and comparison | built |
| the library: SQLite index, track/day/session/lap/split records | built |
| ingest as one flow: identify → map → trim/split/compress → write | built |
| track identity as shape + direction | built |
| editable names and metadata at every level | built |
| lap deletion, single and pruning upward | built |
| splits stored per track, settable | built |
| **a confirmation step for an ambiguous track match** | **to build** |
| **overlay layout editor and named layouts** | **to build** |
| **split placement by clicking the map** | **to build** |
| bulk deletion by rule, selection separate from removal | built |
| schema migrations, verify and repair, merge and split | built |
| import screen: choose a folder, watch it work, review what came of it | built |
| the OS folder chooser, opened by the server | built |
| video deletable separately from telemetry | built |
| per-lap GPS coverage recorded at ingest | built |
| import from any folder, read in place, nothing copied or deleted | built |
| verification, and the "safe to delete" report it earns | built |
| the session view reading from the library | built |
| exporting an overlaid video from a stored lap, in the app | built |

Measured end to end on the working set: 20 sessions in (19 after the duplicate is
skipped), 12.43 GB of footage, 3 tracks with maps, 3 days, 43 laps, **6.50 GB**
stored, 13 minutes wall clock.

One of the three conflicts below is resolved; the other two remain:

1. ~~**`trackmap.auto_select` picks the map with the nearest origin.**~~
   **Resolved.** `identify.py` measures fit, coverage and direction, and gets 20
   of 20 sessions right while rejecting the same circuit reversed on direction
   alone. `auto_select` is still what the *old* entry points use, so it stays
   until they read from the library.
2. ~~**The session view still reads `Raw Data/`.**~~ **Resolved.** Everything the
   app serves comes from the library. `input_data` and `input_footage` no longer
   exist in the config, and nothing in the tool reads a fixed folder: an import
   points at one, and the laps it produced are what the tool has. The folder-based
   renderers that remain (`main.py`, `tools/preview_server.py`) take a folder as
   an argument rather than assuming one.
3. **Identity comes from filenames.** A session is keyed by its CSV's stem, a
   traversal by `"<stem>#<n>"`, and a map by its filename. Every one of those
   breaks when a file is renamed or a track is given a proper name, so all three
   have to become stable ids with names as separate editable labels. This is the
   change that touches the most code, and it is worth doing before the library
   is written rather than after.

---

## 10. Not yet: richer track geometry

Parked deliberately, recorded so the shape of it is not forgotten.

A track map is currently a centreline: one line, no width. Camera video plus
SLAM could give considerably more — **track width at every point**, and for
autocross **the cone positions**. With those, an optimal line becomes a question
you could actually pose, rather than a guess.

What makes it plausible rather than fantasy is that the hard part of monocular
SLAM is already solved here by other means: **scale and absolute position.** A
monocular reconstruction is scale-free and drifts, but every frame already has a
GPS fix, a metric centreline to anchor to, and a solved vehicle frame from the
IMU. Anchoring a local reconstruction to a known metric path is a much smaller
problem than building a map from video alone.

Honest about the difficulty, in order:

1. **Cone detection** in 1080p60 video — the most tractable. Cones are a
   high-contrast, fixed-size, single-class target, and their ground positions
   follow from the camera geometry plus the known vehicle path.
2. **Track edge and width** on a circuit — harder, because the edge is sometimes
   a kerb, sometimes a painted line, sometimes a change in tarmac colour.
3. **Optimal line** — the biggest claim by far, and not a vision problem. It
   needs a vehicle model: grip, weight transfer, power, and how they trade. What
   the data supports without one is *comparative*: the fastest line **actually
   driven** through each corner, assembled from real laps. That is close to what
   the ideal-lap-from-best-splits figure already does, and it does not require
   pretending to know the car's limits.

None of this starts before ingest, the library and editing work. It is noted as a
direction, not a plan.

---

## 11. Open questions

1. **Hand-placed splits.** Equal-distance divisions to start with. RaceChrono
   exports no split data, so this needs a placement UI on the track map. Worth
   doing after ingest works.
2. **Track file import.** Stubbed until the format is known.
3. **Two similar courses in one parking lot.** The discriminator separates the
   dissimilar pair by 50×; the similar case is unmeasured. Worth revisiting with
   data from a day where two courses shared a lot.
4. **Trimmed padding.** 3 s either side by default. Fixed, or a preference?
5. **Deleting a lap a map was built from.** The map is left alone and the
   provenance records the deletion. Should refining ever be *prompted* after
   deletions, or is it always the user's move?

Resolved since the first draft: trimming stays and is not optional (§4.4);
a walked course is rejected on time, not shape, and a slow run is never rejected
(§4.2); splits cannot be imported and are editable on the track instead (§2, §6);
names and metadata are editable everywhere and never derived from filenames (§6);
laps are deletable (§6); richer track geometry from video is parked (§10).
