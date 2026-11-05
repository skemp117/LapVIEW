/* The session view.
 *
 * One clock. `S.t` is seconds since the run's first sample, and everything --
 * the video's currentTime, the map marker, the trace playheads, the lap
 * highlight, the overlay request -- is derived from it. When footage exists the
 * <video> element owns the clock and we read it; when it does not, a
 * requestAnimationFrame timer owns it. Nothing else is allowed to be a source of
 * time, because two clocks is how a preview ends up disagreeing with an export.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const API = {
  library: () => fetch('/api/library').then(r => r.json()),
  lap:     (id) => fetch('/api/lap/' + encodeURIComponent(id)).then(r => r.json()),
  layout:  (id) => fetch('/api/layout/' + encodeURIComponent(id)).then(r => r.json()),
  trace:   (id, ch, n) => fetch('/api/trace/' + encodeURIComponent(id) +
                                '?ch=' + encodeURIComponent(ch) + '&n=' + n)
                          .then(r => r.json()),
};

/* Everything that changes something -- rename, tag, delete, import, export,
 * reveal, the folder chooser, uploads -- is a POST with this header, and a GET
 * of one of those routes is refused. Any page on any site can make a browser
 * send a GET here (a link, an image) or a plain form POST; none can add a
 * custom header without a CORS preflight, which the server never answers. So
 * the header is how the server knows the request came from this page.
 *
 * Resolves to the reply, with `error` set if the server refused, the same shape
 * the GET routes give; `status` is the HTTP status. Rejects only when there was
 * no answer at all. */
async function postJSON(path, body, binary) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'X-LapVIEW': '1',
               'Content-Type': binary ? 'application/octet-stream'
                                      : 'application/json' },
    body: binary ? body : JSON.stringify(body || {}),
  });
  let d = {};
  try { d = await r.json(); } catch (e) { d = {}; }
  if (!d || typeof d !== 'object' || Array.isArray(d)) d = {};
  if (!r.ok && !d.error) d.error = 'HTTP ' + r.status;
  Object.defineProperty(d, 'status', { value: r.status, enumerable: false });
  return d;
}

/* Where the page is running, from /api/config. `headless` is a server: no
 * folder chooser, no "show in folder", downloads instead. */
const CFG = { headless: false, inbox_dir: '', upload: null };

/* The lap sidebar's own scratch: which event folders are shut, the row order
 * (for shift-click ranges) and the debounce timer for the comparison. */
const LB = { shut: new Set(), rows: [], timer: null, lastTick: null };

const S = {
  lib: null,          // the library tree
  lap: null,          // the lap being played, by id
  track: null,        // the track whose laps the sidebar lists
  model: null,
  layout: null,
  t: 0,
  playing: false,
  traces: {},        // channel -> {t, v, unit}
  plots: [],         // channels currently drawn, top to bottom
  /* Speed reads in mph, with no control to change it. Custom units are
   * their own job: per channel, remembered, and not a single checkbox
   * that means "imperial" to one person and "mph" to another. */
  imperial: true,
  overlayOn: true,
  rate: 1,
  rafTime: null,     // wall-clock anchor when running without video
  rafT: 0,
  /* A seek across a camera split has to load a new file before the video knows
   * where it is. Until it does, `video.currentTime` is 0 and reading the clock
   * from it would snap the playhead back to the start of the new clip -- the map
   * marker and every trace jumping to the clip boundary and then back. `seekTo`
   * holds the requested moment until the element catches up. */
  seekTo: null,
  perf: [],
  /* Bumped on every lap switch. `selectLap` awaits several requests, and a
   * second switch landing mid-flight would otherwise interleave: the model from
   * one lap, the map and traces from another. Each call captures the token and
   * abandons itself if it is no longer the current one. */
  gen: 0,
  mode: 'session',
  /* Comparison state. `cmp` is the last payload from /api/compare, `cmpSel` the
   * traversal keys asked for, `cmpRef` the reference. `cmpD` is the distance the
   * cursor is at, which is the compare view's playhead -- distance, not time,
   * because that is the coordinate the whole comparison lives in. */
  comparable: null,
  cmp: null,
  cmpSel: [],
  cmpRef: null,
  /* Two or more laps selected: the comparison is showing. `classList.toggle`
   * with an undefined second argument flips instead of setting, so this has to
   * exist as a boolean from the start. */
  cmpOn: false,
  cmpD: null,
  cmpGen: 0,
  /* Two counters, not one. `cmpGen` identifies which session/venue the sidebar
   * is for; `cmpRunGen` identifies which comparison request is current. Sharing
   * one meant `runCompare` bumped the counter `loadComparable` was guarding on,
   * so the venue-wide load after the first comparison always aborted itself. */
  cmpRunGen: 0,
  review: null,       // what the last import produced
  src: '',            // the folder being imported from
  impStep: 'choose',  // which of the three import steps is up
  impDone: false,     // the last import's review has been dismissed
  review: null,       // what the last import produced
};

/* Channel colours are assigned per plot slot rather than per channel name: the
 * same colour always means "the top trace", which is what the eye tracks when
 * the playhead moves. */
const COLOURS = ['#4fa3ff', '#3ddc84', '#e8b339', '#c77dff', '#ff8a5c', '#5ce1e6'];

/* ------------------------------------------------------------ formatting */

function fmtTime(s) {
  if (!isFinite(s)) return '0:00.00';
  const neg = s < 0; s = Math.abs(s);
  const m = Math.floor(s / 60), r = s - m * 60;
  return (neg ? '-' : '') + m + ':' + (r < 10 ? '0' : '') + r.toFixed(2);
}

/* Speed is the only channel worth converting: everything else in a RaceChrono
 * export is already in a unit people read directly (rpm, %, psi, g). */
const SPEED_UNITS = { 'm/s': 2.2369363, 'km/h': 0.6213712, 'kph': 0.6213712 };

function convert(value, unit) {
  if (!S.imperial) return { v: value, u: unit };
  const f = SPEED_UNITS[unit];
  if (f === undefined) return { v: value, u: unit };
  return { v: value * f, u: 'mph' };
}

function fmtValue(value, unit, digits) {
  if (value === null || value === undefined || !isFinite(value)) return '--';
  const c = convert(value, unit);
  const d = digits !== undefined ? digits
          : Math.abs(c.v) >= 100 ? 0 : Math.abs(c.v) >= 10 ? 1 : 2;
  return c.v.toFixed(d) + (c.u ? ' ' + c.u : '');
}

/* ------------------------------------------------------------ series lookup */

/* Value of a decimated series at time `t`, by binary search.
 *
 * The series the page holds is the min/max envelope the server sent, not the raw
 * samples, so this is a read of what is drawn rather than of the source. That is
 * the right thing for a readout beside a trace -- the number and the line agree
 * -- and the wrong thing for analysis, which must go back to the run. */
function valueAt(series, t) {
  const ts = series.t;
  if (!ts || ts.length === 0) return null;
  let lo = 0, hi = ts.length - 1;
  if (t <= ts[0]) return series.v[0];
  if (t >= ts[hi]) return series.v[hi];
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) lo = mid; else hi = mid;
  }
  const span = ts[hi] - ts[lo];
  const f = span > 0 ? (t - ts[lo]) / span : 0;
  return series.v[lo] + f * (series.v[hi] - series.v[lo]);
}

function indexAt(ts, t) {
  if (!ts.length) return -1;
  let lo = 0, hi = ts.length - 1;
  if (t <= ts[0]) return 0;
  if (t >= ts[hi]) return hi;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) lo = mid; else hi = mid;
  }
  return (t - ts[lo]) < (ts[hi] - t) ? lo : hi;
}

/* ------------------------------------------------------------ session load */

async function loadLibrary() {
  const d = await API.library();
  S.lib = d;
  /* The header picks a track. Laps are picked in the sidebar, where there is
   * room to group them and to say which have footage -- a flat select of every
   * lap in the library was unreadable by the second event. */
  const pick = $('picker');
  pick.innerHTML = '';
  const withLaps = d.tracks.filter(t => lapsOf(t).length);
  for (const t of withLaps) {
    const o = document.createElement('option');
    o.value = t.id;
    o.textContent = t.name + '  (' + lapsOf(t).length + ' lap(s))';
    pick.appendChild(o);
  }
  const empty = !withLaps.length;
  $('empty').classList.toggle('hidden', !empty || S.mode !== 'session');
  $('app').classList.toggle('hidden', empty || S.mode !== 'session');
  if (empty) {
    fillTrackChoices();
    buildManage();
    status('the library is empty — open the library tab and point the importer '
           + 'at a folder');
    return;
  }
  /* The library tab lists the same tracks, and offers them to import onto. */
  fillTrackChoices();
  buildManage();
  const want = withLaps.find(t => t.id === trackOfLap(S.lap)) ||
               withLaps.find(t => lapsOf(t).some(l => l.has_video)) ||
               withLaps[0];
  pick.value = want.id;
  await selectTrack(want.id);
}

function lapsOf(track) {
  return track.days.flatMap(day => day.laps);
}

function trackOfLap(lapId) {
  if (!lapId || !S.lib) return null;
  const t = S.lib.tracks.find(x => lapsOf(x).some(l => l.id === lapId));
  return t ? t.id : null;
}

/* "session_20260819_191442_amp_full_v3" is 35 characters of which four matter.
 * Keep the time of day, which is what distinguishes the sessions of a day. A
 * name this cannot parse keeps its tail rather than its head. */
function shortStem(name) {
  /* The clock in the middle of the name is four digits from some exports and
   * six from others -- `session_20260905_1251_scca_barber_v3` next to
   * `session_20260822_154240_grip_8-22_lap1_v3`. Taking only the six-digit form
   * left the four-digit ones falling back to their tail, which is the long
   * unhelpful half of the name. */
  const m = /^session_(\d{8})_(\d{2})(\d{2})(\d{2})?_/.exec(name || '');
  if (m) return m[2] + ':' + m[3];
  const n = name || '';
  return n.length > 14 ? '…' + n.slice(-13) : n;
}

function currentTrack() {
  return S.lib ? S.lib.tracks.find(t => t.id === S.track) : null;
}

/* Choosing a track fills the lap list and opens a lap from it. */
async function selectTrack(id) {
  S.track = id;
  buildLapList();
  const laps = lapsOf(currentTrack() || { days: [] });
  if (!laps.length) return;
  /* Prefer a lap with footage: it is the one where every panel has something in
   * it, so a first look is not a page of empty boxes. */
  const want = laps.find(l => l.id === S.lap) ||
               laps.find(l => l.has_video) || laps[0];
  await selectLap(want.id);
}

/* Open one lap in the player. Everything the panels need comes from `/api/lap`,
 * and `S.gen` guards the awaits: a click during a load must not have the older
 * lap's traces and video land on top of the newer one. */
async function selectLap(id) {
  const gen = ++S.gen;
  status('loading…');
  S.lap = id;
  S.model = null; S.layout = null; S.traces = {}; S.plots = [];
  S.t = 0; setPlaying(false);
  const m = await API.lap(id);
  if (gen !== S.gen) return;
  if (m.error) { status(m.error); return; }
  S.model = m;

  $('hint').textContent = [
    m.track.name,
    m.session.name,
    m.rows.toLocaleString() + ' rows',
    m.sample_hz + ' Hz',
    m.gap_m > 20 ? Math.round(m.gap_m) + ' m with no GPS fix' : 'GPS complete',
    m.representative ? '' : 'out-lap or in-lap',
  ].filter(Boolean).join(' · ');

  /* A lap opened from anywhere -- the library tab, a click on a delta trace --
   * brings the sidebar with it. Landing on another track starts a fresh
   * selection, because a comparison never crosses tracks. */
  const tid = trackOfLap(id);
  if (tid && tid !== S.track) {
    S.track = tid;
    $('picker').value = tid;
    S.cmpSel = [id];
    S.cmpRef = id;
    S.cmpOn = false;
  }
  if (!S.cmpSel.length) { S.cmpSel = [id]; S.cmpRef = id; }
  buildLapList();
  buildLapMarks();
  buildChannelBar();
  buildMap();
  $('dur').textContent = fmtTime(m.file_s);

  const defaults = [m.default_channel, 'rpm', 'throttle_position']
    .filter(c => c && m.channels.some(x => x.name === c));
  for (const c of [...new Set(defaults)].slice(0, 3)) {
    await addPlot(c, false);
    if (gen !== S.gen) return;
  }
  layoutPlots();

  const trouble = await setupVideo();
  if (gen !== S.gen) return;
  await loadLayout();
  if (gen !== S.gen) return;
  /* Start where the timed lap starts, not at the first padded frame. */
  seek(m.lap_start);
  status(trouble ||
         (m.video ? '' : 'no video for this lap — running on the data clock'));
  applyMode();
  scheduleCompare();
}

/* Laps grouped by event, in the same language as the library page: folders that
 * open and close, click to select, ctrl-click to add, shift-click for a range.
 *
 * The selection is the mode. One lap is a player; two or more is a comparison,
 * and the comparison is of exactly the laps that are selected. There used to be
 * a separate tab with its own list of the same laps, its own reference picker
 * and its own idea of which lap was current -- two lists over one library that
 * had to be kept in step, which is where two bugs came from.
 */

function buildLapList() {
  const track = currentTrack();
  const box = $('lapList');
  box.innerHTML = '';
  LB.rows = [];
  if (!track) return;
  const laps = lapsOf(track);
  $('lapCount').textContent = laps.length + ' lap(s)';
  const best = Math.min(...laps.filter((l) => l.representative)
                                .map((l) => l.duration), Infinity);
  const refLap = laps.find((l) => l.id === S.cmpRef);

  const groups = new Map();
  for (const day of track.days) {
    if (!day.laps.length) continue;
    const key = day.event || 'no event';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(day);
  }
  for (const [event, days] of groups) {
    const head = document.createElement('div');
    head.className = 'lapgroup';
    const dates = [...new Set(days.map((d) => d.date))].sort();
    const shut = LB.shut.has(event);
    head.innerHTML = '<span class="ev">' + (shut ? '▸ ' : '▾ ') + esc(event) +
      '</span><span class="date">' +
      esc(dates.length > 1 ? dates[0] + ' … ' + dates[dates.length - 1]
                           : dates[0]) + '</span>';
    head.onclick = () => {
      if (LB.shut.has(event)) LB.shut.delete(event);
      else LB.shut.add(event);
      buildLapList();
    };
    box.appendChild(head);
    if (shut) continue;
    for (const day of days) {
      for (const lap of day.laps) {
        box.appendChild(lapRow(day, lap, best, refLap));
      }
    }
  }
}

function lapRow(day, lap, best, refLap) {
  const i = S.cmpSel.indexOf(lap.id);
  const row = document.createElement('div');
  row.className = 'lapitem' +
    (lap.id === S.lap ? ' on' : '') +
    (i >= 0 ? ' cmp' : '') +
    (S.cmpOn && lap.id === S.cmpRef ? ' ref' : '') +
    (lap.representative ? '' : ' edge') +
    (lap.duration === best ? ' best' : '');

  /* A real checkbox, not a swatch that looks like one: the square was already
   * sitting there and reading as something to tick. When the lap is in the
   * comparison the tick takes the colour of its trace, so the list and the plots
   * are still read together. */
  const sw = document.createElement('input');
  sw.type = 'checkbox';
  sw.className = 'sw';
  sw.checked = i >= 0;
  sw.title = i >= 0 ? 'in the comparison' : 'add to the comparison';
  if (i >= 0) sw.style.accentColor = COLOURS[i % COLOURS.length];
  sw.onclick = (e) => {
    e.stopPropagation();          // the row's own click means something else
    if (e.shiftKey && LB.lastTick) lapRange(lap.id);
    else { LB.lastTick = lap.id; lapToggle(lap.id); }
  };

  const nm = document.createElement('span');
  nm.className = 'nm';
  nm.innerHTML = '<span class="who">' +
    esc(shortStem(day.sessions[lap.session_id] || '')) + ' ' + esc(lap.kind) +
    ' ' + lap.number + '</span>' +
    (lap.has_video ? '' : '<span class="flag">no video</span>') +
    (lap.gap_m > 20 ? '<span class="flag">gps</span>' : '');

  /* While comparing, the time column is the gap to the reference: that is the
   * number being looked for, and the absolute times are in the sector table. */
  const t = document.createElement('span');
  const delta = (S.cmpOn && refLap && lap.id !== refLap.id)
    ? lap.duration - refLap.duration : null;
  if (delta === null) {
    t.className = 't';
    t.textContent = lap.label;
  } else {
    t.className = 'd ' + (delta > 0 ? 'up' : 'dn');
    t.textContent = (delta > 0 ? '+' : '−') + Math.abs(delta).toFixed(2);
  }

  row.append(sw, nm, t);
  LB.rows.push(lap.id);
  /* Clicking a lap plays it and makes it the reference. It never changes what is
   * compared -- that is the checkbox's job, and only the checkbox's.
   *
   * Before, a click also collapsed the selection to that one lap, so picking a
   * different lap to watch threw away the comparison you had just built. */
  row.onclick = (e) => { e.preventDefault(); lapAsRef(lap.id); };
  return row;
}

/* ------------------------------------------------------- what is selected */

function lapOnly(id) {
  S.cmpSel = [id];
  S.cmpRef = id;
  afterSelection(id);
}

/* Eight at once, and the ninth is refused rather than silently ignored.
 *
 * Comparing more than a handful of laps is unreadable long before it is slow --
 * eight traces on one plot is already a lot -- and a limit you can see beats a
 * comparison that quietly drops what you picked. */
const MAX_COMPARED = 8;

function lapToggle(id) {
  const i = S.cmpSel.indexOf(id);
  if (i >= 0) {
    S.cmpSel.splice(i, 1);
  } else {
    if (S.cmpSel.length >= MAX_COMPARED) {
      buildLapList();          // put the checkbox back the way it was
      cmpStatus('eight laps at once is the limit — untick one first');
      return;
    }
    S.cmpSel.push(id);
  }
  if (!S.cmpSel.includes(S.cmpRef)) S.cmpRef = S.cmpSel[0] || null;
  afterSelection(S.cmpSel.length === 1 ? S.cmpSel[0] : null);
}

/* The reference is the lap in the player, and everything else is measured
 * against it. A lap being compared against has to be in the comparison, so this
 * ticks it -- unless the eight are already taken, in which case it still plays
 * and the comparison is left exactly as it was. Watching a lap never costs you
 * one you had chosen. */
function lapAsRef(id) {
  if (!S.cmpSel.includes(id)) {
    if (S.cmpOn && S.cmpSel.length >= MAX_COMPARED) {
      cmpStatus('eight laps at once is the limit — untick one to measure '
                + 'against this lap');
      return selectLap(id);
    }
    S.cmpSel.push(id);
  }
  S.cmpRef = id;
  afterSelection(id);
}

function lapRange(id) {
  const from = LB.rows.indexOf(LB.lastTick);
  const to = LB.rows.indexOf(id);
  if (from < 0 || to < 0) return lapOnly(id);
  const [lo, hi] = from <= to ? [from, to] : [to, from];
  let range = LB.rows.slice(lo, hi + 1);
  const over = range.length - MAX_COMPARED;
  if (over > 0) {
    /* Keep the eight nearest the row the range started from, so the anchor is
     * always one of them. */
    range = from <= to ? range.slice(0, MAX_COMPARED)
                       : range.slice(over);
    cmpStatus('eight laps at once is the limit — took the eight nearest');
  }
  S.cmpSel = range;
  LB.lastTick = id;
  if (S.cmpSel.includes(S.cmpRef)) return afterSelection(null);
  S.cmpRef = S.cmpSel[0];
  afterSelection(S.cmpRef);
}

/* The mode follows the count, and the toggle can override it until the count
 * changes again. Nothing to remember to set. */
function afterSelection(playId) {
  S.cmpOn = S.cmpSel.length >= 2;
  if (playId && playId !== S.lap) {
    selectLap(playId);          // redraws the list itself
  } else {
    buildLapList();
    applyMode();
    scheduleCompare();
  }
}

/* Showing the panels, and hiding the ones the other mode owns. The channel
 * chips and their plots are hidden rather than cleared: flipping to a comparison
 * and back must not throw away the traces someone opened. */
function applyMode() {
  const on = !!S.cmpOn;
  $('lapMode').classList.toggle('on', on);
  $('lapMode').textContent = on ? 'comparing' : 'compare';
  $('tracks').classList.toggle('hidden', on);
  $('cmpBand').classList.toggle('hidden', !on);
  $('idealCard').classList.toggle('hidden', !on);
  $('thisLapCard').classList.toggle('hidden', on);
  $('map').classList.toggle('hidden', on);
  $('cmpMap').classList.toggle('hidden', !on);
  $('lapSectorsRow').classList.toggle('hidden', !on);
  $('lapExport').disabled = !(S.model && S.model.video);
  $('mapName').textContent = on ? 'where the time goes' : (
    S.model && S.model.map ? S.model.map.name : 'map');
  $('mapMeta').textContent = on ? 'green = faster than the reference'
    : (S.model && S.model.map
       ? S.model.map.length_m.toFixed(0) + ' m · ' +
         (S.model.map.closed ? 'circuit' : 'open course') : '');
  if (on) sizeCmp();
  else { sizeMap(); sizePlots(); }
}

/* Each lap in a comparison is a traversal to project and difference, and picking
 * three or four in a row is normal. The request waits a second after the last
 * click rather than firing on each one. */
function scheduleCompare() {
  clearTimeout(LB.timer);
  if (!S.cmpOn) { S.cmp = null; cmpStatus(''); return; }
  cmpStatus('comparing…');
  LB.timer = setTimeout(() => runCompare(), 1000);
}

/* ------------------------------------------------------------ video */

async function setupVideo() {
  const v = $('video'), m = S.model;
  const has = !!(m && m.video);
  $('noVideo').classList.toggle('hidden', has);
  v.classList.toggle('hidden', !has);
  if (!has) { v.removeAttribute('src'); v.load(); return; }
  v.playbackRate = S.rate;
  /* Waiting on `loadedmetadata` alone is waiting forever when the video cannot
   * be decoded: no codec for HEVC, a file that went missing, a truncated
   * encode. Everything after this in `selectLap` -- the overlay layout, the
   * seek, the status line -- never ran, so the page sat on "loading..." with a
   * black rectangle and no reason given. The error and a timeout resolve it too.
   */
  return new Promise((resolve) => {
    let settled = false;
    /* Resolves with what went wrong, or nothing. `selectLap` writes the status
     * line last, so saying it here would only be overwritten. */
    const finish = (why) => {
      if (settled) return;
      settled = true;
      resolve(why || '');
    };
    v.addEventListener('loadedmetadata', () => finish(null), { once: true });
    v.addEventListener('error', () => finish(
      'the video for this lap could not be played — the map, the traces and '
      + 'the overlay still work'), { once: true });
    setTimeout(() => finish('the video is slow to load; the rest of the lap is '
                            + 'ready'), 5000);
    v.src = m.video.url;
    v.load();
  });
}

/* Which clip covers this moment. A session can be several files -- the camera
 * chapters every few minutes -- and the gap between them is real: the recorder
 * stops for about 1.5 s. Inside a gap the nearest clip's edge is the honest
 * answer, and the status line says so rather than pretending. */
function hasVideo() {
  return !!(S.model && S.model.video);
}

/* Where the video sits on the lap's clock.
 *
 * A lap's file starts before the timed lap by whatever padding was granted, and
 * the video may start later still: padding is clamped to the footage that
 * exists, so a lap at the end of a clip gets less than it asked for. `offset` is
 * that difference, recorded at import rather than assumed. */
function videoTime(t) {
  const m = S.model;
  if (!m || !m.video) return 0;
  return Math.max(0, Math.min(m.video.duration - 0.05, t - m.video.offset));
}

/* ------------------------------------------------------------ the clock */

function currentT() {
  const m = S.model;
  if (!m) return 0;
  if (m.video) {
    const v = $('video');
    const t = m.video.offset + v.currentTime;
    if (S.seekTo !== null) {
      if (v.readyState >= 1 && Math.abs(t - S.seekTo) < 0.25) S.seekTo = null;
      else return S.seekTo;
    }
    if (v.readyState >= 1) return t;
    return S.rafT;
  }
  return S.rafT;
}

function seek(t) {
  const m = S.model;
  if (!m) return;
  t = Math.max(0, Math.min(m.file_s, t));
  S.t = t; S.rafT = t;
  S.seekTo = m.video ? t : null;
  if (m.video) $('video').currentTime = videoTime(t);
  render();
}

function setPlaying(on) {
  S.playing = on;
  $('play').textContent = on ? 'pause' : 'play';
  const v = $('video');
  if (hasVideo()) { if (on) v.play().catch(() => {}); else v.pause(); }
  S.rafTime = on ? performance.now() : null;
}

function tick(now) {
  requestAnimationFrame(tick);
  if (!S.model) return;
  if (S.playing && !hasVideo()) {
    const dt = (now - (S.rafTime || now)) / 1000;
    S.rafTime = now;
    S.rafT = Math.min(S.model.file_s, S.rafT + dt * S.rate);
    if (S.rafT >= S.model.file_s) setPlaying(false);
  }
  const t = currentT();
  if (S.playing && hasVideo() && $('video').ended) setPlaying(false);
  if (Math.abs(t - S.t) > 1e-4) { S.t = t; render(); }
  meterRate(now);
  paintOverlay();
}

/* ------------------------------------------------------- playback rate
 *
 * Measured, not assumed. A session once played back at 1.497x and the cause
 * turned out to be a browser extension rather than anything here -- which took
 * far longer to establish than it should have, because nothing on the page said
 * what rate it was actually achieving. Both clocks are reported: the lap clock
 * and the video element's own, against wall time.
 */

const RATE = { t0: null, wall0: null, vt0: null, last: 0, text: '' };

function meterRate(nowMs) {
  const v = $('video');
  if (!S.playing) { RATE.t0 = null; return; }
  if (RATE.t0 === null) {
    RATE.t0 = S.t; RATE.wall0 = nowMs; RATE.vt0 = v.currentTime;
    return;
  }
  const wall = (nowMs - RATE.wall0) / 1000;
  if (wall < 2.0) return;                 // too short a window to mean anything
  const clockRate = (S.t - RATE.t0) / wall;
  const videoRate = (v.currentTime - RATE.vt0) / wall;
  let frames = '';
  if (typeof v.getVideoPlaybackQuality === 'function') {
    try {
      const q = v.getVideoPlaybackQuality();
      if (q && q.totalVideoFrames) {
        frames = '  ·  dropped ' + q.droppedVideoFrames + '/' + q.totalVideoFrames;
      }
    } catch (e) { /* not every browser has it */ }
  }
  RATE.text = 'measured  clock ' + clockRate.toFixed(3) + 'x' +
              '  video ' + videoRate.toFixed(3) + 'x' +
              '  (asked ' + S.rate + 'x)' + frames;
  $('rateMeter').textContent = RATE.text;
  // Roll the window forward so the figure stays current rather than averaging
  // the whole session.
  RATE.t0 = S.t; RATE.wall0 = nowMs; RATE.vt0 = v.currentTime;
}

/* ------------------------------------------------------------ overlay */

let overlayBusy = false, overlayWant = null;

async function loadLayout() {
  const gen = S.gen;
  let got;
  try {
    got = await API.layout(S.lap);
  } catch (e) { S.layout = null; return; }
  if (gen !== S.gen) return;
  S.layout = got;
  const c = $('overlay');
  c.width = S.layout.frame[0];
  c.height = S.layout.frame[1];
  placeOverlay();
}

/* The canvas has to sit exactly where the overlay's frame lands on screen, and
 * that rectangle moves whenever the window resizes, so it is measured every
 * frame rather than once. */
function placeOverlay() {
  const c = $('overlay');
  if (!S.layout || !S.overlayOn) { c.style.display = 'none'; return; }
  const box = overlayBox();
  c.style.display = '';
  c.style.left   = box.x + 'px';
  c.style.top    = box.y + 'px';
  c.style.width  = box.w + 'px';
  c.style.height = box.h + 'px';
}

/* The overlay frame letterboxed into the video box -- always, whether or not
 * there is footage.
 *
 * The frame size comes from the lap's own video, so its aspect and the clip's
 * are the same number: letterboxing the frame into the container lands exactly
 * where the browser paints the picture, and lands somewhere sensible when there
 * is no picture at all. Half the laps of an autocross day have no footage and
 * the overlay is the whole view for those.
 *
 * Measuring the `video` element instead was two bugs: hidden outright when the
 * element was absent, and a thumbnail in the middle of a large black box when
 * the element existed at its default size because the clip would not decode. */
function overlayBox() {
  const wrap = $('videoWrap');
  const [fw, fh] = S.layout.frame;
  const scale = Math.min(wrap.clientWidth / fw, wrap.clientHeight / fh);
  const w = fw * scale, h = fh * scale;
  return { x: (wrap.clientWidth - w) / 2, y: (wrap.clientHeight - h) / 2,
           w: w, h: h };
}

async function paintOverlay() {
  placeOverlay();
  if (!S.layout || !S.overlayOn) return;
  const t = S.t;
  if (overlayBusy) { overlayWant = t; return; }
  overlayBusy = true;
  const started = performance.now();
  const gen = S.gen;
  try {
    const buf = await (await fetch('/api/overlay/' + encodeURIComponent(S.lap) +
                                   '?t=' + t.toFixed(4))).arrayBuffer();
    if (gen !== S.gen) return;
    drawOverlay(buf);
    S.perf.push(performance.now() - started);
    if (S.perf.length > 40) S.perf.shift();
    const avg = S.perf.reduce((a, b) => a + b, 0) / S.perf.length;
    $('perf').textContent = S.layout.regions.length + ' region(s) · ' +
      (S.layout.bytes_per_frame / 1e6).toFixed(2) + ' MB/frame · ' +
      avg.toFixed(1) + ' ms' +
      (avg > 0 ? ' · ' + (1000 / avg).toFixed(0) + ' fps ceiling' : '');
  } catch (e) {
    /* A seek cancels in-flight fetches; that is not an error worth reporting. */
  } finally {
    overlayBusy = false;
    if (overlayWant !== null && Math.abs(overlayWant - S.t) < 1e-9) overlayWant = null;
    else if (overlayWant !== null) { overlayWant = null; paintOverlay(); }
  }
}

function drawOverlay(buf) {
  const c = $('overlay'), ctx = c.getContext('2d');
  const dv = new DataView(buf);
  const n = dv.getInt32(0, true);
  let off = 4 + n * 16;
  ctx.clearRect(0, 0, c.width, c.height);
  for (let i = 0; i < n; i++) {
    const b = 4 + i * 16;
    const x = dv.getInt32(b, true), y = dv.getInt32(b + 4, true);
    const w = dv.getInt32(b + 8, true), h = dv.getInt32(b + 12, true);
    const src = new Uint8Array(buf, off, w * h * 4); off += w * h * 4;
    const px = new Uint8ClampedArray(w * h * 4);
    /* cairo hands back premultiplied BGRA; a canvas wants straight RGBA. This is
     * the same unpremultiply ffmpeg does in the export filter graph. */
    for (let p = 0; p < w * h; p++) {
      const a = src[p * 4 + 3], s = a ? 255 / a : 0;
      px[p * 4]     = src[p * 4 + 2] * s;
      px[p * 4 + 1] = src[p * 4 + 1] * s;
      px[p * 4 + 2] = src[p * 4] * s;
      px[p * 4 + 3] = a;
    }
    ctx.putImageData(new ImageData(px, w, h), x, y);
  }
}

/* ------------------------------------------------------------ map */

let mapStatic = null;

function buildMap() {
  const m = S.model, c = $('map');
  const head = $('mapName'), meta = $('mapMeta');
  if (!m.map) {
    head.textContent = 'no map';
    meta.textContent = 'run tools/generate_map.py';
    c.height = 0; mapStatic = null; return;
  }
  head.textContent = m.map.name;
  meta.textContent = m.map.length_m.toFixed(0) + ' m · ' +
    (m.map.closed ? 'circuit' : 'open course');
  sizeMap();
}

function sizeMap() {
  const m = S.model, c = $('map');
  if (!m || !m.map) return;
  const w = c.clientWidth;
  if (!w) return;
  const dpr = window.devicePixelRatio || 1;
  /* points_unit lives in a square box with the short axis centred, so the
   * drawing area is square and the aspect only decides how much of it is used. */
  const h = Math.round(w / Math.max(1, m.map.aspect));
  c.style.height = h + 'px';
  c.width = Math.round(w * dpr);
  c.height = Math.round(h * dpr);
  mapStatic = null;
  drawMap();
}

function mapProject(u, v, w, h, aspect) {
  /* Undo the square-box centring, the same arithmetic MapGauge._to_px does. */
  const sq = aspect >= 1 ? w : h;
  return [u * sq - (sq - w) / 2, v * sq - (sq - h) / 2];
}

function drawMap() {
  const m = S.model, c = $('map');
  if (!m || !m.map || !c.width) return;
  const ctx = c.getContext('2d');
  const w = c.width, h = c.height, ar = m.map.aspect;
  const pad = Math.max(6, w * 0.03);
  const iw = w - 2 * pad, ih = h - 2 * pad;

  if (!mapStatic) {
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.translate(pad, pad);
    // The averaged line: what the map says the track is.
    ctx.lineJoin = ctx.lineCap = 'round';
    ctx.beginPath();
    m.map.points.forEach(([u, v], i) => {
      const [x, y] = mapProject(u, v, iw, ih, ar);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    if (m.map.closed) ctx.closePath();
    ctx.strokeStyle = '#2f3742'; ctx.lineWidth = Math.max(5, w * 0.026); ctx.stroke();
    ctx.strokeStyle = '#5b6675'; ctx.lineWidth = Math.max(2, w * 0.010); ctx.stroke();

    // This run's own line on top, so an off-line lap is visible as such.
    // `track` is the track this lap belongs to; `track_line` is the line this
    // lap actually drove.
    if (m.track_line && m.track_line.u.length) {
      ctx.beginPath();
      for (let i = 0; i < m.track_line.u.length; i++) {
        const [x, y] = mapProject(m.track_line.u[i], m.track_line.v[i],
                                  iw, ih, ar);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      }
      ctx.strokeStyle = 'rgba(79,163,255,.55)';
      ctx.lineWidth = Math.max(1.4, w * 0.006);
      ctx.stroke();
    }

    if (m.map.start_finish) {
      const [x, y] = mapProject(m.map.start_finish[0], m.map.start_finish[1],
                                iw, ih, ar);
      let nx = 0, ny = 1;
      if (m.map.sf_normal) { nx = m.map.sf_normal[0]; ny = m.map.sf_normal[1]; }
      const len = Math.hypot(-ny, nx) || 1;
      const half = Math.max(5, w * 0.032);
      const tx = (-ny / len) * half, ty = (nx / len) * half;
      ctx.beginPath(); ctx.moveTo(x - tx, y - ty); ctx.lineTo(x + tx, y + ty);
      ctx.strokeStyle = '#e0564b'; ctx.lineWidth = Math.max(2, w * 0.009); ctx.stroke();
    }
    for (const [pt, col] of [[m.map.start, '#3ddc84'], [m.map.finish, '#e0564b']]) {
      if (!pt) continue;
      const [x, y] = mapProject(pt[0], pt[1], iw, ih, ar);
      ctx.beginPath(); ctx.arc(x, y, Math.max(3, w * 0.014), 0, 6.284);
      ctx.fillStyle = col; ctx.fill();
    }
    ctx.restore();
    mapStatic = ctx.getImageData(0, 0, w, h);
  } else {
    ctx.putImageData(mapStatic, 0, 0);
  }

  // Marker.
  const tr = m.track_line;
  if (!tr || !tr.t.length) return;
  const i = indexAt(tr.t, S.t);
  const j = Math.min(tr.t.length - 1, i + 1);
  ctx.save();
  ctx.translate(pad, pad);
  const [x, y] = mapProject(tr.u[i], tr.v[i], iw, ih, ar);
  const [x2, y2] = mapProject(tr.u[j], tr.v[j], iw, ih, ar);
  const ang = Math.atan2(y2 - y, x2 - x);
  const r = Math.max(5, w * 0.026);
  ctx.translate(x, y); ctx.rotate(ang);
  ctx.beginPath();
  ctx.moveTo(r, 0); ctx.lineTo(-r * 0.7, r * 0.62); ctx.lineTo(-r * 0.7, -r * 0.62);
  ctx.closePath();
  ctx.fillStyle = '#ff4d3d'; ctx.fill();
  ctx.strokeStyle = 'rgba(0,0,0,.8)'; ctx.lineWidth = Math.max(1, w * 0.004);
  ctx.stroke();
  ctx.restore();
}

/* ------------------------------------------------------------ laps */

/* A lap file is one lap plus its padding, so the "lap list" is now two marks on
 * the timeline: where the timed lap starts and where it ends. */
function buildLapMarks() {
  const m = S.model, box = $('laps');
  if (!m) return;
  const pad0 = m.lap_start, pad1 = m.file_s - m.lap_end;
  $('lapMeta').textContent = m.label;
  box.innerHTML =
    '<div class="lap on"><span class="n">lap</span>' +
    '<span class="bar"><i style="width:100%"></i></span>' +
    '<span class="t">' + m.label + '</span></div>' +
    '<div class="muted" style="padding:4px 5px;font-size:11px">' +
    'timed from ' + fmtTime(m.lap_start) + ' to ' + fmtTime(m.lap_end) +
    ' of this file; ' + pad0.toFixed(1) + ' s before and ' +
    pad1.toFixed(1) + ' s after' +
    (m.gap_m > 20 ? '<br>' + Math.round(m.gap_m) + ' m of the line has no GPS fix'
                  : '') +
    '</div>';
}

function highlightLap() {
  const m = S.model;
  if (!m) return;
  const row = $('laps').firstElementChild;
  if (row) row.classList.toggle('on', S.t >= m.lap_start && S.t <= m.lap_end);
}

/* ------------------------------------------------------------ traces */

function buildChannelBar() {
  const bar = $('chanbar');
  bar.innerHTML = '';
  for (const ch of S.model.channels) {
    const el = document.createElement('span');
    el.className = 'chip';
    el.textContent = ch.name + (ch.unit ? ' (' + ch.unit + ')' : '');
    el.title = ch.min + ' … ' + ch.max + ' ' + ch.unit +
               '  ·  ' + (ch.coverage * 100).toFixed(0) + '% of rows';
    el.dataset.ch = ch.name;
    el.onclick = () => S.plots.includes(ch.name) ? removePlot(ch.name)
                                                 : addPlot(ch.name, true);
    bar.appendChild(el);
  }
  syncChips();
}

function syncChips() {
  for (const el of $('chanbar').children) {
    const i = S.plots.indexOf(el.dataset.ch);
    el.classList.toggle('on', i >= 0);
    el.style.background = i >= 0 ? COLOURS[i % COLOURS.length] : '';
    el.style.borderColor = i >= 0 ? COLOURS[i % COLOURS.length] : '';
  }
}

async function addPlot(ch, redraw) {
  if (S.plots.includes(ch)) return;
  if (!S.traces[ch]) {
    const gen = S.gen;
    const d = await API.trace(S.lap, ch, 1600);
    if (gen !== S.gen) return;
    if (d.error) { status(d.error); return; }
    S.traces[ch] = d;
  }
  S.plots.push(ch);
  if (redraw) { layoutPlots(); }
  syncChips();
}

function removePlot(ch) {
  S.plots = S.plots.filter(c => c !== ch);
  layoutPlots();
  syncChips();
}

function layoutPlots() {
  const box = $('plots');
  box.innerHTML = '';
  for (const ch of S.plots) {
    const wrap = document.createElement('div');
    wrap.className = 'plot';
    const cv = document.createElement('canvas');
    const label = document.createElement('div');
    label.className = 'label';
    const drop = document.createElement('div');
    drop.className = 'drop'; drop.textContent = '×';
    drop.title = 'remove ' + ch;
    drop.onclick = () => removePlot(ch);
    wrap.append(cv, label, drop);
    box.appendChild(wrap);
    wrap.dataset.ch = ch;
    cv.addEventListener('pointerdown', (e) => scrubFrom(cv, e));
    cv.addEventListener('pointermove', (e) => { if (e.buttons & 1) scrubFrom(cv, e); });
  }
  sizePlots();
}

function scrubFrom(cv, e) {
  const r = cv.getBoundingClientRect();
  const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  seek(f * S.model.file_s);
}

/* The stylesheet decides how tall a trace is; this only matches the backing
 * store to whatever it decided. Dividing the leftover height between plots is
 * what made a fourth channel squash the other three instead of making the page
 * longer. */
function sizePlots() {
  const dpr = window.devicePixelRatio || 1;
  for (const wrap of $('plots').children) {
    const cv = wrap.querySelector('canvas');
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) continue;
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  drawPlots();
}

function drawPlots() {
  const m = S.model;
  if (!m) return;
  const kids = $('plots').children;
  for (let k = 0; k < kids.length; k++) {
    const wrap = kids[k], ch = wrap.dataset.ch;
    const cv = wrap.querySelector('canvas'), tr = S.traces[ch];
    if (!cv.width || !tr) continue;
    const ctx = cv.getContext('2d');
    const w = cv.width, h = cv.height;
    const colour = COLOURS[k % COLOURS.length];
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#141821'; ctx.fillRect(0, 0, w, h);

    const meta = m.channels.find(c => c.name === ch) || {};
    /* Scale to p1..p99 with a margin rather than min..max: one bad GPS sample
     * puts `accuracy` at 10 and squashes everything else into a band. Values
     * outside the scale are still drawn, just clipped by the canvas. */
    let lo = meta.p1 !== undefined ? meta.p1 : meta.min;
    let hi = meta.p99 !== undefined ? meta.p99 : meta.max;
    if (!(hi > lo)) { lo = meta.min; hi = meta.max; }
    const pad = (hi - lo) * 0.08 || 1;
    lo -= pad; hi += pad;

    const X = (t) => t / m.file_s * w;
    const Y = (v) => h - (v - lo) / (hi - lo) * h;

    // Where the timed lap starts and ends, so the padding either side reads as
    // padding rather than as part of the lap.
    ctx.strokeStyle = 'rgba(255,255,255,.14)'; ctx.lineWidth = 1;
    for (const t of [m.lap_start, m.lap_end]) {
      ctx.beginPath(); ctx.moveTo(X(t), 0); ctx.lineTo(X(t), h); ctx.stroke();
    }
    ctx.fillStyle = 'rgba(0,0,0,.28)';
    ctx.fillRect(0, 0, X(m.lap_start), h);
    ctx.fillRect(X(m.lap_end), 0, w - X(m.lap_end), h);
    // Zero line when the channel straddles it -- lateral g, longitudinal g.
    if (lo < 0 && hi > 0) {
      ctx.strokeStyle = 'rgba(255,255,255,.14)';
      ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(w, Y(0)); ctx.stroke();
    }

    ctx.beginPath();
    for (let i = 0; i < tr.t.length; i++) {
      const x = X(tr.t[i]), y = Y(tr.v[i]);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.strokeStyle = colour;
    ctx.lineWidth = Math.max(1, 1.2 * (window.devicePixelRatio || 1));
    ctx.stroke();

    // Playhead.
    const px = X(S.t);
    ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
    const val = valueAt(tr, S.t);
    if (val !== null) {
      ctx.beginPath();
      ctx.arc(px, Y(val), Math.max(2.5, 3 * (window.devicePixelRatio || 1)), 0, 6.284);
      ctx.fillStyle = colour; ctx.fill();
    }

    const label = wrap.querySelector('.label');
    label.style.color = colour;
    label.textContent = ch + '  ' + fmtValue(val, tr.unit);
  }
}

/* ------------------------------------------------------------ readout */

const READOUT = [
  ['speed', ['speed_gps']],
  ['rpm', ['rpm']],
  ['throttle', ['throttle_position']],
  ['lateral', ['imu_lateral_acc', 'lateral_acc']],
  ['longitudinal', ['imu_longitudinal_acc', 'longitudinal_acc']],
  ['combined', ['imu_combined_acc', 'combined_acc']],
];

function drawReadout() {
  const m = S.model, box = $('readout');
  if (!m) { box.innerHTML = ''; return; }
  const rows = [];
  for (const [label, prefs] of READOUT) {
    const ch = prefs.find(c => m.channels.some(x => x.name === c));
    if (!ch) continue;
    const tr = S.traces[ch];
    if (!tr) { ensureTrace(ch); rows.push([label, '…']); continue; }
    rows.push([label, fmtValue(valueAt(tr, S.t), tr.unit)]);
  }
  const into = S.t - m.lap_start;
  rows.push(['into the lap',
             (S.t < m.lap_start ? '−' + fmtTime(m.lap_start - S.t)
                                : fmtTime(Math.min(into, m.duration)))]);
  box.innerHTML = rows.map(([k, v]) =>
    '<span class="k">' + k + '</span><span class="v">' + v + '</span>').join('');
}

const pending = new Set();
async function ensureTrace(ch) {
  if (S.traces[ch] || pending.has(ch)) return;
  pending.add(ch);
  const gen = S.gen;
  try {
    const d = await API.trace(S.lap, ch, 1600);
    if (gen === S.gen && !d.error) S.traces[ch] = d;
  } finally { pending.delete(ch); }
}

/* ------------------------------------------------------------ render */

function render() {
  $('clock').textContent = fmtTime(S.t);
  const m = S.model;
  if (m) $('seek').value = Math.round(S.t / Math.max(1e-6, m.file_s) * 1000);
  drawMap();
  drawPlots();
  drawReadout();
  highlightLap();
}

function status(msg) { $('status').textContent = msg || ''; }

/* ------------------------------------------------------------ wiring */

$('picker').onchange = (e) => selectTrack(e.target.value);
$('play').onclick = () => setPlaying(!S.playing);
$('back').onclick = () => seek(S.t - 1);
$('fwd').onclick = () => seek(S.t + 1);
$('seek').oninput = (e) => {
  if (S.model) seek(S.model.file_s * e.target.value / 1000);
};
$('rate').onchange = (e) => {
  S.rate = parseFloat(e.target.value);
  $('video').playbackRate = S.rate;
  S.rafTime = performance.now();
};
$('showOverlay').onchange = (e) => {
  S.overlayOn = e.target.checked;
  placeOverlay();
  paintOverlay();
};

window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT') return;
  const step = e.shiftKey ? 5 : e.altKey ? 0.1 : 1;
  if (e.code === 'Space') { e.preventDefault(); setPlaying(!S.playing); }
  else if (e.code === 'ArrowLeft') { e.preventDefault(); seek(S.t - step); }
  else if (e.code === 'ArrowRight') { e.preventDefault(); seek(S.t + step); }
  else if (e.code === 'Home') seek(S.model ? S.model.lap_start : 0);
  else if (e.code === 'KeyO') { $('showOverlay').click(); }
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  placeOverlay();
  clearTimeout(resizeTimer);
  /* Only what is on screen: a hidden canvas measures zero and would be resized
   * to nothing, then drawn into on the next flip. */
  resizeTimer = setTimeout(() => {
    if (S.cmpOn) sizeCmp();
    else { sizeMap(); sizePlots(); }
  }, 120);
});

/* ============================================================ comparison
 *
 * The compare view has a different playhead from the session view: a *distance*
 * along the centreline, not a time. That is the whole point -- two laps at the
 * same distance are at the same place on the track, and two laps at the same
 * elapsed time are not. Clicking converts back: each traversal knows its own
 * elapsed time at every distance and where it started in its session, so a
 * click on the delta trace can open exactly that corner in the player.
 */

const CMP_API = {
  comparable: (id) => fetch('/api/comparable/' + encodeURIComponent(id))
                      .then(r => r.json()),
  compare: (q) => fetch('/api/compare?' + q).then(r => r.json()),
};

/* The tab is in the URL, so a reload keeps the tab and a link can point at one:
 * `/#import` opens the import screen. */
function setMode(mode) {
  S.mode = mode;
  if (window.location) {
    const want = mode === 'session' ? (S.cmpOn ? '#compare' : '') : '#library';
    if (window.location.hash !== want) {
      try { window.history.replaceState(null, '', want || '#'); } catch (e) {}
    }
  }
  for (const [id, want] of [['modeSession', 'session'],
                            ['modeImport', 'import']]) {
    $(id).classList.toggle('on', mode === want);
  }
  $('app').classList.toggle('hidden', mode !== 'session' || !S.model);
  $('impApp').classList.toggle('hidden', mode !== 'import');
  if (S.playing && mode !== 'session') setPlaying(false);
  if (mode === 'import') return refreshImport();
  applyMode();
  placeOverlay();
  render();
  return Promise.resolve();
}

/* What to show when nothing has been chosen: the three quickest laps at this
 * track. An empty comparison is a blank page and reads as a broken one.
 */
function preselect() {
  const laps = lapsOf(currentTrack() || { days: [] });
  let pool = laps.filter(l => l.representative && !(l.gap_m > 100));
  if (pool.length < 2) pool = laps.filter(l => l.representative);
  if (pool.length < 2) pool = laps;
  const quick = [...pool].sort((a, b) => a.duration - b.duration);
  S.cmpSel = quick.slice(0, 3).map(l => l.id);
  S.cmpRef = quick.length ? quick[0].id : null;
  S.cmpOn = S.cmpSel.length >= 2;
}

async function runCompare() {
  if (!S.cmpOn || S.cmpSel.length < 2) {
    S.cmp = null; drawCmp();
    cmpStatus(S.cmpOn ? 'ctrl-click another lap to compare against' : '');
    return;
  }
  const gen = ++S.cmpRunGen;
  cmpStatus('comparing…');
  const q = new URLSearchParams();
  q.set('laps', S.cmpSel.join(','));
  if (S.cmpRef) q.set('ref', S.cmpRef);
  q.set('sectors', $('cmpSectors').value || '8');
  q.set('points', '900');
  const d = await CMP_API.compare(q.toString());
  if (gen !== S.cmpRunGen) return;
  if (d.error) { cmpStatus(d.error); S.cmp = null; drawCmp(); return; }
  S.cmp = d;
  S.cmpRef = d.ref;
  buildLapList();
  buildSectorTable();
  buildIdeal();
  $('cmpDeltaMeta').textContent = d.length_m.toFixed(0) + ' m of centreline' +
    (d.uncompared_m >= 1 ? '  ·  ' + d.uncompared_m.toFixed(0) +
     ' m outside the overlap' : '');
  drawCmp();
  cmpStatus(d.note || '');
}

/* ------------------------------------------------------------ drawing */

/* `h` is only passed for the map, whose height follows the track's shape. The
 * plots take theirs from the stylesheet. */
function cmpCanvasSize(id, h) {
  const cv = $(id), dpr = window.devicePixelRatio || 1;
  if (h) cv.style.height = h + 'px';
  const w = cv.clientWidth, hh = h || cv.clientHeight;
  cv.width = Math.max(1, Math.round((w || 1) * dpr));
  cv.height = Math.max(1, Math.round((hh || 1) * dpr));
  return cv;
}

function sizeCmp() {
  cmpCanvasSize('cmpDelta');
  cmpCanvasSize('cmpSpeed');
  const cv = $('cmpMap');
  const w = cv.clientWidth || 1;
  const ar = (S.model && S.model.map) ? Math.max(1, S.model.map.aspect) : 1;
  cmpCanvasSize('cmpMap', Math.round(w / ar));
  drawCmp();
}

function drawCmp() {
  drawDeltaPlot();
  drawSpeedPlot();
  drawCmpMap();
}

function cmpSeries() {
  if (!S.cmp || !S.cmp.traversals || !S.cmp.grid.length) return null;
  const g = S.cmp.grid;
  const rows = S.cmpSel
    .map((k, i) => {
      const r = S.cmp.traversals.find(t => t.key === k);
      return r ? { r, colour: COLOURS[i % COLOURS.length] } : null;
    })
    .filter(Boolean);
  return rows.length ? { g, rows } : null;
}

/* Is this distance inside a stretch where the traversal had no fixes?
 *
 * At AMP fourteen of twenty-seven laps have a 250-300 m hole in the same place,
 * where the GPS accuracy blows out under trees. Interpolating across it is
 * unavoidable; drawing it at full strength as though it were measured is not.
 */
function inGap(r, d) {
  const g = r.gaps;
  if (!g || !g.length) return false;
  for (const [a, b] of g) if (d >= a && d <= b) return true;
  return false;
}

/* Draw a series as solid where it was measured and faint where it was filled in,
 * by breaking the path at every gap boundary. */
function strokeWithGaps(ctx, g, values, r, X, Y, colour, width) {
  let i = 0;
  while (i < g.length) {
    const gapped = inGap(r, g[i]);
    let j = i;
    while (j + 1 < g.length && inGap(r, g[j + 1]) === gapped) j++;
    ctx.beginPath();
    for (let k = i; k <= j; k++) {
      const x = X(g[k]), y = Y(values[k]);
      k === i ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    // Join the segments up so the line does not visibly break.
    if (j + 1 < g.length) ctx.lineTo(X(g[j + 1]), Y(values[j + 1]));
    ctx.strokeStyle = colour;
    ctx.lineWidth = width;
    ctx.globalAlpha = gapped ? 0.28 : 1;
    if (gapped) ctx.setLineDash([4, 4]); else ctx.setLineDash([]);
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
    i = j + 1;
  }
}

function drawDeltaPlot() {
  const cv = $('cmpDelta'), ctx = cv.getContext('2d');
  const w = cv.width, h = cv.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#141821'; ctx.fillRect(0, 0, w, h);
  const S2 = cmpSeries();
  if (!S2) { $('cmpDeltaLabel').textContent = ''; return; }
  const { g, rows } = S2;

  let lo = 0, hi = 0;
  for (const { r } of rows) for (const v of r.delta) {
    if (v < lo) lo = v; if (v > hi) hi = v;
  }
  const pad = Math.max(0.05, (hi - lo) * 0.12);
  lo -= pad; hi += pad;
  const X = (d) => d / g[g.length - 1] * w;
  const Y = (v) => h - (v - lo) / (hi - lo) * h;

  cmpGrid(ctx, w, h, g, X);
  // Zero is the reference: above it is time lost, below it is time gained.
  ctx.strokeStyle = 'rgba(255,255,255,.28)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(w, Y(0)); ctx.stroke();

  for (const { r, colour } of rows) {
    if (r.is_ref) continue;
    strokeWithGaps(ctx, g, r.delta, r, X, Y, colour,
                   Math.max(1.4, 1.6 * (window.devicePixelRatio || 1)));
  }
  cmpPlayhead(ctx, w, h, g, X);

  const parts = ['0 = ' + refLabel()];
  if (S.cmpD !== null) {
    for (const { r } of rows) {
      if (r.is_ref) continue;
      const j = indexAt(g, S.cmpD);
      parts.push(tvLabel(r) + ' ' + (r.delta[j] >= 0 ? '+' : '') +
                 r.delta[j].toFixed(2) + 's');
    }
    parts.push(Math.round(S.cmpD) + ' m');
  } else {
    parts.push('lost/gained in seconds; flat = equal');
  }
  $('cmpDeltaLabel').textContent = parts.join('   ');
}

function drawSpeedPlot() {
  const cv = $('cmpSpeed'), ctx = cv.getContext('2d');
  const w = cv.width, h = cv.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#141821'; ctx.fillRect(0, 0, w, h);
  const S2 = cmpSeries();
  if (!S2 || !S.cmp.channels.includes('speed_gps')) {
    $('cmpSpeedLabel').textContent = ''; return;
  }
  const { g, rows } = S2;
  let lo = Infinity, hi = -Infinity;
  for (const { r } of rows) for (const v of (r.speed_gps || [])) {
    if (v < lo) lo = v; if (v > hi) hi = v;
  }
  if (!(hi > lo)) { $('cmpSpeedLabel').textContent = ''; return; }
  const padv = (hi - lo) * 0.08; lo -= padv; hi += padv;
  const X = (d) => d / g[g.length - 1] * w;
  const Y = (v) => h - (v - lo) / (hi - lo) * h;

  cmpGrid(ctx, w, h, g, X);
  for (const { r, colour } of rows) {
    const v = r.speed_gps;
    if (!v) continue;
    strokeWithGaps(ctx, g, v, r, X, Y, colour,
                   Math.max(1.2, 1.4 * (window.devicePixelRatio || 1)));
  }
  cmpPlayhead(ctx, w, h, g, X);

  const unit = 'm/s';
  if (S.cmpD !== null) {
    const j = indexAt(g, S.cmpD);
    $('cmpSpeedLabel').textContent = 'speed  ' + rows.map(
      ({ r }) => tvLabel(r) + ' ' + fmtValue(r.speed_gps ? r.speed_gps[j] : null, unit)
    ).join('   ');
  } else {
    $('cmpSpeedLabel').textContent = 'speed by distance';
  }
}

function cmpGrid(ctx, w, h, g, X) {
  if (!S.cmp || !S.cmp.sectors || !S.cmp.sectors.edges) return;
  ctx.strokeStyle = 'rgba(255,255,255,.08)'; ctx.lineWidth = 1;
  for (const e of S.cmp.sectors.edges) {
    ctx.beginPath(); ctx.moveTo(X(e), 0); ctx.lineTo(X(e), h); ctx.stroke();
  }
}

function cmpPlayhead(ctx, w, h, g, X) {
  if (S.cmpD === null) return;
  ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(X(S.cmpD), 0); ctx.lineTo(X(S.cmpD), h);
  ctx.stroke();
}

function refLabel() {
  const r = S.cmp && S.cmp.traversals.find(t => t.is_ref);
  return r ? tvLabel(r) + ' ' + r.time_label : 'reference';
}

function tvLabel(r) {
  /* Always session-qualified. A legend reading "lap 6" beside "19:14 lap 5"
   * invites the reading that the first one is somehow not from a session. */
  return shortStem(r.session) + ' ' + r.label;
}

/* The map coloured by where time changed hands. This is the view that turns a
 * delta trace into somewhere on the circuit: red where the selected lap is
 * losing, green where it is gaining, and the width carries the magnitude so a
 * glance finds the corner without reading a scale. */
function drawCmpMap() {
  const cv = $('cmpMap'), ctx = cv.getContext('2d');
  const w = cv.width, h = cv.height;
  ctx.clearRect(0, 0, w, h);
  const m = S.model && S.model.map;
  const S2 = cmpSeries();
  if (!m || !S2) return;
  const { g, rows } = S2;
  const shown = rows.find(x => !x.r.is_ref);
  if (!shown) return;
  const rate = shown.r.rate;

  const pad = Math.max(6, w * 0.04);
  const iw = w - 2 * pad, ih = h - 2 * pad;
  const ar = m.aspect;
  const pts = m.points;

  // Distance along the drawn polyline, in metres, so a centreline distance can
  // be turned into a point on it. Same arithmetic the server does in
  // compare.centreline: unit arc length times the metric span.
  const cum = [0];
  for (let i = 1; i < pts.length; i++) {
    cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0],
                                     pts[i][1] - pts[i - 1][1]) * m.span_m);
  }
  const offset = S.cmp.offset_m || 0;

  ctx.save();
  ctx.translate(pad, pad);
  // The line itself, dark, so the coloured part reads as an overlay on a track.
  ctx.beginPath();
  pts.forEach(([u, v], i) => {
    const [x, y] = mapProject(u, v, iw, ih, ar);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  if (m.closed) ctx.closePath();
  ctx.strokeStyle = '#2b323c';
  ctx.lineWidth = Math.max(7, w * 0.036);
  ctx.lineJoin = ctx.lineCap = 'round';
  ctx.stroke();

  let worst = 0;
  for (const v of rate) worst = Math.max(worst, Math.abs(v));
  worst = worst || 1;

  // One short segment per grid step, coloured by the local rate.
  let k = 0;
  for (let i = 0; i + 1 < g.length; i++) {
    const d0 = g[i] + offset, d1 = g[i + 1] + offset;
    const a = pointAtDistance(pts, cum, d0, iw, ih, ar);
    const b = pointAtDistance(pts, cum, d1, iw, ih, ar);
    if (!a || !b) continue;
    // Never colour a stretch the GPS did not see: the rate there is the slope
    // of a straight line drawn between two fixes, which is not a fact about
    // the driving.
    if (inGap(shown.r, g[i])) continue;
    const v = rate[i];
    const f = Math.min(1, Math.abs(v) / worst);
    if (f < 0.06) continue;               // near-equal: leave the track dark
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
    ctx.strokeStyle = v > 0
      ? 'rgba(224,86,75,' + (0.25 + 0.75 * f).toFixed(3) + ')'
      : 'rgba(61,220,132,' + (0.25 + 0.75 * f).toFixed(3) + ')';
    ctx.lineWidth = Math.max(2, w * 0.012 + w * 0.026 * f);
    ctx.stroke();
    k++;
  }

  if (S.cmpD !== null) {
    const p = pointAtDistance(pts, cum, S.cmpD + offset, iw, ih, ar);
    if (p) {
      ctx.beginPath();
      ctx.arc(p[0], p[1], Math.max(4, w * 0.016), 0, 6.284);
      ctx.fillStyle = '#ffffff'; ctx.fill();
      ctx.strokeStyle = '#000'; ctx.lineWidth = 1.5; ctx.stroke();
    }
  }
  ctx.restore();
}

function pointAtDistance(pts, cum, d, iw, ih, ar) {
  const total = cum[cum.length - 1];
  if (!(total > 0)) return null;
  let x = d;
  if (x < 0) x += total;
  if (x > total) x -= total;
  if (x < 0 || x > total) return null;
  let lo = 0, hi = cum.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= x) lo = mid; else hi = mid;
  }
  const span = cum[hi] - cum[lo];
  const f = span > 0 ? (x - cum[lo]) / span : 0;
  const u = pts[lo][0] + f * (pts[hi][0] - pts[lo][0]);
  const v = pts[lo][1] + f * (pts[hi][1] - pts[lo][1]);
  return mapProject(u, v, iw, ih, ar);
}

/* ------------------------------------------------------------ tables */

function buildSectorTable() {
  const box = $('cmpSectors2');
  box.innerHTML = '';
  const sec = S.cmp && S.cmp.sectors;
  if (!sec || !sec.rows.length) return;
  const n = sec.best.length;
  const byKey = {};
  for (const r of S.cmp.traversals) byKey[r.key] = r;

  const t = document.createElement('table');
  t.className = 'sec';
  let head = '<tr><th class="nm">traversal</th><th>time</th>';
  for (let j = 0; j < n; j++) {
    head += '<th title="' + Math.round(sec.edges[j]) + '-' +
            Math.round(sec.edges[j + 1]) + ' m">S' + (j + 1) + '</th>';
  }
  t.innerHTML = head + '</tr>';

  const order = S.cmpSel
    .map(k => sec.rows.find(r => r.key === k))
    .filter(Boolean);
  for (const r of order) {
    const meta = byKey[r.key] || {};
    const tr = document.createElement('tr');
    let html = '<td class="nm">' + (meta.label ? tvLabel(meta) : r.key) +
               '</td><td>' + (meta.time_label || '') + '</td>';
    for (let j = 0; j < n; j++) {
      const cls = r.best_sectors.includes(j) ? ' class="best"'
                : (r.vs_ref && r.vs_ref[j] > 0.05) ? ' class="up"'
                : (r.vs_ref && r.vs_ref[j] < -0.05) ? ' class="dn"' : '';
      html += '<td' + cls + '>' + r.times[j].toFixed(2) + '</td>';
    }
    tr.innerHTML = html;
    tr.style.cursor = 'pointer';
    tr.onclick = () => { S.cmpRef = r.key; runCompare(); };
    tr.title = 'make this the reference';
    t.appendChild(tr);
  }

  const tr = document.createElement('tr');
  tr.className = 'ideal';
  let html = '<td class="nm">best sector</td><td>' +
             fmtTime(sec.ideal.total) + '</td>';
  for (let j = 0; j < n; j++) html += '<td>' + sec.best[j].toFixed(2) + '</td>';
  tr.innerHTML = html;
  t.appendChild(tr);
  box.appendChild(t);

  $('cmpSecMeta').textContent = n + ' equal-distance sector(s) of ' +
    Math.round(S.cmp.length_m / n) + ' m';
}

function buildIdeal() {
  const sec = S.cmp && S.cmp.sectors;
  const box = $('cmpIdeal');
  if (!sec || !sec.rows.length) { box.innerHTML = ''; return; }
  const fastest = Math.min(...S.cmp.traversals.map(r => r.duration));
  const rows = [
    ['ideal lap', fmtTime(sec.ideal.total)],
    ['fastest real', fmtTime(fastest)],
    ['on the table', (fastest - sec.ideal.total).toFixed(2) + ' s'],
  ];
  box.innerHTML = rows.map(([k, v]) =>
    '<span class="k">' + k + '</span><span class="v">' + v + '</span>').join('');
  $('cmpIdealMeta').textContent = 'from ' + sec.ideal.from_traversals +
    ' traversal(s)';
}

/* --------------------------------------------------- pointer handling */

function cmpPointer(e, click) {
  const cv = e.currentTarget;
  const r = cv.getBoundingClientRect();
  const g = S.cmp && S.cmp.grid;
  if (!g || !g.length) return;
  const f = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
  S.cmpD = f * g[g.length - 1];
  drawCmp();
  if (click) openAtDistance(S.cmpD);
}

/* Open the player at this distance in the traversal being compared.
 *
 * This is the payoff of keeping one distance coordinate: "I lost 0.8 s between
 * 187 and 286 m" becomes the video of that corner, in the lap it happened in,
 * without anyone converting anything by hand.
 */
async function openAtDistance(d) {
  const S2 = cmpSeries();
  if (!S2) return;
  const { g, rows } = S2;
  const pick = rows.find(x => !x.r.is_ref) || rows[0];
  const r = pick.r;
  const j = indexAt(g, d);
  /* The traversal's clock starts at the timed lap; the player's starts at the
   * first row of the lap's file, padding included. */
  const t = (r.lap_start || 0) + r.t[j];
  if (r.key !== S.lap) {
    await selectLap(r.key);
  }
  setMode('session');
  seek(t);
  status('opened ' + tvLabel(r) + ' at ' + Math.round(d) + ' m along the line');
}

function cmpStatus(msg) { $('cmpStatus').textContent = msg || ''; }

for (const id of ['cmpDelta', 'cmpSpeed']) {
  $(id).addEventListener('pointermove', (e) => cmpPointer(e, false));
  $(id).addEventListener('pointerdown', (e) => cmpPointer(e, true));
  $(id).addEventListener('pointerleave', () => { S.cmpD = null; drawCmp(); });
}
$('modeSession').onclick = () => setMode('session');
$('cmpSectors').onchange = () => runCompare();
/* The mode toggle only forces what the selection has not already decided: with
 * one lap it turns the comparison on against the quickest few, and with several
 * it drops back to watching the reference. */
$('lapMode').onclick = () => {
  if (!S.cmpOn && S.cmpSel.length < 2) preselect();
  else S.cmpOn = !S.cmpOn;
  if (S.cmpOn && S.cmpSel.length < 2) preselect();
  buildLapList();
  applyMode();
  scheduleCompare();
};
$('lapBest').onclick = () => {
  preselect();
  buildLapList();
  applyMode();
  scheduleCompare();
};
$('lapClear').onclick = () => {
  S.cmpSel = S.lap ? [S.lap] : [];
  S.cmpRef = S.lap;
  S.cmpOn = false;
  LB.lastTick = null;
  buildLapList();
  applyMode();
  scheduleCompare();
};


/* ============================================================== exporting
 *
 * Rendering an overlaid video, from the library rather than from the folder the
 * footage arrived in. That folder is the user's to delete -- the import says
 * when it is safe to -- so an export that needed it would be an export that
 * stops working. A stored lap already holds everything: its own telemetry
 * slice, the clean clip, the data time of the clip's first frame measured at
 * import, and the track's map.
 */

async function exportLaps(ids, opts) {
  const list = [...new Set(ids)].filter(Boolean);
  if (!list.length) return;
  let offer;
  try {
    offer = await fetch('/api/export?laps=' + list.join(','))
      .then((r) => r.json());
  } catch (e) { return status('' + e); }
  if (offer.error) return status(offer.error);
  const usable = offer.laps.filter((l) => l.has_video);
  if (!usable.length) {
    return status('none of those laps has footage in the library, so there is '
                  + 'nothing to overlay');
  }
  const preview = (opts || {}).preview;
  const mins = Math.max(1, Math.round(offer.seconds / 60));
  if (!confirm('Export ' + usable.length + ' lap(s) with the overlay burned in?'
               + (preview ? '\n\nFirst ' + preview + ' s of each, as a check.'
                          : '\n\n' + fmtTime(offer.seconds) + ' of video — '
                            + 'roughly ' + mins + ' minute(s) of rendering.')
               + (CFG.headless
                 ? '\n\nEach file gets a download link under exports in the '
                   + 'library tab when it is done.'
                 : '\n\nInto ' + offer.out_dir))) return;
  try {
    const d = await postJSON('/api/export/run', {
      laps: usable.map((l) => l.id), preview: preview || null,
      overwrite: !!(opts || {}).overwrite });
    if (d.error) return status(d.error);
  } catch (e) { return status('' + e); }
  pollExport();
}

async function pollExport() {
  let job;
  try {
    job = await fetch('/api/export/progress').then((r) => r.json());
  } catch (e) {
    setTimeout(pollExport, 2000);
    return;
  }
  drawExports(job);
  if (job.state === 'running') {
    setTimeout(pollExport, 700);
    return;
  }
  if (job.state === 'failed') status('export failed: ' + job.error);
  else if (job.state === 'done') {
    const ok = (job.files || []).filter((f) => !f.error);
    status('exported ' + ok.length + ' file(s) into ' + job.out_dir);
  }
}

function drawExports(job) {
  const running = job.state === 'running';
  $('expBar').classList.toggle('hidden', !running);
  if (running) {
    $('expFill').style.width =
      Math.max(2, Math.round(100 * (job.fraction || 0))) + '%';
    const bits = [(job.done + 1) + ' of ' + job.total, job.lap];
    if (job.eta_s) bits.push('about ' + Math.round(job.eta_s) + 's left');
    $('expNow').textContent = bits.filter(Boolean).join('   ·   ');
  } else {
    $('expNow').textContent = job.state === 'failed' ? job.error : '';
  }
  const files = job.files || [];
  $('expMeta').textContent = files.length
    ? files.length + ' file(s)  ·  ' + (job.out_dir || '')
    : (job.out_dir || '');
  const box = $('expList');
  box.innerHTML = '';
  for (const f of files) {
    const row = document.createElement('div');
    row.className = 'expfile' + (f.error ? ' bad' : '');
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = f.name + (f.error ? ' — ' + f.error
                                       : (f.skipped ? ' — ' + f.skipped : ''));
    const sz = document.createElement('span');
    sz.className = 'sz';
    sz.textContent = f.bytes ? human(f.bytes) : '';
    const sp = document.createElement('span');
    sp.className = 'sp';
    sp.textContent = f.speed ? f.speed.toFixed(2) + '× realtime' : '';
    row.append(nm, sz, sp);
    row.appendChild(f.out && !f.error ? exportActions(f)
                                      : document.createElement('span'));
    box.appendChild(row);
  }
  if (!running) refreshExports(files);
}

/* Download everywhere; "show in folder" only where there is a desktop to show
 * it on. On a server the file is on someone else's disk. */
function exportActions(f) {
  const acts = document.createElement('span');
  acts.className = 'acts';
  if (f.url) {
    const a = document.createElement('a');
    a.className = 'act';
    a.href = f.url;
    a.setAttribute('download', f.name || '');
    /* A browser saves it and stays put. Run full screen from an iPhone's home
     * screen there is no back button, and if that WebKit ignores `download`
     * the file opens in a sheet with a Done button rather than replacing the
     * app. */
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'download';
    acts.appendChild(a);
  }
  if (!CFG.headless) {
    const b = document.createElement('button');
    b.className = 'act';
    b.textContent = 'show in folder';
    b.title = 'open the desktop file browser at this file';
    b.onclick = () => reveal(f.out);
    acts.appendChild(b);
  }
  return acts;
}

/* Everything already in the export folder, so a file rendered yesterday -- or
 * from another device -- can still be fetched. */
async function refreshExports(current) {
  let d;
  try {
    d = await fetch('/api/exports').then((r) => r.json());
  } catch (e) { return; }
  const shown = new Set((current || []).map((f) => f.out));
  const rows = (d.files || []).filter((f) => !shown.has(f.out));
  const box = $('expSaved');
  box.innerHTML = '';
  if (!rows.length) return;
  const head = document.createElement('div');
  head.className = 'opthead';
  head.textContent = 'in the export folder';
  box.appendChild(head);
  for (const f of rows) {
    const row = document.createElement('div');
    row.className = 'expfile saved';
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = f.name;
    const sz = document.createElement('span');
    sz.className = 'sz';
    sz.textContent = human(f.bytes);
    row.append(nm, sz, exportActions(f));
    box.appendChild(row);
  }
}

async function reveal(path) {
  try {
    const d = await postJSON('/api/reveal', { path });
    if (d.error) status(d.error);
  } catch (e) { status('' + e); }
}

/* The lap being watched, from the laps tab. */
$('lapExport').onclick = (e) => exportLaps([S.cmpRef || S.lap],
                                           { preview: e.shiftKey ? 6 : null });
/* Whatever is selected, from the library tab. Folders count as their laps. */
$('mgExport').onclick = (e) => exportLaps(
  MG.sel.flatMap(mgLapsIn).map((l) => l.id), { preview: e.shiftKey ? 6 : null });

/* ========================================================== managing data
 *
 * Track and event are folders; laps are rows under them. Click to select,
 * ctrl-click to add, shift-click for a range, double-click to open. The tools
 * are a fixed row at the top, greyed out when the selection gives them nothing
 * to do -- buttons that appear and disappear make you hunt for them.
 *
 * A file explorer, in other words. Not because it is clever but because everyone
 * already knows it.
 *
 * The listing is one CSS grid with `display: contents` rows. Each row used to be
 * its own grid, which meant a `1fr` name column resolved against that row's own
 * text and the times landed at a different x on every line.
 */

const MG = {
  sel: [],                 // [{kind, id}], in the order they were clicked
  shut: new Set(),         // folders collapsed, by id
  rows: [],                // what is on screen, for shift-click ranges
};

function mgKey(item) { return item.kind + ':' + item.id; }

function mgSelected(kind, id) {
  return MG.sel.some((x) => x.kind === kind && x.id === id);
}

async function mgCall(route, params) {
  const d = await postJSON(route, params);
  if (d.error) throw new Error(d.error);
  return d;
}

/* Every action ends the same way: do it, re-read the library, redraw. Nothing is
 * patched in place, so the screen cannot drift from the database and a delete
 * that failed does not look like it worked. */
async function mgDo(what, calls) {
  impStatus(what + '…');
  try {
    let freed = 0;
    for (const [route, params] of calls) {
      const d = await mgCall(route, params);
      freed += d.freed_bytes || 0;
    }
    await loadLibrary();
    buildManage();
    impStatus(what + ' — done' + (freed ? ', ' + human(freed) + ' freed' : ''));
  } catch (e) {
    impStatus(what + ' failed: ' + e.message);
  }
}

/* ------------------------------------------------------------- the listing */

function buildManage() {
  const box = $('mgTree');
  const lib = S.lib;
  /* Keep the header row, replace everything under it. */
  [...box.children].slice(1).forEach((el) => el.remove());
  MG.rows = [];
  if (!lib) return;

  const sum = lib.summary || {};
  $('mgMeta').textContent = [
    (sum.tracks || 0) + ' track(s)',
    (sum.laps || 0) + ' lap(s)',
    human(sum.bytes || 0),
    lib.library_dir,
  ].join('  ·  ');

  if (!lib.tracks.length) {
    const row = document.createElement('div');
    row.className = 'mgrow folder';
    row.innerHTML = '<span class="c-name muted">Nothing imported yet. Point ' +
      'the importer below at a folder of RaceChrono CSVs and footage.</span>';
    box.appendChild(row);
    mgSyncBar();
    return;
  }

  for (const t of lib.tracks) {
    const laps = lapsOf(t);
    box.appendChild(mgFolder({
      kind: 'track', id: t.id, cls: 'track',
      name: t.name,
      meta: [t.closed ? 'circuit' : 'open course',
             t.has_map ? Math.round(t.length_m) + ' m' : 'no map',
             laps.length + ' lap(s)',
             human(laps.reduce((a, l) => a + l.bytes, 0)),
             t.notes ? '“' + t.notes + '”' : ''].filter(Boolean).join('  ·  '),
    }));
    if (MG.shut.has(t.id)) continue;

    /* Days grouped under their event, because that is how anyone refers to
     * them -- "the second event", not "the twenty-second of August". */
    const byEvent = new Map();
    for (const day of t.days) {
      const key = day.event || '';
      if (!byEvent.has(key)) byEvent.set(key, []);
      byEvent.get(key).push(day);
    }
    for (const [event, days] of byEvent) {
      /* One folder per day, so renaming the event and deleting a day both have
       * something to act on. Days of one event sit next to each other. */
      for (const day of days) {
        box.appendChild(mgFolder({
          kind: 'day', id: day.id, cls: 'event',
          name: event || 'no event',
          faint: !event,
          meta: [day.date, day.laps.length + ' lap(s)',
                 day.notes ? '“' + day.notes + '”' : ''].filter(Boolean)
            .join('  ·  '),
        }));
        if (MG.shut.has(day.id)) continue;
        const best = Math.min(...day.laps.filter((l) => l.representative)
                                         .map((l) => l.duration), Infinity);
        for (const lap of day.laps) box.appendChild(mgLapRow(day, lap, best));
      }
    }
  }
  mgSyncBar();
}

function mgFolder(spec) {
  const row = document.createElement('div');
  row.className = 'mgrow folder ' + spec.cls +
                  (mgSelected(spec.kind, spec.id) ? ' sel' : '');
  const shut = MG.shut.has(spec.id);
  const name = document.createElement('span');
  name.className = 'c-name';
  const twist = document.createElement('span');
  twist.className = 'twist';
  twist.textContent = shut ? '▸' : '▾';
  twist.onclick = (e) => { e.stopPropagation(); mgToggle(spec.id); };
  const label = document.createElement('span');
  label.textContent = spec.name;
  if (spec.faint) label.className = 'muted';
  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = spec.meta;
  name.append(twist, label, meta);
  row.appendChild(name);
  mgClickable(row, { kind: spec.kind, id: spec.id },
              () => mgToggle(spec.id));
  return row;
}

function mgLapRow(day, lap, best) {
  const row = document.createElement('div');
  row.className = 'mgrow lap' + (mgSelected('lap', lap.id) ? ' sel' : '') +
                  (lap.representative ? '' : ' edge') +
                  (lap.duration === best ? ' best' : '');
  const name = document.createElement('span');
  name.className = 'c-name';
  name.textContent = lap.name ||
    (shortStem(day.sessions[lap.session_id] || '') + ' ' + lap.kind + ' ' +
     lap.number);
  for (const [test, text] of [[!lap.representative, 'out/in'],
                              [lap.gap_m > 20, Math.round(lap.gap_m) + ' m gps']]) {
    if (!test) continue;
    const f = document.createElement('span');
    f.className = 'flag';
    f.textContent = text;
    name.appendChild(f);
  }
  const time = document.createElement('span');
  time.className = 'c-time';
  time.textContent = lap.label;
  const vid = document.createElement('span');
  vid.className = 'c-vid' + (lap.has_video ? '' : ' none');
  vid.textContent = lap.has_video ? human(lap.bytes) : '—';
  const tag = document.createElement('span');
  tag.className = 'c-tag';
  tag.textContent = lap.notes || '';
  row.append(name, time, vid, tag);
  mgClickable(row, { kind: 'lap', id: lap.id }, () => {
    setMode('session');
    selectLap(lap.id);
  });
  return row;
}

/* --------------------------------------------------------------- selecting */

function mgClickable(row, item, onOpen) {
  MG.rows.push(item);
  row.onclick = (e) => {
    e.preventDefault();
    if (e.shiftKey && MG.sel.length) mgRange(item);
    else if (e.ctrlKey || e.metaKey) mgToggleOne(item);
    else MG.sel = [item];
    mgPaint();
  };
  row.ondblclick = (e) => { e.preventDefault(); onOpen(); };
}

function mgToggleOne(item) {
  const i = MG.sel.findIndex((x) => mgKey(x) === mgKey(item));
  if (i >= 0) MG.sel.splice(i, 1);
  else MG.sel.push(item);
}

/* Shift-click covers everything on screen between the last click and this one,
 * folders included -- the same rule the row order gives you visually. */
function mgRange(item) {
  const keys = MG.rows.map(mgKey);
  const from = keys.indexOf(mgKey(MG.sel[MG.sel.length - 1]));
  const to = keys.indexOf(mgKey(item));
  if (from < 0 || to < 0) { MG.sel = [item]; return; }
  const [lo, hi] = from <= to ? [from, to] : [to, from];
  MG.sel = MG.rows.slice(lo, hi + 1);
}

function mgToggle(id) {
  if (MG.shut.has(id)) MG.shut.delete(id);
  else MG.shut.add(id);
  buildManage();
}

/* Repaint the selection without rebuilding: a rebuild would collapse the row
 * the pointer is over and lose the hover. */
function mgPaint() {
  const rows = [...$('mgTree').children].slice(1);
  rows.forEach((row, i) => {
    const item = MG.rows[i];
    row.classList.toggle('sel', !!item && mgSelected(item.kind, item.id));
  });
  mgSyncBar();
}

/* ---------------------------------------------------------------- the tools */

function mgLapsIn(item) {
  /* Which laps an item covers: itself, a whole day, or a whole track. */
  const lib = S.lib;
  if (!lib) return [];
  if (item.kind === 'lap') {
    return lib.tracks.flatMap(lapsOf).filter((l) => l.id === item.id);
  }
  if (item.kind === 'day') {
    for (const t of lib.tracks) {
      const day = t.days.find((d) => d.id === item.id);
      if (day) return day.laps;
    }
    return [];
  }
  const track = lib.tracks.find((t) => t.id === item.id);
  return track ? lapsOf(track) : [];
}

function mgSyncBar() {
  const n = MG.sel.length;
  const laps = MG.sel.flatMap(mgLapsIn);
  const withVideo = laps.filter((l) => l.has_video);
  const bytes = laps.reduce((a, l) => a + l.bytes, 0);
  $('mgRename').disabled = n !== 1;
  $('mgTag').disabled = n < 1;
  $('mgDelete').disabled = n < 1;
  $('mgDelVideo').disabled = !withVideo.length;
  $('mgExport').disabled = !withVideo.length;
  $('mgClear').disabled = !n;
  $('mgSel').textContent = !n ? 'nothing selected'
    : [n + ' selected', laps.length + ' lap(s)', human(bytes),
       withVideo.length + ' with footage'].join('  ·  ');
}

const MG_WHAT = { track: 'track', day: 'event', lap: 'lap' };

function mgName(item) {
  const lib = S.lib;
  if (item.kind === 'track') {
    const t = lib.tracks.find((x) => x.id === item.id);
    return t ? t.name : '';
  }
  if (item.kind === 'day') {
    for (const t of lib.tracks) {
      const day = t.days.find((d) => d.id === item.id);
      if (day) return day.event || '';
    }
    return '';
  }
  const lap = lib.tracks.flatMap(lapsOf).find((l) => l.id === item.id);
  return lap ? (lap.name || '') : '';
}

function mgNotes(item) {
  const lib = S.lib;
  if (item.kind === 'track') {
    const t = lib.tracks.find((x) => x.id === item.id);
    return t ? t.notes : '';
  }
  if (item.kind === 'day') {
    for (const t of lib.tracks) {
      const day = t.days.find((d) => d.id === item.id);
      if (day) return day.notes;
    }
    return '';
  }
  const lap = lib.tracks.flatMap(lapsOf).find((l) => l.id === item.id);
  return lap ? lap.notes : '';
}

$('mgRename').onclick = () => {
  const item = MG.sel[0];
  if (!item) return;
  const what = MG_WHAT[item.kind];
  const name = prompt('Name for this ' + what + ':', mgName(item));
  if (name === null || !name.trim()) return;
  /* Renaming a day means renaming the event it is under, which is what anyone
   * means by it -- and it puts the day under an existing event of that name if
   * there is one. */
  if (item.kind === 'day') {
    mgDo('renamed the event',
         [['/api/event', { day: item.id, name: name.trim() }]]);
  } else {
    mgDo('renamed the ' + what,
         [['/api/rename', { kind: item.kind, id: item.id,
                            name: name.trim() }]]);
  }
};

$('mgTag').onclick = () => {
  if (!MG.sel.length) return;
  const one = MG.sel.length === 1 ? mgNotes(MG.sel[0]) : '';
  const text = prompt(MG.sel.length === 1
    ? 'Tags or notes (empty clears them):'
    : 'Tags or notes for ' + MG.sel.length + ' item(s) — this replaces what ' +
      'each of them has:', one);
  if (text === null) return;
  mgDo(text.trim() ? 'tagged ' + MG.sel.length + ' item(s)'
                   : 'cleared the tags',
       MG.sel.map((it) => ['/api/notes',
                           { kind: it.kind, id: it.id, text: text.trim() }]));
};

$('mgDelete').onclick = () => {
  if (!MG.sel.length) return;
  const laps = MG.sel.flatMap(mgLapsIn);
  const tracks = MG.sel.filter((x) => x.kind === 'track');
  const what = MG.sel.length === 1
    ? 'the ' + MG_WHAT[MG.sel[0].kind] + ' “' +
      (mgName(MG.sel[0]) || 'unnamed') + '”'
    : MG.sel.length + ' item(s)';
  if (!confirm('Delete ' + what + '?\n\nThat is ' + laps.length + ' lap(s), ' +
               human(laps.reduce((a, l) => a + l.bytes, 0)) +
               '.\n\nIt cannot be undone. The originals are wherever you ' +
               'left them.')) return;
  const calls = [];
  if (tracks.length) {
    calls.push(['/api/delete',
                { what: 'track', ids: tracks.map((x) => x.id) }]);
  }
  /* Laps under a track being deleted go with it; anything else by id. */
  const gone = new Set(tracks.flatMap((t) => mgLapsIn(t).map((l) => l.id)));
  const ids = [...new Set(laps.map((l) => l.id))].filter((i) => !gone.has(i));
  if (ids.length) {
    calls.push(['/api/delete', { what: 'lap', ids }]);
  }
  MG.sel = [];
  mgDo('deleted ' + what, calls);
};

$('mgDelVideo').onclick = () => {
  const ids = [...new Set(MG.sel.flatMap(mgLapsIn).filter((l) => l.has_video)
                                .map((l) => l.id))];
  if (!ids.length) return;
  if (!confirm('Delete the video of ' + ids.length + ' lap(s), keeping their ' +
               'telemetry?')) return;
  mgDo('deleted video from ' + ids.length + ' lap(s)',
       [['/api/delete', { what: 'video', ids }]]);
};

$('mgClear').onclick = () => { MG.sel = []; mgPaint(); };

/* ============================================================== import
 *
 * Three steps: choose a folder, watch it work, look at what came of it. The
 * only expensive thing on this screen happens when the import button is
 * pressed.
 *
 * An earlier version planned the whole import on every keystroke so it could
 * show an estimate of laps kept. Reading nineteen sessions is about twelve
 * seconds, which made every checkbox feel broken, and an estimate was never
 * what anyone came here for. Choosing a folder now costs a file count and
 * nothing else.
 *
 * Each control that costs something says what it costs, measured rather than
 * guessed: 720p is about 52% smaller, 30 fps about 44%, dropping audio about 1%.
 * That last one is worth stating precisely because it looks like a space
 * control and is not.
 */

function importOptions() {
  const q = new URLSearchParams();
  const on = (id) => $(id).checked;
  const val = (id) => $(id).value;
  q.set('pad', val('optPad') || '3');
  q.set('cq', val('optCq'));
  if (val('optHeight')) q.set('height', val('optHeight'));
  if (val('optFps')) q.set('fps', val('optFps'));
  q.set('audio', on('optAudio') ? '1' : '0');
  q.set('video', on('optVideo') ? '1' : '0');
  if (on('optSkipEdge')) q.set('skip_edge', '1');
  if (on('optKeepBestOn')) q.set('keep_best', val('optKeepBest'));
  if (on('optWithinOn')) q.set('within_pct', val('optWithin'));
  if (on('optGapOn')) q.set('max_gap', val('optGap'));
  if (on('optRequireVideo')) q.set('require_video', '1');
  q.set('event', val('optEvent').trim());
  if (val('optTrack')) q.set('track_id', val('optTrack'));
  else q.set('track', val('optTrackName').trim());
  return q;
}

/* What is missing before this import can run, in a sentence, or '' when it is
 * ready. Both names are required: the library is worth keeping because its names
 * mean something, and the name a phone writes into a CSV does not. */
function importBlocker() {
  if (!S.src) return 'choose a folder to import from';
  if (!$('optEvent').value.trim()) return 'name the event';
  if (!$('optTrack').value && !$('optTrackName').value.trim()) {
    return 'name the new track, or pick an existing one';
  }
  return '';
}

/* Existing tracks to import onto, from the library the page already has. */
function fillTrackChoices() {
  const sel = $('optTrack');
  const had = sel.value;
  sel.innerHTML = '';
  const first = document.createElement('option');
  first.value = '';
  first.textContent = 'a new track, named below';
  sel.appendChild(first);
  for (const t of (S.lib ? S.lib.tracks : [])) {
    const o = document.createElement('option');
    o.value = t.id;
    o.textContent = t.name + (t.length_m ? '  (' + Math.round(t.length_m) +
                                           ' m)' : '');
    sel.appendChild(o);
  }
  sel.value = [...sel.options].some(o => o.value === had) ? had : '';
  syncImportReady();
}

/* The new-track name only applies when no existing track is chosen. */
function syncImportReady() {
  const newTrack = !$('optTrack').value;
  $('optTrackNameRow').classList.toggle('hidden', !newTrack);
  const why = importBlocker();
  $('impRun').disabled = !!why;
  const note = $('impRunNote');
  if (why) {
    note.className = 'bad';
    note.textContent = why;
  } else {
    note.className = 'muted';
    note.textContent = $('optVideo').checked
      ? 'reads every session, then cuts and re-encodes each lap — minutes, '
        + 'not seconds'
      : 'reads every session and cuts the laps out of it';
  }
}

function impStep(which) {
  S.impStep = which;
  for (const [id, name] of [['impChoose', 'choose'], ['impWorking', 'working'],
                            ['impReview', 'review']]) {
    $(id).classList.toggle('hidden', which !== name);
  }
}

async function refreshImport() {
  if (S.mode !== 'import') return;
  /* Opening the tab while an import is running joins it in progress, and
   * opening it just after one lands on the review rather than a blank chooser:
   * someone who has just imported wants to see what they got.
   *
   * Once that review has been dismissed the tab goes back to being a chooser.
   * It used to show the last import's review for as long as the server was up,
   * which left no way to import a second folder -- a day's footage usually
   * arrives in more than one place. */
  let job;
  try {
    job = await fetch('/api/import/progress').then((r) => r.json());
  } catch (e) {
    job = { state: 'idle' };
  }
  buildManage();
  pollExport();
  refreshInbox();
  if (job.state === 'running') { impStep('working'); return pollImport(); }
  const finished = job.state === 'done' || job.state === 'unverified';
  if (finished && !S.impDone) return showReview();
  impStep('choose');
  fillTrackChoices();
  /* Coming back to the chooser -- from a failed import, or from another tab --
   * must not leave a button armed for a folder that is no longer chosen. */
  syncImportReady();
  if (job.state === 'failed') impStatus('last import failed: ' + job.error);
}

/* Back to the chooser, ready for the next folder. The path box keeps what was
 * imported so it is obvious which folder that was, but it is re-counted rather
 * than trusted: the point of coming back here is usually to choose a different
 * one. */
async function importAnother() {
  S.impDone = true;
  S.review = null;
  impStatus('');
  impStep('choose');
  $('revTracks').innerHTML = '';
  const was = S.src;
  S.src = '';
  fillTrackChoices();
  if (was) await useSource(was);
  else $('srcNote').textContent = '';
}

/* --------------------------------------------------------- choose a folder */

/* The operating system's own chooser, opened by the server, because a page
 * cannot open one that reports a path -- that is a security boundary, not an
 * oversight. The server is on this machine, so it can. */
async function chooseFolder() {
  const note = $('srcNote');
  note.className = 'muted';
  note.textContent = 'waiting for the folder chooser...';
  $('srcBrowse').disabled = true;
  let d;
  try {
    d = await postJSON('/api/pick');
  } catch (e) {
    note.className = 'bad';
    note.textContent = '' + e;
    return;
  } finally {
    $('srcBrowse').disabled = false;
  }
  if (d.cancelled) { note.className = 'muted'; note.textContent = ''; return; }
  if (d.error) {
    note.className = 'bad';
    note.textContent = d.error + (d.detail ? ' — ' + d.detail : '');
    return;
  }
  await useSource(d.path);
}

/* Counting files in a folder, and nothing more. No parsing, no probing, no
 * planning: those wait for the import button. */
async function useSource(path) {
  const clean = (path || '').trim().replace(/^"|"$/g, '');
  const note = $('srcNote');
  S.src = '';
  syncImportReady();
  if (!clean) { note.textContent = ''; return; }
  note.className = 'muted';
  note.textContent = 'looking...';
  let d;
  try {
    d = await fetch('/api/survey?path=' + encodeURIComponent(clean))
      .then((r) => r.json());
  } catch (e) {
    note.className = 'bad';
    note.textContent = '' + e;
    return;
  }
  if (d.error || !d.exists) {
    note.className = 'bad';
    note.textContent = d.error || ('no folder at ' + clean);
    return;
  }
  if (!d.csvs) {
    note.className = 'bad';
    note.textContent = 'no RaceChrono CSVs anywhere under ' + d.path +
      (d.videos ? ' — ' + d.videos + ' video file(s), but nothing to put '
                  + 'them on a track with' : '');
    return;
  }
  S.src = d.path;
  $('srcPath').value = d.path;
  note.className = 'good';
  note.innerHTML = '<span class="big">' + d.csvs + ' data file(s), ' +
    d.videos + ' video file(s)</span><br><span class="muted">' +
    human(d.bytes) + ' in ' + esc(d.path) +
    (d.videos ? '' : ' — no footage here, so the laps will be telemetry '
                     + 'only') + '</span>';
  syncImportReady();
}

/* ------------------------------------------------------------- do the work */

async function runImport() {
  if (!S.src) return;
  const sel = $('optTrack');
  const where = sel.value ? sel.options[sel.selectedIndex].textContent
                          : $('optTrackName').value.trim() + '  (new track)';
  if (!confirm('Import from\n\n' + S.src + '\n\nEvent: ' +
               $('optEvent').value.trim() + '\nTrack: ' + where +
               '\n\nNothing in that folder is moved, changed or deleted. ' +
               'Re-encoding video takes a while.')) return;
  const q = importOptions();
  q.set('source', S.src);
  return startImport(q);
}

/* Start an import with every option already decided, and follow it. Shared by
 * the import form and the upload card's one button. True if it started. */
async function startImport(q) {
  $('impRun').disabled = true;
  S.impDone = false;
  impStep('working');
  $('impPhase').textContent = 'starting...';
  $('impBar').style.width = '0';
  $('impDetail').textContent = '';
  impStatus('');
  let d;
  try {
    d = await postJSON('/api/import/run', Object.fromEntries(q));
  } catch (err) {
    d = { error: '' + err };
  }
  if (d.error) {
    impStep('choose');
    $('impRun').disabled = false;
    impStatus(d.error);
    quickJob({ state: 'failed', error: d.error });
    return false;
  }
  pollImport();
  return true;
}

async function pollImport() {
  let job;
  try {
    job = await fetch('/api/import/progress').then((r) => r.json());
  } catch (e) {
    $('impDetail').textContent =
      'lost contact with the server — the import may still be running';
    setTimeout(pollImport, 2000);
    return;
  }
  quickJob(job);
  if (job.state === 'running') {
    $('impPhase').textContent = job.phase || 'working';
    /* Reading the folder and building the maps come before any lap is cut, so
     * a bar driven only by laps would sit at zero through them. A floor says
     * something is happening without pretending to know how far along it is. */
    const pct = job.total
      ? Math.max(3, Math.round(100 * job.done / job.total)) : 3;
    $('impBar').style.width = pct + '%';
    const bits = [];
    if (job.total) bits.push(job.done + ' of ' + job.total + ' lap(s)');
    if (job.eta_s) bits.push('about ' + Math.round(job.eta_s) + 's left');
    if (job.bytes) bits.push(human(job.bytes) + ' written');
    if (job.lap) bits.push(job.lap);
    $('impDetail').textContent = bits.join('   ·   ');
    setTimeout(pollImport, 700);
    return;
  }
  if (job.state === 'failed') {
    impStep('choose');
    $('impRun').disabled = false;
    impStatus('import failed: ' + job.error);
    return;
  }
  $('impBar').style.width = '100%';
  S.impDone = false;
  await showReview();
  await loadLibrary();
  buildManage();
}

/* --------------------------------------------------------------- review it */

async function showReview() {
  impStep('review');
  let d;
  try {
    d = await fetch('/api/import/review').then((r) => r.json());
  } catch (e) {
    impStatus('' + e);
    return;
  }
  S.review = d;
  const r = d.result || {};
  const v = r.verified || {};
  const rows = [
    ['tracks', String(r.tracks || 0)],
    ['sessions', String(r.sessions || 0)],
    ['laps', String(r.laps || 0)],
    ['written', human(r.bytes || 0)],
  ];
  /* Both of these mean the folder holds more than the library does, which is
   * worth saying on the screen that invites deleting the folder. */
  if (r.already) rows.push(['already imported', r.already + ' session(s)']);
  if (r.dropped) rows.push(['left behind by a filter', r.dropped + ' lap(s)']);
  rows.push(['library holds', (d.library.laps || 0) + ' lap(s), ' +
                              human(d.library.bytes || 0)]);
  $('revSummary').innerHTML = rows.map(
    ([k, val]) => '<span class="k">' + k + '</span><span class="v">' +
                  val + '</span>').join('');

  /* The one sentence in this tool that invites someone to delete irreplaceable
   * footage. It appears only when every lap written has been read back off disk
   * and checked against the plan. */
  const safe = $('revSafe');
  if (v.ok) {
    const caveat = [];
    if (v.already) caveat.push(v.already + ' session(s) were already in the ' +
                               'library');
    if (v.dropped) caveat.push(v.dropped + ' lap(s) were left behind by a ' +
                               'filter and are not in the library');
    safe.className = '';
    safe.textContent = 'Checked: ' + v.laps + ' lap(s), ' + human(v.bytes) +
      ' on disk. The ' + v.source_files + ' file(s) in ' + v.source + ' (' +
      human(v.source_bytes) + ') are safe to delete — this tool never ' +
      'touches them.' + (caveat.length ? ' Note: ' + caveat.join('; ') + '.'
                                       : '');
  } else {
    const bad = v.problems || [];
    safe.className = 'bad';
    safe.textContent = bad.length
      ? 'The check found ' + bad.length + ' problem(s): ' + bad.join('; ') +
        ' — leave the originals where they are.'
      : 'This import was not checked. Leave the originals where they are.';
  }

  const box = $('revTracks');
  box.innerHTML = '';
  /* A session put on a track by hand can be the wrong shape for it, and that is
   * worth seeing before it is buried in a lap list. */
  for (const w of (r.warnings || [])) {
    const note = document.createElement('div');
    note.className = 'card wide';
    note.innerHTML = '<div class="cardhead bad">' + esc(w.session) + '</div>' +
                     '<div class="bad">' + esc(w.why) + '</div>';
    box.appendChild(note);
  }
  if (!d.tracks.length) {
    const note = document.createElement('div');
    note.className = 'card wide';
    note.innerHTML = '<div class="cardhead">nothing new</div>' +
      '<div class="muted">' + (r.already
        ? 'Every session in that folder was already in the library, so nothing '
          + 'was written. Import a different folder, or delete laps first.'
        : 'That folder produced no laps. Check the options — a filter may have '
          + 'left everything behind.') + '</div>';
    box.appendChild(note);
  }
  for (const t of d.tracks) {
    const card = document.createElement('div');
    card.className = 'card revtrack';
    const withVideo = t.laps.filter((l) => l.has_video).length;
    const head = [t.closed ? 'circuit' : 'open course'];
    if (t.new) head.push('new track');
    head.push(t.map ? Math.round(t.map.length_m) + ' m' : 'no map');
    head.push(t.laps.length + ' lap(s)');
    head.push(withVideo ? withVideo + ' with footage' : 'no footage');
    card.innerHTML =
      '<div class="cardhead">' + esc(t.name) +
      '<span class="muted">' + head.join('  ·  ') + '</span></div>' +
      '<div class="revbody"><canvas></canvas><div class="revlaps"></div></div>';
    box.appendChild(card);

    const body = t.laps.map((l) =>
      '<tr><td class="nm">' + esc(shortStem(l.session)) + ' ' + esc(l.kind) +
      ' ' + l.number + '</td>' +
      '<td>' + esc(l.label) + '</td>' +
      '<td>' + (l.has_video ? human(l.bytes) : '—') + '</td>' +
      '<td>' + (l.gap_m > 20
        ? '<span class="hole">' + Math.round(l.gap_m) + ' m hole</span>'
        : '<span class="muted">ok</span>') + '</td></tr>').join('');
    card.querySelector('.revlaps').innerHTML =
      '<table class="sec"><tr><th class="nm">lap</th><th>time</th>' +
      '<th>video</th><th>GPS</th></tr>' + body + '</table>';

    const cv = card.querySelector('canvas');
    if (t.map) drawTrackMap(cv, t.map);
    else cv.remove();
  }
  impStatus('');
}

/* The map that was built out of these laps, so it can be recognised at a glance
 * as the shape that was driven. A length in metres cannot be recognised. */
function drawTrackMap(cv, map) {
  const dpr = window.devicePixelRatio || 1;
  /* Fit the shape into a fixed box rather than fixing its width: a tall circuit
   * at a fixed 240 px wide grew a 240 px-tall card with an empty half beside the
   * lap table. */
  const box = [240, 200], ar = Math.max(0.05, map.aspect);
  let w = box[0], h = w / ar;
  if (h > box[1]) { h = box[1]; w = h * ar; }
  w = Math.round(Math.max(90, w));
  h = Math.round(Math.max(90, h));
  cv.style.width = w + 'px';
  cv.style.height = h + 'px';
  cv.width = Math.round(w * dpr);
  cv.height = Math.round(h * dpr);
  const ctx = cv.getContext('2d');
  const pad = 10 * dpr;
  const W = cv.width - 2 * pad, H = cv.height - 2 * pad;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.save();
  ctx.translate(pad, pad);
  ctx.beginPath();
  map.points.forEach(([u, v], i) => {
    const [x, y] = mapProject(u, v, W, H, map.aspect);
    if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
  });
  if (map.closed) ctx.closePath();
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.strokeStyle = '#2f3742';
  ctx.lineWidth = 7 * dpr;
  ctx.stroke();
  ctx.strokeStyle = '#c9d1d9';
  ctx.lineWidth = 2.5 * dpr;
  ctx.stroke();
  /* Where a lap starts and where it ends. On a circuit they are the same place
   * and the finish dot lands on the start one, which is correct and is also how
   * a circuit can be told from a course at a glance. */
  for (const pair of [[map.start, '#3ddc84'], [map.finish, '#e0564b']]) {
    const pt = pair[0];
    if (!pt) continue;
    const [x, y] = mapProject(pt[0], pt[1], W, H, map.aspect);
    ctx.beginPath();
    ctx.arc(x, y, 4 * dpr, 0, Math.PI * 2);
    ctx.fillStyle = pair[1];
    ctx.fill();
  }
  ctx.restore();
}

function impStatus(msg) { $('impStatus').textContent = msg || ''; }

/* Track and session names are typed by people and end up inside innerHTML. */
function esc(s) {
  return String(s === null || s === undefined ? '' : s).replace(
    /[&<>"]/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function human(n) {
  n = Number(n) || 0;
  for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (Math.abs(n) < 1024 || unit === 'TB') return n.toFixed(1) + ' ' + unit;
    n /= 1024;
  }
}

$('modeImport').onclick = () => setMode('import');
$('srcBrowse').onclick = chooseFolder;
$('impRun').onclick = runImport;
$('impDone').onclick = () => { S.impDone = true; setMode('session'); };
$('impAgain').onclick = importAnother;
$('srcPath').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); useSource($('srcPath').value); }
});
$('srcPath').addEventListener('change', () => useSource($('srcPath').value));
for (const id of ['optEvent', 'optTrackName']) {
  $(id).addEventListener('input', syncImportReady);
}
$('optTrack').addEventListener('change', syncImportReady);
$('optVideo').addEventListener('change', syncImportReady);

{
  const zone = $('drop');
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  for (const t of ['dragenter', 'dragover']) {
    zone.addEventListener(t, (e) => { stop(e); zone.classList.add('over'); });
  }
  for (const t of ['dragleave', 'drop']) {
    zone.addEventListener(t, (e) => { stop(e); zone.classList.remove('over'); });
  }
  /* A drop hands over the files inside a folder and the folder's name, never
   * its path. Nothing can be done with that but say so and point at the
   * chooser, which does know where things are. */
  zone.addEventListener('drop', (e) => {
    const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
    /* On a server a dropped file has nowhere to be read from but here, so it
     * is uploaded, the same as one dropped on the upload card. */
    if (CFG.headless && files.length) return uploadFiles(files);
    const rel = files.map((f) => f.webkitRelativePath || '').filter(Boolean);
    const folder = rel.length ? rel[0].split('/')[0] : '';
    const note = $('srcNote');
    note.className = 'muted';
    if (folder) {
      note.textContent = 'A browser will not tell this page where "' + folder +
        '" is on disk. Choose it with the button, or paste the full path.';
    } else if (files.length) {
      note.textContent = 'Dropped ' + files.length + ' file(s). Point at the ' +
        'folder holding them: use the button, or paste its path.';
    }
  });
}

/* ================================================================ uploading
 *
 * From a phone there is no folder to point at, so the files come to the server
 * first, into a new folder of their own, and that folder is then imported like
 * any other.
 *
 * Each file goes up in slices (Blob.slice), each sent with the offset it
 * belongs at; the server writes it there and keeps a note of which ranges it
 * has, so nothing is held in memory at either end. A slice that fails is sent
 * again with a growing pause -- a phone's link drops for seconds at a time --
 * and a file that still fails can be retried: starting it again returns what
 * already arrived, and only the gaps are sent. Nothing appears in the upload
 * folder until every byte of a file is there.
 */

const UP = {
  folder: null,     // {folder, path}: where this batch is going
  label: '',
  rows: [],         // one per file: {file, el, state, received, id, error}
  running: false,
  wake: null,
  free: null,
};

/* `postJSON`, thrown rather than returned when refused: `upRetry` decides from
 * the status whether sending it again could help. */
async function upPost(path, body, binary) {
  const d = await postJSON(path, body, binary);
  if (d.error) {
    const err = new Error(d.error);
    err.status = d.status;
    throw err;
  }
  return d;
}

function upAccepts(file) {
  const exts = (CFG.upload && CFG.upload.extensions) ||
    ['.avi', '.csv', '.insv', '.lrv', '.m4v', '.mkv', '.mov', '.mp4'];
  const i = file.name.lastIndexOf('.');
  return i > 0 && exts.includes(file.name.slice(i).toLowerCase());
}

async function uploadFiles(list) {
  const files = [...list];
  if (!files.length) return;
  if (!UP.folder) {
    UP.label = $('upName').value.trim();
    try {
      UP.folder = await upPost('/api/upload/folder', { name: UP.label });
    } catch (e) {
      $('upMeta').textContent = 'could not start an upload: ' + e.message;
      return;
    }
  }
  $('upName').disabled = true;
  $('upDone').classList.add('hidden');
  $('upChoose').textContent = 'Add more to this upload…';
  Q.seq++;                      // a check of the folder as it was is stale now
  for (const f of files) {
    const ok = upAccepts(f);
    const row = { file: f, state: ok ? 'queued' : 'skipped', received: 0,
                  id: null, error: ok ? '' : 'not a CSV or a video', note: '' };
    row.el = upRowEl();
    UP.rows.push(row);
    upPaint(row);
  }
  upPump();
}

async function upPump() {
  if (UP.running) return;
  UP.running = true;
  upWake(true);
  try {
    for (;;) {
      const row = UP.rows.find((r) => r.state === 'queued');
      if (!row) break;
      await upOne(row);
    }
  } finally {
    UP.running = false;
    upWake(false);
    upFinished();
  }
}

async function upOne(row) {
  const f = row.file;
  row.state = 'sending';
  row.error = '';
  row.t0 = performance.now();
  row.b0 = row.received;
  upPaint(row);
  try {
    const b = await upRetry(() => upPost('/api/upload/begin', {
      folder: UP.folder.folder, name: f.name, size: f.size }), row);
    if (b.done) {
      row.received = f.size;
      row.state = 'done';
      row.note = 'already there';
      return;
    }
    row.id = b.id;
    row.received = row.b0 = b.received;
    row.t0 = performance.now();
    const chunk = b.chunk || (8 << 20);
    let ranges = b.ranges || [];
    for (let off = 0; off < f.size; off += chunk) {
      const end = Math.min(f.size, off + chunk);
      if (ranges.some(([a, z]) => a <= off && z >= end)) continue;
      const d = await upRetry(() => upPost(
        '/api/upload/chunk?id=' + row.id + '&offset=' + off,
        f.slice(off, end), true), row);
      ranges = d.ranges;
      row.received = d.received;
      upPaint(row);
    }
    const fin = await upRetry(() => upPost('/api/upload/finish',
                                           { id: row.id }), row);
    row.state = 'done';
    row.received = f.size;
    row.savedAs = fin.name;
  } catch (e) {
    row.state = 'failed';
    row.error = e.message || String(e);
  } finally {
    upPaint(row);
  }
}

/* Network errors and server errors are retried with a growing pause. A
 * refusal (4xx, or 507 for a full disk) is not: sending it again would get the
 * same answer. */
async function upRetry(fn, row) {
  const waits = [1, 2, 4, 8, 15, 30];
  for (let i = 0; ; i++) {
    try {
      return await fn();
    } catch (e) {
      const refused = (e.status >= 400 && e.status < 500 &&
                       e.status !== 408 && e.status !== 429) ||
                      e.status === 507;
      if (refused || i >= waits.length) throw e;
      row.note = 'retrying in ' + waits[i] + ' s — ' + (e.message || e);
      upPaint(row);
      await new Promise((res) => setTimeout(res, waits[i] * 1000));
      row.note = '';
    }
  }
}

function upRowEl() {
  const el = document.createElement('div');
  el.className = 'upfile';
  el.innerHTML = '<span class="nm"></span><span class="st"></span>' +
                 '<div class="progress"><i></i></div>';
  $('upList').appendChild(el);
  return el;
}

function fmtLeft(s) {
  if (!isFinite(s)) return '';
  if (s < 90) return Math.max(1, Math.round(s)) + ' s left';
  if (s < 5400) return Math.round(s / 60) + ' min left';
  return Math.floor(s / 3600) + ' h ' + Math.round((s % 3600) / 60) +
         ' min left';
}

function upPaint(row) {
  const el = row.el;
  const f = row.file;
  el.classList.toggle('done', row.state === 'done');
  el.classList.toggle('bad', row.state === 'failed' || row.state === 'skipped');
  el.querySelector('.nm').textContent = f.name +
    (row.savedAs && row.savedAs !== f.name ? '  →  ' + row.savedAs : '');
  el.querySelector('.progress > i').style.width =
    (f.size ? Math.round(100 * row.received / f.size) : 100) + '%';
  let text;
  if (row.state === 'skipped') text = 'skipped — ' + row.error;
  else if (row.state === 'failed') text = 'stopped at ' + human(row.received) +
                                          ' — ' + row.error + ' ';
  else if (row.state === 'done') text = human(f.size) + ' — ' +
                                        (row.note || 'done');
  else if (row.state === 'queued') text = human(f.size) + ' — waiting';
  else {
    const secs = (performance.now() - row.t0) / 1000;
    const rate = secs > 2 ? (row.received - row.b0) / secs : 0;
    text = human(row.received) + ' of ' + human(f.size);
    if (rate > 0) text += '  ·  ' + human(rate) + '/s  ·  ' +
                          fmtLeft((f.size - row.received) / rate);
    if (row.note) text += '  ·  ' + row.note;
  }
  const st = el.querySelector('.st');
  st.textContent = text;
  if (row.state === 'failed') {
    const again = document.createElement('button');
    again.className = 'act';
    again.textContent = 'retry';
    again.onclick = () => {
      row.state = 'queued';
      upPaint(row);
      upPump();
    };
    st.appendChild(again);
  }
  upSummary();
}

function upSummary() {
  const rows = UP.rows.filter((r) => r.state !== 'skipped');
  const free = UP.free !== null ? human(UP.free) + ' free on the server' : '';
  if (!rows.length) { $('upMeta').textContent = free; return; }
  const done = rows.filter((r) => r.state === 'done').length;
  const total = rows.reduce((s, r) => s + r.file.size, 0);
  const sent = rows.reduce((s, r) => s + r.received, 0);
  $('upMeta').textContent = done + ' of ' + rows.length + ' file(s)  ·  ' +
    human(sent) + ' of ' + human(total) + '  ·  into ' + UP.folder.folder;
}

function upFinished() {
  const rows = UP.rows.filter((r) => r.state !== 'skipped');
  const ok = rows.length && rows.every((r) => r.state === 'done');
  $('upDone').classList.toggle('hidden', !ok);
  refreshInbox();
  if (ok) quickPlan();
}

/* Safari on a phone stops a page's network traffic when the screen locks. A
 * wake lock keeps the screen on while files are going up; the browser drops it
 * when the page is hidden, so it is taken again on return. */
async function upWake(on) {
  try {
    if (on && !UP.wake && navigator.wakeLock) {
      UP.wake = await navigator.wakeLock.request('screen');
    } else if (!on && UP.wake) {
      const w = UP.wake;
      UP.wake = null;
      await w.release();
    }
  } catch (e) {
    UP.wake = null;
  }
}

function importFolder(path) {
  if (!$('optEvent').value.trim() && UP.label) $('optEvent').value = UP.label;
  useSource(path).then(() => {
    $('impNaming').scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (!$('optEvent').value.trim()) $('optEvent').focus();
  });
}

/* Earlier uploads: import one, or delete it once its laps are in the library.
 * On a server nobody can clear the inbox by hand without a shell. */
async function refreshInbox() {
  let d;
  try {
    d = await fetch('/api/inbox').then((r) => r.json());
  } catch (e) { return; }
  UP.free = d.free_bytes === undefined ? null : d.free_bytes;
  upSummary();
  const box = $('upFolders');
  box.innerHTML = '';
  const rows = d.folders || [];
  if (!rows.length) return;
  const head = document.createElement('div');
  head.className = 'opthead';
  head.textContent = 'uploads on the server';
  box.appendChild(head);
  for (const f of rows) {
    const row = document.createElement('div');
    row.className = 'upfolder';
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = f.folder + (f.partial ? '  (' + f.partial +
                                 ' unfinished)' : '');
    const sz = document.createElement('span');
    sz.className = 'sz';
    sz.textContent = f.csvs + ' csv  ·  ' + f.videos + ' video  ·  ' +
                     human(f.bytes);
    const acts = document.createElement('span');
    acts.className = 'acts';
    const imp = document.createElement('button');
    imp.className = 'act';
    imp.textContent = 'import';
    imp.disabled = !f.csvs;
    imp.onclick = () => importFolder(f.path);
    const del = document.createElement('button');
    del.className = 'act';
    del.textContent = 'delete';
    del.disabled = UP.running && UP.folder && UP.folder.folder === f.folder;
    del.onclick = async () => {
      if (!confirm('Delete the upload "' + f.folder + '"?\n\nIts ' +
                   (f.csvs + f.videos) + ' file(s), ' + human(f.bytes) +
                   ', are removed from the server. Laps already imported ' +
                   'from it stay in the library.')) return;
      try {
        await upPost('/api/inbox/delete', { folder: f.folder });
      } catch (e) {
        impStatus(e.message);
      }
      if (UP.folder && UP.folder.folder === f.folder) resetUpload();
      refreshInbox();
    };
    acts.append(imp, del);
    row.append(nm, sz, acts);
    box.appendChild(row);
  }
}

function resetUpload() {
  if (UP.running || Q.active) return;
  UP.folder = null;
  UP.label = '';
  UP.rows = [];
  $('upList').innerHTML = '';
  $('upDone').classList.add('hidden');
  $('upName').disabled = false;
  $('upName').value = '';
  $('upChoose').textContent = 'Choose files…';
  Q.seq++;
  Q.folder = null;
  Q.s = null;
  $('upReady').classList.add('hidden');
  upSummary();
}

/* ============================================================ quick import
 *
 * From a phone, the import form is most of the work: two names to type and a
 * dozen options nobody changes at the track. So once an upload is in, the
 * server plans it -- reading, writing nothing -- and says what an import would
 * do and what it would call things, and this card offers that as one button.
 * The names are editable, "more options" is the full form, and anything the
 * server is unsure of -- a session with no footage, two tracks in one upload,
 * a session that fits no track -- is said rather than imported quietly.
 *
 * "Import by itself" (remembered per browser) skips the button, but only for
 * an upload the server calls `auto`: nothing missing, nothing odd. The CSV
 * from Files usually arrives before the video from Photos, and the first
 * check finds no footage, so it waits for the second.
 */
const Q = {
  seq: 0,          // bumped by anything that makes a check in flight stale
  folder: null,    // the upload the card describes
  s: null,         // the server's `suggest` for it
  active: false,   // an import started from this card is running
};
const AUTO_KEY = 'lapview.autoImport';

function quickAuto() {
  try { return localStorage.getItem(AUTO_KEY) === '1'; } catch (e) {
    return false;
  }
}

function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

function clockOf(unix) {
  try {
    return new Date(unix * 1000).toLocaleTimeString([], {
      hour: 'numeric', minute: '2-digit' });
  } catch (e) { return ''; }
}

async function quickPlan() {
  if (!UP.folder || UP.running || Q.active) return;
  const seq = ++Q.seq;
  const folder = UP.folder;
  Q.folder = folder;
  Q.s = null;
  $('upReady').classList.remove('hidden');
  $('qHead').textContent = 'checking the upload…';
  $('qMeta').textContent = 'reading the session and matching the footage';
  $('qWarn').textContent = '';
  $('qNames').classList.add('hidden');
  $('qImport').disabled = true;
  $('qImport').classList.remove('hidden');
  $('qMore').classList.add('hidden');
  $('qMore').textContent = 'more options';
  $('qMore').onclick = quickMore;
  $('qBar').classList.add('hidden');
  $('qNote').textContent = '';
  /* The form's filters, so the lap count is what an import would keep; but
   * not its names, which would change what the plan calls a new track. */
  const q = importOptions();
  for (const k of ['event', 'track', 'track_id']) q.delete(k);
  q.set('source', folder.path);
  let d;
  try {
    d = await fetch('/api/import?' + q).then((r) => r.json());
  } catch (e) {
    d = { error: '' + e };
  }
  if (seq !== Q.seq) return;          // more files arrived while it read
  if (d.error) {
    $('qHead').textContent = 'could not check the upload';
    $('qMeta').textContent = d.error;
    $('qMore').classList.remove('hidden');
    return;
  }
  const s = d.suggest || {};
  Q.s = s;
  paintQuick(s);
  $('upReady').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  if (s.auto && quickAuto()) quickImport();
}

function paintQuick(s) {
  $('qHead').textContent = s.track_name
    ? s.track_name + (s.track_new ? '  ·  new track' : '')
    : s.sessions ? 'more than one track'
    : s.waiting ? 'waiting for the CSV' : 'nothing new to import';
  const bits = [];
  if (s.sessions) bits.push(plural(s.sessions, 'session'));
  if (s.laps) {
    bits.push(plural(s.laps, 'lap'));
    bits.push(s.laps_video === s.laps ? 'video on every lap'
      : s.laps_video ? 'video on ' + s.laps_video + ' of ' + s.laps
      : 'no video');
  }
  if (s.dates && s.dates.length) bits.push(s.dates.join(', '));
  $('qMeta').textContent = bits.join('  ·  ');

  const warn = [];
  if (s.why) warn.push(s.why);
  for (const n of (s.no_video || [])) {
    warn.push('No video found for the ' + clockOf(n.t0) + ' session (' +
              Math.round(n.duration / 60) + ' min). Add it from Photos with ' +
              'the button above, or import it without.');
  }
  for (const p of (s.problems || [])) warn.push(p);
  $('qWarn').innerHTML = warn.map((w) => '<div>' + esc(w) + '</div>').join('');

  $('qNames').classList.toggle('hidden', !s.ready);
  $('qEvent').value = s.event || '';
  $('qTrackRow').classList.toggle('hidden', !s.track_new);
  $('qTrack').value = s.track_name || '';
  $('qImport').disabled = !s.ready;
  $('qMore').classList.toggle('hidden', !s.sessions);
  $('qAuto').checked = quickAuto();
  $('qNote').textContent = s.ready && s.auto && quickAuto()
    ? 'importing by itself…' : '';
}

async function quickImport() {
  const s = Q.s;
  if (!s || !s.ready || !Q.folder || Q.active) return;
  const event = $('qEvent').value.trim();
  const track = s.track_new ? $('qTrack').value.trim() : s.track_name;
  if (!event) { $('qNote').textContent = 'name the event'; return; }
  if (!track) { $('qNote').textContent = 'name the track'; return; }
  /* The track goes by name, not id, even when it is one the library has: the
   * plan put these sessions there by their shape, and a name only ever names
   * a track the import creates. An id would put every session on it by hand. */
  const q = importOptions();
  q.delete('track_id');
  q.set('event', event);
  q.set('track', track);
  q.set('source', Q.folder.path);
  Q.active = true;
  $('qImport').disabled = true;
  $('qNames').classList.add('hidden');
  $('qMore').classList.add('hidden');
  $('qBar').classList.remove('hidden');
  $('qFill').style.width = '3%';
  $('qNote').textContent = 'starting…';
  if (!await startImport(q)) Q.active = false;
}

/* The import's progress, on the card that started it. */
function quickJob(job) {
  if (!Q.active) return;
  if (job.state === 'running') {
    $('qFill').style.width = (job.total
      ? Math.max(3, Math.round(100 * job.done / job.total)) : 3) + '%';
    $('qNote').textContent = (job.phase || 'working') +
      (job.total ? ' — ' + job.done + ' of ' + job.total + ' lap(s)' : '');
    return;
  }
  Q.active = false;
  if (job.state === 'failed') {
    $('qBar').classList.add('hidden');
    $('qNames').classList.toggle('hidden', !(Q.s && Q.s.ready));
    $('qImport').disabled = !(Q.s && Q.s.ready);
    $('qMore').classList.remove('hidden');
    $('qNote').textContent = 'import failed: ' + (job.error || 'unknown');
    return;
  }
  $('qBar').classList.add('hidden');
  $('qImport').classList.add('hidden');
  const r = job.result || {};
  const v = r.verified || {};
  $('qHead').textContent = 'Imported ' + plural(r.laps || 0, 'lap') +
    (Q.s && Q.s.track_name ? ' at ' + Q.s.track_name : '');
  $('qMeta').textContent = v.ok
    ? 'checked on disk; the upload can be deleted below'
    : 'written, but the check found a problem — see the review below';
  $('qWarn').textContent = '';
  $('qNote').textContent = '';
  const watch = (r.lap_ids || [])[0];
  if (watch) {
    $('qMore').textContent = 'watch it';
    $('qMore').classList.remove('hidden');
    $('qMore').onclick = async () => {
      S.impDone = true;
      await setMode('session');
      await selectLap(watch);
    };
  }
}

$('qImport').onclick = quickImport;
function quickMore() { if (Q.folder) importFolder(Q.folder.path); }
$('qMore').onclick = quickMore;
/* Ticking it does not import what is on the card now: it says "by itself
 * once both are in", and this one is waiting for a person already. */
$('qAuto').addEventListener('change', (e) => {
  try { localStorage.setItem(AUTO_KEY, e.target.checked ? '1' : '0'); }
  catch (err) { /* a private window: nothing to remember it in */ }
});

for (const id of ['upFiles', 'upFilesAny']) {
  $(id).addEventListener('change', (e) => {
    const files = [...e.target.files];
    e.target.value = '';
    uploadFiles(files);
  });
}
$('upMore').onclick = resetUpload;
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && UP.running) {
    UP.wake = null;
    upWake(true);
  }
});
window.addEventListener('beforeunload', (e) => {
  if (!UP.running) return;
  e.preventDefault();
  e.returnValue = '';
});
{
  const zone = $('upDrop');
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  for (const t of ['dragenter', 'dragover']) {
    zone.addEventListener(t, (e) => { stop(e); zone.classList.add('over'); });
  }
  for (const t of ['dragleave', 'drop']) {
    zone.addEventListener(t, (e) => { stop(e); zone.classList.remove('over'); });
  }
  zone.addEventListener('drop', (e) => {
    uploadFiles((e.dataTransfer && e.dataTransfer.files) || []);
  });
}

/* A server has no desktop: no folder chooser, and the import box reads
 * folders under the inbox rather than anywhere on a disk. */
function applyServerMode() {
  if (!CFG.headless) return;
  /* On a server, files arrive by upload -- from a phone, at the track -- so
   * the upload card leads the library tab instead of sitting under the
   * library and the exports, three screens down on a phone. */
  $('impApp').prepend($('upCard'));
  $('srcBrowse').classList.add('hidden');
  $('dropTitle').textContent = 'Import a folder on the server';
  $('dropWhy').textContent = 'an upload from above, or a folder under ' +
    CFG.inbox_dir + ' — type its name or full path. Files dropped here are ' +
    'uploaded.';
  $('srcPath').placeholder = 'an upload, e.g. 2026-11-03_141502';
}

async function loadConfig() {
  try {
    Object.assign(CFG, await fetch('/api/config').then((r) => r.json()));
  } catch (e) { /* a desktop default is a safe one */ }
  applyServerMode();
}

requestAnimationFrame(tick);
/* The hash names the screen, so a reload keeps it and a link can point at one:
 * `#library` for the library tab, `#compare` for the laps tab with a comparison
 * already up, `#add` for the library tab scrolled to the upload card -- what a
 * phone's shortcut to "send a session" should open. */
const WANT = (window.location && window.location.hash || '').replace('#', '');
loadConfig()
  .then(() => {
    if (WANT === 'library' || WANT === 'import' || WANT === 'add') {
      setMode('import');
    }
    if (WANT === 'add') $('upCard').scrollIntoView({ block: 'start' });
    return loadLibrary();
  })
  .then(() => {
    if (WANT !== 'compare' || !S.lap) return;
    preselect();
    buildLapList();
    applyMode();
    return runCompare();
  })
  .catch(e => status('' + e));
