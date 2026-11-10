#!/usr/bin/env python3
"""NInfer live panel backend — read-only, stdlib only.

What it does, and nothing else:
  * every poll it GETs the local NInfer server's own read endpoints:
    /health, /v1/load, /stats, /metrics (and /slots for the processing count)
  * it computes *instant* decode / prefill rates from counter deltas
    (committed_decode_tokens / computed_prefill_tokens between two polls).
    NInfer's own /metrics and /stats only give cumulative averages, so a
    "right now" tok/s has to come from a delta — that is the one number
    this panel derives, and it says so in the UI.
  * it optionally runs `nvidia-smi` (host-injected by the NVIDIA container
    toolkit) for GPU utilisation / memory / temperature / power.
  * it optionally reads /host/proc/meminfo (host /proc mounted read-only)
    for host system memory. Without that mount it reports system memory
    as unavailable — it never substitutes the container's own meminfo,
    which would be the wrong machine's numbers.

What it never does:
  * no POST to NInfer (no load/unload/chat/generation), no model download,
    no engine start, no Strata anything. If NInfer is down, the panel
    says offline; it does not start anything to fill the gap.
  * the NInfer console (/api/console*) is a read-only management
    *preview*: model library, the config on record, schema validation,
    compatibility checks and a diff/compose preview. It applies
    nothing — no Docker access, no compose writes, no NInfer calls.
    The only files it writes are the panel's own records in the data
    volume (the config on record, an unsaved-changes draft, and the
    record history). No deployment's configuration is built into the
    code: the library, presets and limits all come from the operator's
    own files, the models directory, and runtime measurements.

State mapping (the panel's own mapping — NInfer exposes no single
"state" field, so this is documented, not quoted from the server):
  offline  : /health unreachable or not ok
  queued   : waiting > 0 and running == 0
  warming  : prefilling > 0 and decode_ready == 0
  generating: decode_ready > 0, or running > 0
  idle     : otherwise (loaded, nothing running or waiting)

Environment:
  NINFER_BASE_URL  default http://host.docker.internal:8080
                   (set to http://ninfer:8080 if both containers share
                   a user network, or http://127.0.0.1:8080 with host networking)
  NINFER_API_KEY   optional; sent as Bearer on the read endpoints that
                   require it. /health itself is public in NInfer.
  PANEL_BIND / PANEL_PORT  default 0.0.0.0:8091 (compose publishes it
                   as 127.0.0.1:8091 on the host — see docker-compose.yml)
  POLL_INTERVAL    seconds between backend refreshes, default 2
  PANEL_REQUEST_LOG  optional path to NInfer's --request-log-jsonl file
                   (mount it read-only) for real per-request records
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import console_core as console

BASE = os.environ.get("NINFER_BASE_URL", "http://host.docker.internal:8080").rstrip("/")
API_KEY = os.environ.get("NINFER_API_KEY", "")
BIND = os.environ.get("PANEL_BIND", "0.0.0.0")
PORT = int(os.environ.get("PANEL_PORT", "8091"))
POLL = float(os.environ.get("POLL_INTERVAL", "2"))
WEB_DIR = Path(__file__).resolve().parent / "web"

_lock = threading.Lock()
_snapshot = {"ninfer_ok": False, "state": "offline", "error": "not polled yet"}
_prev = None  # (timestamp, decode_tokens, prefill_tokens)
_prev_draft = None  # (timestamp, draft_tokens_total, draft_accepted_tokens_total)
# Rolling 15-minute window: counter samples kept for 900 s so the panel
# can show a true "last 15 minutes" average like the screenshot panel,
# not just the since-startup average NInfer itself reports.
from collections import deque as _deque
_history = _deque()  # (timestamp, decode_tokens, prefill_tokens)
_prev_cpu = None  # (timestamp, total_jiffies, idle_jiffies)

# History curve, persisted so a page refresh (or a panel restart, when the
# data directory is on a volume) does not wipe it. The old page kept its
# sparklines in browser memory only, which is exactly why a refresh lost
# them. Points are aggregate rates only — no prompt or generated text.
HISTORY_FILE = os.environ.get("PANEL_HISTORY_FILE", "/app/data/history.jsonl")
HISTORY_RETENTION = int(os.environ.get("PANEL_HISTORY_RETENTION_SECONDS", "86400"))
# (timestamp, decode, prefill, kv_pct, gpu_pct, vram_gb, temp_c, power_w,
#  cpu_pct, disk_read_mbs). The last five use exactly the units the
#  frontend live ticks push into its spark buffers, so seeded and live
#  points share one scale. PCIe has no numeric series (link info only)
#  and stays unseeded.
_series = _deque()
_last_file_prune = 0.0


def _history_load():
    """Load persisted points (within retention) at startup; never fatal."""
    try:
        path = Path(HISTORY_FILE)
        if not path.is_file():
            return
        cutoff = time.time() - HISTORY_RETENTION
        for line in path.read_text().splitlines():
            try:
                p = json.loads(line)
                t = float(p["t"])
                if t >= cutoff:
                    _series.append((t, p.get("d"), p.get("p"), p.get("k"), p.get("g"),
                                    p.get("v"), p.get("e"), p.get("w"), p.get("c"), p.get("s"),
                                    p.get("o"), p.get("a")))
            except (ValueError, KeyError, TypeError):
                continue
    except Exception:
        pass


def _history_append(now, decode, prefill, kv_pct, gpu_pct,
                    vram_gb=None, temp_c=None, power_w=None, cpu_pct=None, disk_read_mbs=None,
                    cum_decode=None, accept_pct=None):
    """Append one point to memory and to the file; prune stale data.

    cum_decode is the committed_decode_tokens counter at poll time: a level,
    not a rate, so the cumulative-output curve reads it directly and the
    downsampler keeps the bucket-end value instead of averaging it.
    accept_pct is the interval speculative-acceptance rate (%) computed
    from /metrics draft-counter deltas; None when no drafts happened."""
    global _last_file_prune
    _series.append((now, decode, prefill, kv_pct, gpu_pct,
                    vram_gb, temp_c, power_w, cpu_pct, disk_read_mbs, cum_decode,
                    accept_pct))
    cutoff = now - HISTORY_RETENTION
    while _series and _series[0][0] < cutoff:
        _series.popleft()
    try:
        path = Path(HISTORY_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(
                {"t": now, "d": decode, "p": prefill, "k": kv_pct, "g": gpu_pct,
                 "v": vram_gb, "e": temp_c, "w": power_w, "c": cpu_pct, "s": disk_read_mbs,
                 "o": cum_decode, "a": accept_pct}) + "\n")
        if now - _last_file_prune > 3600:
            _last_file_prune = now
            with path.open("w") as fh:
                for t, d, p, k, g, v, e, w, c, s, o, a in _series:
                    fh.write(json.dumps({"t": t, "d": d, "p": p, "k": k, "g": g,
                                         "v": v, "e": e, "w": w, "c": c, "s": s,
                                         "o": o, "a": a}) + "\n")
    except Exception:
        pass  # history falls back to memory-only; never break polling


# State-change events, derived only from the poller's own snapshots.
# One entry per transition (time, from-state, to-state, error text if any).
# No prompt or generated text, no new NInfer calls beyond normal polling.
# Capped at 200 entries; a write failure never breaks polling, like history.
EVENTS_FILE = os.environ.get(
    "PANEL_EVENTS_FILE", str(Path(HISTORY_FILE).parent / "events.jsonl"))
_events = _deque()  # (timestamp, from_state, to_state, error_or_None)
_prev_event_state = None
_last_ok = None  # timestamp of the last successful poll; None until first success


def _events_load():
    """Load persisted events at startup; never fatal."""
    try:
        path = Path(EVENTS_FILE)
        if not path.is_file():
            return
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
                _events.append((float(e["t"]), e.get("from"), e.get("to"), e.get("error")))
            except (ValueError, KeyError, TypeError):
                continue
        while len(_events) > 200:
            _events.popleft()
    except Exception:
        pass


def _events_append(now, from_state, to_state, error):
    """Append one transition to memory and to the file."""
    _events.append((now, from_state, to_state, error))
    while len(_events) > 200:
        _events.popleft()
    try:
        path = Path(EVENTS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(
                {"t": now, "from": from_state, "to": to_state, "error": error}) + "\n")
    except Exception:
        pass  # events fall back to memory-only; never break polling


# Panel-observed request history: NInfer has no request-history API, so
# the panel records what its own /slots polling sees. One record per
# slot episode: a slot id that disappears and later reappears is a NEW
# record (slot ids are a reused pool; the panel cannot tell whether it
# is the same request). Statuses are only "active" (in the latest
# processing list) and "ended" (seen before, absent now) — never
# "completed": NInfer does not report why a slot left. Only slot id,
# prompt/cached lengths, window and first/last-seen times are stored;
# prompt text is never in /slots and never stored here. Ended records
# older than one hour are pruned; the file is rewritten only when a
# record is created, ends, or is pruned, so normal polls do no disk I/O.
REQUESTS_FILE = os.environ.get(
    "PANEL_REQUESTS_FILE", str(Path(HISTORY_FILE).parent / "requests.json"))
REQUESTS_WINDOW = 3600
REQUESTS_CAP = 500
_requests = []  # dicts: slot, prompt, cached, n_ctx, first, last, status
_requests_active = {}  # slot id -> record, for currently processing slots
_requests_pending = None  # slot id -> record, loaded active records awaiting first poll


def _requests_load():
    """Load persisted records at startup; never fatal."""
    global _requests_pending
    _requests_pending = {}
    try:
        path = Path(REQUESTS_FILE)
        if not path.is_file():
            return
        cutoff = time.time() - REQUESTS_WINDOW
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
                rec = {"slot": r["slot"], "prompt": int(r.get("prompt") or 0),
                       "cached": int(r.get("cached") or 0), "n_ctx": int(r.get("n_ctx") or 0),
                       "first": float(r["first"]), "last": float(r["last"]),
                       "status": r.get("status") or "ended"}
                if rec["status"] == "ended" and rec["last"] < cutoff:
                    continue
                _requests.append(rec)
                if rec["status"] == "active":
                    _requests_pending[rec["slot"]] = rec
            except (ValueError, KeyError, TypeError):
                continue
        while len(_requests) > REQUESTS_CAP:
            ended = [r for r in _requests if r["status"] == "ended"]
            if not ended:
                break
            _requests.remove(min(ended, key=lambda r: r["last"]))
    except Exception:
        pass


def _requests_save():
    """Rewrite the whole file atomically; never fatal."""
    try:
        path = Path(REQUESTS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            for r in _requests:
                fh.write(json.dumps(r) + "\n")
        tmp.replace(path)
    except Exception:
        pass  # requests fall back to memory-only; never break polling


def _requests_update(details, now):
    """Fold one successful /slots observation into the record list."""
    global _requests_pending
    with _lock:
        dirty = False
        if _requests_pending is not None:
            # First poll after start: a loaded active record that is not
            # processing now ended while the panel was down; its last-seen
            # stays at the last pre-restart observation, not now.
            current = {d["id"] for d in details}
            for slot, rec in list(_requests_pending.items()):
                if slot in current:
                    _requests_active[slot] = rec
                else:
                    rec["status"] = "ended"
                    dirty = True
            _requests_pending = None
        current_ids = {d["id"] for d in details}
        for d in details:
            rec = _requests_active.get(d["id"])
            if rec is None:
                rec = {"slot": d["id"], "prompt": d["prompt_tokens"],
                       "cached": d["cached_tokens"], "n_ctx": d["n_ctx"],
                       "first": now, "last": now, "status": "active"}
                _requests.append(rec)
                _requests_active[d["id"]] = rec
                dirty = True
            else:
                rec["prompt"] = d["prompt_tokens"]
                rec["cached"] = d["cached_tokens"]
                rec["n_ctx"] = d["n_ctx"]
                rec["last"] = now
        for sid in list(_requests_active):
            if sid not in current_ids:
                _requests_active.pop(sid)["status"] = "ended"
                dirty = True
        before = len(_requests)
        _requests[:] = [r for r in _requests
                        if not (r["status"] == "ended" and r["last"] < now - REQUESTS_WINDOW)]
        if len(_requests) != before:
            dirty = True
        while len(_requests) > REQUESTS_CAP:
            ended = [r for r in _requests if r["status"] == "ended"]
            if not ended:
                break
            _requests.remove(min(ended, key=lambda r: r["last"]))
            dirty = True
        if dirty:
            _requests_save()



# Optional NInfer request log (--request-log-jsonl on the serve command):
# when the operator mounts that file into the panel and sets
# PANEL_REQUEST_LOG, the poller tails it and keeps parsed request_done
# records — the same fields NInfer's own tools/monitor reads. This is the
# only source of real per-request output counts, decode speeds and TTFT;
# the log carries counts and timings only, never prompt or output text.
# Parsed records and the tail offset persist next to requests.json, so a
# panel restart resumes instead of re-importing. Without the env var (or
# the file) the feature is off and the observation records above remain
# the past-rows source.
REQUEST_LOG = os.environ.get("PANEL_REQUEST_LOG", "")
REQLOG_FILE = os.environ.get(
    "PANEL_REQLOG_FILE", str(Path(HISTORY_FILE).parent / "request_log.json"))
REQLOG_STATE_FILE = str(Path(HISTORY_FILE).parent / "reqlog_state.json")
REQLOG_CAP = 500
REQLOG_BOOTSTRAP_BYTES = 4_000_000
_reqlog = []  # dicts: id, t, prompt, completion, cached, path, ttft, queue, decode_s, finish
_reqlog_offset = None  # None until loaded or first tail poll


def _reqlog_load():
    """Load parsed records and the saved tail offset; never fatal."""
    global _reqlog_offset
    try:
        path = Path(REQLOG_FILE)
        if path.is_file():
            cutoff = time.time() - REQUESTS_WINDOW
            for line in path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    rec = {"id": r.get("id"), "t": float(r["t"]),
                           "prompt": r.get("prompt"), "completion": r.get("completion"),
                           "cached": r.get("cached"), "path": r.get("path"),
                           "ttft": r.get("ttft"), "queue": r.get("queue"),
                           "decode_s": r.get("decode_s"), "finish": r.get("finish")}
                    if rec["t"] >= cutoff:
                        _reqlog.append(rec)
                except (ValueError, KeyError, TypeError):
                    continue
            _reqlog.sort(key=lambda r: r["t"])
            while len(_reqlog) > REQLOG_CAP:
                _reqlog.pop(0)
    except Exception:
        pass
    try:
        state = json.loads(Path(REQLOG_STATE_FILE).read_text())
        off = int(state.get("offset"))
        if off >= 0:
            _reqlog_offset = off
    except Exception:
        pass


def _reqlog_save():
    """Persist records and offset; never fatal."""
    try:
        path = Path(REQLOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            for r in _reqlog:
                fh.write(json.dumps(r) + "\n")
        tmp.replace(path)
        Path(REQLOG_STATE_FILE).write_text(
            json.dumps({"offset": _reqlog_offset or 0}))
    except Exception:
        pass


def _reqlog_parse(line):
    """One JSONL line -> a request_done record, or None. Mirrors the field
    usage of NInfer's own tools/monitor: counts and timings only."""
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if rec.get("event") != "request_done":
        return None
    ts = rec.get("timestamp_unix_ms")
    if not isinstance(ts, (int, float)):
        return None
    res = rec.get("result") or {}
    timings = rec.get("timings_seconds") or {}
    eng = rec.get("engine_timing") or {}

    def num(d, k):
        v = d.get(k)
        return v if isinstance(v, (int, float)) else None

    return {"id": (rec.get("request") or {}).get("request_id"),
            "t": ts / 1000.0,
            "prompt": num(res, "prompt_tokens"),
            "completion": num(res, "completion_tokens"),
            "cached": num(res, "prefix_cache_hit_tokens"),
            "path": res.get("prefix_reuse_path"),
            "ttft": num(timings, "ttft"),
            "queue": num(eng, "queue_wait_seconds"),
            "decode_s": num(timings, "decode"),
            "finish": res.get("finish_reason")}


def _reqlog_poll():
    """Tail the request log once; fail-soft like every other source."""
    global _reqlog_offset
    if not REQUEST_LOG:
        return
    try:
        path = Path(REQUEST_LOG)
        size = path.stat().st_size
        if _reqlog_offset is None:
            # First sight: bootstrap from the tail so a huge log is not
            # imported whole (records are windowed to the last hour).
            start = max(0, size - REQLOG_BOOTSTRAP_BYTES)
            skip_partial = start > 0
        elif _reqlog_offset > size:
            start, skip_partial = 0, False  # rotated or truncated
        else:
            start, skip_partial = _reqlog_offset, False
        if size == start:
            _reqlog_offset = start
            return
        with path.open("rb") as fh:
            fh.seek(start)
            data = fh.read(min(size - start, 16_000_000))
        if skip_partial:
            nl = data.find(b"\n")
            if nl < 0:
                return
            start += nl + 1
            data = data[nl + 1:]
        end = data.rfind(b"\n")
        if end < 0:
            if len(data) >= 16_000_000:
                _reqlog_offset = size  # one gigantic line: skip it
            return
        chunk = data[:end]
        new_offset = start + end + 1
        now = time.time()
        added = []
        for raw in chunk.split(b"\n"):
            if raw.strip():
                rec = _reqlog_parse(raw.decode("utf-8", "replace"))
                if rec is not None:
                    added.append(rec)
        changed = new_offset != _reqlog_offset
        _reqlog_offset = new_offset
        if added:
            with _lock:
                _reqlog.extend(added)
                _reqlog.sort(key=lambda r: r["t"])
                _reqlog[:] = [r for r in _reqlog
                              if r["t"] >= now - REQUESTS_WINDOW]
                while len(_reqlog) > REQLOG_CAP:
                    _reqlog.pop(0)
            changed = True
        if changed:
            _reqlog_save()
    except Exception:
        pass


# GUI-editable NInfer address. Precedence: GUI-saved value (settings.json
# in the data volume) > NINFER_BASE_URL env > built-in default. The compose
# file no longer hardcodes an address: first run uses the env/default, and
# the operator changes it from the panel UI instead of editing compose.
SETTINGS_FILE = os.environ.get(
    "PANEL_SETTINGS_FILE", str(Path(HISTORY_FILE).parent / "settings.json"))
_settings = {"ninfer_base_url": None}


def _valid_base_url(url):
    """http(s)://host[:port] only: no userinfo, no fragment, no path query."""
    try:
        from urllib.parse import urlparse
        u = urlparse(url.strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            return None
        if u.username or u.password or u.fragment or u.query:
            return None
        if u.path not in ("", "/"):
            return None
        if u.port is not None and not (1 <= u.port <= 65535):
            return None
        return url.strip().rstrip("/")
    except Exception:
        return None


def _current_base():
    with _lock:
        saved = _settings.get("ninfer_base_url")
    return saved or BASE


def _settings_load():
    try:
        path = Path(SETTINGS_FILE)
        if path.is_file():
            data = json.loads(path.read_text())
            url = _valid_base_url(data.get("ninfer_base_url", ""))
            if url:
                _settings["ninfer_base_url"] = url
    except Exception:
        pass


def _settings_save(url):
    path = Path(SETTINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ninfer_base_url": url}))
    tmp.replace(path)
    with _lock:
        _settings["ninfer_base_url"] = url
    global _prev
    _prev = None
    _history.clear()


# NInfer console (ninfer-console, read-only management preview).
#
# Nothing here is seeded with any deployment's configuration. The
# config on record is, in priority order:
#   1. a record the operator explicitly saved (console_current.json),
#   2. a record derived from the live engine snapshot — only the fields
#      NInfer itself reports (model, max context, max concurrency) —
#      labelled as snapshot-derived and never silently persisted,
#   3. a blank config.
# The model library is operator data: entries from a models JSON file
# (capabilities are declared there, nowhere else) merged with whatever
# *.ninfer files are actually present in the models directory. Host
# limits are measured at runtime (host RAM via /host/proc, GPU VRAM
# via nvidia-smi), never constants baked in for one machine.
# _console_payload() cross-checks the fields /v1/load reports and
# flags a mismatch instead of hiding it.
CONSOLE_VERSION = "0.3.0"
_DATA_DIR = Path(HISTORY_FILE).parent
CONSOLE_CURRENT_FILE = os.environ.get(
    "PANEL_CONSOLE_FILE", str(_DATA_DIR / "console_current.json"))
CONSOLE_DRAFT_FILE = os.environ.get(
    "PANEL_CONSOLE_DRAFT_FILE", str(_DATA_DIR / "console_draft.json"))
CONSOLE_HISTORY_FILE = os.environ.get(
    "PANEL_CONSOLE_HISTORY_FILE", str(_DATA_DIR / "console_history.json"))
MODELS_DIR = os.environ.get("PANEL_MODELS_DIR", "")
# Host-side models directory shown in the console's compose preview.
# Generic placeholder by default: the real host path is deployment
# information and is only ever supplied through the environment.
MODELS_HOST_DIR = os.environ.get("PANEL_MODELS_HOST_DIR", "/path/to/models")
# Operator-maintained data files (see examples/ in the repo). Missing
# files are normal — the library then comes from discovery alone.
MODELS_FILE = os.environ.get("PANEL_MODELS_FILE", str(_DATA_DIR / "models.json"))
PRESETS_FILE = os.environ.get("PANEL_PRESETS_FILE", str(_DATA_DIR / "presets.json"))
try:
    _HOST_CACHE_MAX_MIB_ENV = int(os.environ.get("PANEL_HOST_CACHE_MAX_MIB", "") or 0) or None
except ValueError:
    _HOST_CACHE_MAX_MIB_ENV = None
_console_current = None  # {"config", "source", "saved_at"} once known
_console_draft = None    # dict config, or None
_console_history = []    # [{"t", "source_zh", "source_en", "config"}]

_HISTORY_SOURCES = {
    "import": ("导入的配置", "Imported config"),
    "manual": ("手动保存为当前记录", "Saved as the current record"),
    "live": ("实测快照推导", "Derived from the live snapshot"),
}


def _read_json_file(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def _save_json_file(path, obj):
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj))
        tmp.replace(path)
        return True
    except Exception:
        return False  # records fall back to memory-only; never break the panel


def _console_load():
    global _console_current, _console_draft, _console_history
    data = _read_json_file(CONSOLE_CURRENT_FILE)
    if isinstance(data, dict) and isinstance(data.get("config"), dict):
        _console_current = {
            "config": console.sanitize_config(data["config"]),
            "source": data.get("source") or "saved",
            "saved_at": data.get("saved_at")}
    data = _read_json_file(CONSOLE_DRAFT_FILE)
    if isinstance(data, dict) and isinstance(data.get("config"), dict):
        _console_draft = console.sanitize_config(data["config"])
    data = _read_json_file(CONSOLE_HISTORY_FILE)
    if isinstance(data, list):
        hist = []
        for rec in data:
            if isinstance(rec, dict) and isinstance(rec.get("config"), dict):
                hist.append({"t": rec.get("t"),
                             "source_zh": rec.get("source_zh") or "",
                             "source_en": rec.get("source_en") or "",
                             "config": console.sanitize_config(rec["config"])})
        _console_history = hist[-50:]


def _console_save_draft(cfg):
    """Persist or clear the unsaved-changes draft (panel-local only)."""
    global _console_draft
    _console_draft = cfg
    if cfg is None:
        try:
            path = Path(CONSOLE_DRAFT_FILE)
            if path.is_file():
                path.unlink()
        except Exception:
            pass
        return
    _save_json_file(CONSOLE_DRAFT_FILE, {"config": cfg, "saved_at": time.time()})


def _console_append_history(cfg, source_key):
    zh, en = _HISTORY_SOURCES.get(source_key, (source_key, source_key))
    _console_history.append({"t": time.time(), "source_zh": zh,
                             "source_en": en, "config": cfg})
    del _console_history[:-50]
    _save_json_file(CONSOLE_HISTORY_FILE, _console_history)


def _console_record_current(cfg, source_key):
    """Save the operator-declared current record (panel-local only;
    applies nothing to the engine)."""
    global _console_current
    _console_current = {"config": cfg, "source": source_key,
                        "saved_at": time.time()}
    _save_json_file(CONSOLE_CURRENT_FILE,
                    {"config": cfg, "source": source_key,
                     "saved_at": _console_current["saved_at"]})
    _console_append_history(cfg, source_key)


def _models_dir_scan():
    """Enumerate *.ninfer in PANEL_MODELS_DIR, if configured and readable.
    Returns (configured, {filename: size_bytes}). Absence of the mount
    is reported as not-configured — discovered models then simply do
    not appear, and nothing is invented to fill the gap."""
    if not MODELS_DIR:
        return False, {}
    try:
        found = {}
        for p in Path(MODELS_DIR).glob("*.ninfer"):
            try:
                found[p.name] = p.stat().st_size
            except Exception:
                continue
        return True, found
    except Exception:
        return True, {}


def _operator_data():
    """(models, presets, problems): operator files + discovery, merged.

    Read fresh on every payload: the files are small, and an operator
    editing them expects the page to reflect it without a restart.
    A missing file is not a problem; a malformed one is reported."""
    problems = []
    registered = []
    raw = None
    try:
        if Path(MODELS_FILE).is_file():
            raw = Path(MODELS_FILE).read_text()
    except Exception as exc:
        problems.append(f"models file unreadable: {exc}")
    if raw is not None:
        registered, probs = console.load_models_json(raw)
        problems.extend(probs)
    configured, found = _models_dir_scan()
    models = console.merge_models(registered, found)
    presets = []
    raw = None
    try:
        if Path(PRESETS_FILE).is_file():
            raw = Path(PRESETS_FILE).read_text()
    except Exception as exc:
        problems.append(f"presets file unreadable: {exc}")
    if raw is not None:
        presets, probs = console.load_presets_json(raw, models)
        problems.extend(probs)
    return models, presets, problems, configured, found


def _console_limits():
    """Runtime-measured deployment limits for validation.

    host_cache_max_mib: an explicit env override wins; otherwise a
    quarter of measured host RAM (host caches must leave the OS and
    the engine's own working set ample room); unmeasurable -> None
    (check skipped, never guessed). vram_budget_gb: the first GPU's
    total memory as reported by nvidia-smi, else None."""
    host_max = _HOST_CACHE_MAX_MIB_ENV
    if host_max is None:
        hm = _host_mem()
        if hm.get("available") and hm.get("total_mb"):
            host_max = int(hm["total_mb"] // 4)
    vram = None
    with _lock:
        gpu = (_snapshot.get("gpu") or {})
    if gpu.get("available") and gpu.get("cards"):
        total = gpu["cards"][0].get("mem_total_mb")
        if total:
            vram = total / 1024.0
    return {"host_cache_max_mib": host_max, "vram_budget_gb": vram}


def _console_current_record(models):
    """The config on record: saved record, else snapshot-derived."""
    global _console_current
    if _console_current is not None:
        return _console_current
    with _lock:
        snap = dict(_snapshot)
    cfg = console.blank_config()
    source = "blank"
    if snap.get("ninfer_ok"):
        source = "live"
        live_model = snap.get("model")
        if live_model:
            m = console.model_by_file(models, str(live_model))
            if m is None:
                fname = str(live_model)
                if not fname.endswith(".ninfer"):
                    fname += ".ninfer"
                m = console.model_by_file(models, fname)
            if m is not None:
                cfg["model"] = m["id"]
        ctx = snap.get("context") or {}
        if isinstance(ctx.get("max_context"), (int, float)) and ctx["max_context"]:
            cfg["max_context"] = int(ctx["max_context"])
        if isinstance(ctx.get("max_concurrency"), (int, float)) and ctx["max_concurrency"]:
            cfg["max_concurrency"] = int(ctx["max_concurrency"])
    _console_current = {"config": cfg, "source": source, "saved_at": None}
    return _console_current


def _console_payload():
    models, presets, problems, configured, found = _operator_data()
    record = _console_current_record(models)
    current_cfg = record["config"]
    out_models = []
    for m in models:
        entry = dict(m)
        entry["specs"] = console.available_specs(m)
        entry["is_current"] = (m["id"] == current_cfg.get("model"))
        if configured and m.get("file"):
            entry["present"] = m["file"] in found
        elif m.get("source") == "discovered":
            entry["present"] = True
        else:
            entry["present"] = None
        entry["size_measured"] = m.get("source") in ("both", "discovered")
        out_models.append(entry)
    history = []
    for rec in _console_history:
        t = rec.get("t")
        history.append({
            "t_label": time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "",
            "source_zh": rec.get("source_zh") or "",
            "source_en": rec.get("source_en") or "",
            "current": rec["config"] == current_cfg,
            "config": rec["config"]})
    with _lock:
        snap = dict(_snapshot)
    ctx = snap.get("context") or {}
    checks = []
    for key, live in (("max_context", ctx.get("max_context")),
                      ("max_concurrency", ctx.get("max_concurrency"))):
        recorded = current_cfg.get(key)
        checks.append({"key": key, "recorded": recorded, "live": live,
                       "match": (live == recorded) if isinstance(live, (int, float)) and live else None})
    live_model = snap.get("model")
    model_match = None
    cur_model = console.model_by_id(models, current_cfg.get("model") or "")
    if live_model and cur_model:
        candidates = {cur_model.get("file"), cur_model["id"],
                      console._stem(cur_model.get("file") or ""),
                      current_cfg.get("model_id")}
        if live_model in candidates:
            model_match = True
        else:
            other = console.model_by_file(models, str(live_model))
            if other is not None:
                model_match = False
    return {
        "console_version": CONSOLE_VERSION,
        "readonly": True,
        "schema": console.PARAM_SCHEMA,
        "models": out_models,
        "presets": presets,
        "history": history,
        "current": {"config": current_cfg, "source": record["source"],
                    "saved_at": record["saved_at"]},
        "draft": _console_draft,
        "models_dir": {"configured": configured, "found": len(found)},
        "registry_problems": problems,
        "limits": _console_limits(),
        "live": {"ninfer_ok": bool(snap.get("ninfer_ok")),
                 "state": snap.get("state"),
                 "model": live_model,
                 "model_match": model_match,
                 "in_flight": (snap.get("requests") or {}).get("admitted"),
                 "checks": checks},
    }


def _console_preview(cfg):
    models, _presets, _problems, _configured, _found = _operator_data()
    record = _console_current_record(models)
    errors, warnings = console.validate_config(
        cfg, models=models, limits=_console_limits())
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "diff": console.diff_config(record["config"], cfg, models=models),
            "command": console.build_command(cfg),
            "compose_preview": console.build_compose_preview(
                cfg, models=models, host_models_dir=MODELS_HOST_DIR),
            "image_ref": console.resolve_image_ref(cfg)}


# Version + upgrade check. The panel reports its own VERSION and, once an
# hour, asks the public GitHub Releases API whether a newer release exists.
# It sends nothing (no token, no telemetry, no panel data); it only reads
# the public latest-release record. Set PANEL_UPDATE_CHECK=0 to disable it
# (e.g. fully offline hosts); a failed check is "unavailable", never an error.
def _panel_version():
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip()
    except Exception:
        return "0.0.0-dev"


def _version_tuple(v):
    out = []
    for part in v.lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _is_newer(latest, current):
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return False


_UPDATE_REPO = os.environ.get("PANEL_UPDATE_REPO", "izerok/ninfer-live-panel")
_UPDATE_ENABLED = os.environ.get("PANEL_UPDATE_CHECK", "1") != "0"
_update_cache = {"at": 0.0, "data": None}


def _update_check(force=False):
    current = _panel_version()
    if not _UPDATE_ENABLED:
        return {"enabled": False, "current": current, "latest": None,
                "update_available": False, "url": None}
    now = time.time()
    if not force and _update_cache["data"] is not None and now - _update_cache["at"] < 3600:
        return _update_cache["data"]
    result = {"enabled": True, "current": current, "latest": None,
              "update_available": False, "url": None, "checked_at": now}
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{_UPDATE_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "ninfer-live-panel"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        tag = data.get("tag_name")
        if tag:
            result["latest"] = tag
            result["url"] = data.get("html_url")
            result["update_available"] = _is_newer(tag, current)
    except Exception:
        pass  # offline / no release yet / API hiccup: banner simply stays hidden
    _update_cache["at"] = now
    _update_cache["data"] = result
    return result


def _get(path, timeout=3):
    req = urllib.request.Request(_current_base() + path)
    if API_KEY:
        req.add_header("Authorization", "Bearer " + API_KEY)
    req.add_header("Accept", "application/json, text/plain")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _test_connection(base):
    """Reachability check for a user-supplied address, WITHOUT the API key:
    _get() attaches the Bearer token, so it must never be pointed at an
    address the operator has not saved yet. /health needs no auth, and a
    200 there is all a 'can I reach it' test needs."""
    try:
        req = urllib.request.Request(
            base + "/health", headers={"Accept": "application/json, text/plain"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = None
            try:
                status = json.loads(body).get("status") if body.strip().startswith("{") else None
            except Exception:
                pass
            if resp.status != 200 or status not in (None, "ok"):
                return {"ok": False, "http": resp.status, "status": status,
                        "error": f"/health -> HTTP {resp.status} status={status}"}
            return {"ok": True, "http": resp.status, "status": status}
    except Exception as exc:
        return {"ok": False, "http": None, "status": None, "error": str(exc)[:200]}


def _num(obj, *keys, default=0):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def _parse_metrics(text):
    """Parse the few Prometheus series the panel shows. Unknown / missing
    series stay absent (None) instead of being invented as zero."""
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, value = line.partition(" ")
        base = name.split("{")[0]
        try:
            out[base] = float(value)
        except ValueError:
            pass
    return out


def _gpu():
    """nvidia-smi if the toolkit injected it, else an explicit unavailable."""
    try:
        proc = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,pcie.link.gen.current,pcie.link.width.current",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"available": False, "reason": "nvidia-smi not found in this container"}
    if proc.returncode != 0:
        return {"available": False,
                "reason": (proc.stderr or proc.stdout).strip()[:200] or "nvidia-smi failed"}
    cards = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            card = {
                "name": parts[0],
                "util_percent": float(parts[1]),
                "mem_used_mb": float(parts[2]),
                "mem_total_mb": float(parts[3]),
                "temp_c": float(parts[4]),
                "power_w": float(parts[5]),
            }
            # power.limit / PCIe link may report "[N/A]" on some cards.
            for key, idx in (("power_limit_w", 6), ("pcie_gen", 7), ("pcie_width", 8)):
                if len(parts) > idx:
                    try:
                        card[key] = float(parts[idx])
                    except ValueError:
                        card[key] = None
            cards.append(card)
        except ValueError:
            continue
    if not cards:
        return {"available": False, "reason": "nvidia-smi returned no GPU rows"}
    return {"available": True, "cards": cards}


def _host_cpu():
    """Host CPU utilisation from /host/proc/stat deltas. Without the host
    /proc mount this is unavailable — the container's own numbers are not
    substituted, for the same reason as host memory."""
    global _prev_cpu
    path = Path("/host/proc/stat")
    if not path.is_file():
        return {"available": False,
                "reason": "host /proc not mounted (see README: /proc:/host/proc:ro)"}
    try:
        parts = path.read_text().splitlines()[0].split()[1:]
        vals = [int(x) for x in parts]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    except Exception as exc:
        return {"available": False, "reason": f"could not parse /host/proc/stat: {exc}"}
    now = time.time()
    util = None
    if _prev_cpu is not None:
        prev_t, prev_total, prev_idle = _prev_cpu
        dt_total = total - prev_total
        dt_idle = idle - prev_idle
        if dt_total > 0:
            util = (1.0 - dt_idle / dt_total) * 100.0
    _prev_cpu = (now, total, idle)
    model = cores = None
    cpuinfo = Path("/host/proc/cpuinfo")
    if cpuinfo.is_file():
        try:
            text = cpuinfo.read_text()
            for line in text.splitlines():
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
            cores = text.count("processor\t:")
        except Exception:
            pass
    return {"available": True, "util_percent": util, "model": model, "cores": cores}


_prev_disk = None  # (timestamp, sectors_read, sectors_written)


def _host_disk():
    """Host disk throughput from /host/proc/diskstats deltas (whole disks
    only: sd/vd/nvme/mmcblk devices, not partitions, loop or ram devices).
    Without the host /proc mount this is unavailable."""
    global _prev_disk
    path = Path("/host/proc/diskstats")
    if not path.is_file():
        return {"available": False,
                "reason": "host /proc not mounted (see README: /proc:/host/proc:ro)"}
    import re as _re
    read_sectors = write_sectors = 0
    try:
        for line in path.read_text().splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            if not _re.match(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$", name):
                continue
            read_sectors += int(parts[5])
            write_sectors += int(parts[9])
    except Exception as exc:
        return {"available": False, "reason": f"could not parse /host/proc/diskstats: {exc}"}
    now = time.time()
    read_mbs = write_mbs = None
    if _prev_disk is not None:
        prev_t, prev_r, prev_w = _prev_disk
        dt = now - prev_t
        if dt > 0 and read_sectors >= prev_r and write_sectors >= prev_w:
            read_mbs = (read_sectors - prev_r) * 512 / 1048576 / dt
            write_mbs = (write_sectors - prev_w) * 512 / 1048576 / dt
    _prev_disk = (now, read_sectors, write_sectors)
    return {"available": True, "read_mbs": read_mbs, "write_mbs": write_mbs}


def _host_mem():
    """Host memory only when host /proc is mounted at /host/proc.
    The container's own /proc/meminfo is deliberately never used."""
    path = Path("/host/proc/meminfo")
    if not path.is_file():
        return {"available": False,
                "reason": "host /proc not mounted (see README: /proc:/host/proc:ro)"}
    total = avail = None
    for line in path.read_text().splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])  # KiB
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1])
    if total is None:
        return {"available": False, "reason": "could not parse /host/proc/meminfo"}
    used = total - (avail or 0)
    return {"available": True, "total_mb": total / 1024.0, "used_mb": used / 1024.0,
            "used_percent": (used / total * 100.0) if total else None}


def _poll_once():
    global _prev, _last_ok, _prev_draft
    now = time.time()
    snap = {"as_of": now, "ninfer_base_url": _current_base(), "ninfer_ok": False,
            "state": "offline", "error": None, "last_ok": _last_ok}
    try:
        status, health_body = _get("/health", timeout=3)
        health = json.loads(health_body) if health_body.strip().startswith("{") else {}
        snap["health"] = {"http": status, "status": health.get("status"),
                          "version": health.get("version")}
        if status != 200 or health.get("status") not in (None, "ok"):
            snap["error"] = f"/health -> HTTP {status} status={health.get('status')}"
            return snap
    except Exception as exc:  # unreachable, timeout, bad JSON: all mean offline
        snap["error"] = f"cannot reach {_current_base()}/health: {exc}"
        return snap

    try:
        _, load_body = _get("/v1/load", timeout=3)
        load = json.loads(load_body)
    except Exception as exc:
        snap["error"] = f"/health ok but /v1/load failed: {exc}"
        return snap

    snap["ninfer_ok"] = True
    _last_ok = now
    snap["last_ok"] = now
    req = load.get("requests", {}) or {}
    occ = load.get("occupancy", {}) or {}
    cap = load.get("capacity", {}) or {}
    ctr = load.get("counters", {}) or {}
    running = int(_num(req, "running"))
    waiting = int(_num(req, "waiting"))
    prefilling = int(_num(req, "prefilling"))
    decode_ready = int(_num(req, "decode_ready"))
    admitted = int(_num(req, "admitted"))
    decode_tokens = int(_num(ctr, "committed_decode_tokens"))
    prefill_tokens = int(_num(ctr, "computed_prefill_tokens"))
    kv_tokens = int(_num(occ, "device_main_kv_tokens"))
    kv_cap = int(_num(cap, "kv_capacity_tokens"))

    # Instant rates from counter deltas; a counter reset yields None, not a spike.
    decode_rate = prefill_rate = window = None
    if _prev is not None:
        prev_t, prev_d, prev_p = _prev
        dt = now - prev_t
        if dt > 0 and decode_tokens >= prev_d and prefill_tokens >= prev_p:
            decode_rate = (decode_tokens - prev_d) / dt
            prefill_rate = (prefill_tokens - prev_p) / dt
            window = dt
    _prev = (now, decode_tokens, prefill_tokens)
    _history.append((now, decode_tokens, prefill_tokens))
    while _history and now - _history[0][0] > 900:
        _history.popleft()
    rolling = {"decode_tok_s": None, "prefill_tok_s": None, "window_seconds": None}
    if len(_history) >= 2:
        t0, d0, p0 = _history[0]
        span = now - t0
        if span >= 30:
            rolling = {"decode_tok_s": (decode_tokens - d0) / span,
                       "prefill_tok_s": (prefill_tokens - p0) / span,
                       "window_seconds": span}

    if waiting > 0 and running == 0:
        state = "queued"
    elif prefilling > 0 and decode_ready == 0:
        state = "warming"
    elif decode_ready > 0 or running > 0:
        state = "generating"
    else:
        state = "idle"

    snap.update({
        "state": state,
        "model": load.get("model"),
        "uptime_seconds": _num(load, "uptime_seconds"),
        "requests": {"admitted": admitted, "running": running,
                     "waiting": waiting, "prefilling": prefilling,
                     "decode_ready": decode_ready,
                     "materializing": int(_num(req, "materializing"))},
        "kv": {"tokens": kv_tokens, "capacity_tokens": kv_cap,
               "used_percent": (kv_tokens / kv_cap * 100.0) if kv_cap else None},
        # Context window: the one thing the first version never showed.
        # max_context comes from /v1/load capacity. KV tokens are the
        # device KV cache
        # occupancy — related to context, but not the same as a request's
        # prompt length, so the two are shown separately, never merged
        # into one invented "context used" number.
        "context": {
            "max_context": int(_num(cap, "max_context")),
            "max_concurrency": int(_num(cap, "max_concurrency")),
            "kv_tokens": kv_tokens,
            "kv_capacity_tokens": kv_cap,
            "kv_used_percent": (kv_tokens / kv_cap * 100.0) if kv_cap else None,
            "device_state_slots_used": int(_num(occ, "device_state_slots")),
            "device_state_slots_capacity": int(_num(cap, "device_state_slots")),
            "host_state_slots_used": int(_num(occ, "host_state_slots")),
            "host_state_slots_capacity": int(_num(cap, "host_state_slots")),
            "host_kv_bytes": int(_num(occ, "host_kv_bytes")),
            "host_kv_capacity_bytes": int(_num(cap, "host_kv_bytes")),
        },
        "counters": {"decode_tokens": (decode_tokens if ctr.get("committed_decode_tokens") is not None else None),
                     "prefill_tokens": prefill_tokens,
                     "reused_prompt_tokens": int(_num(ctr, "reused_prompt_tokens")),
                     "decode_rounds": int(_num(ctr, "decode_rounds"))},
        "instant": {"decode_tok_s": decode_rate, "prefill_tok_s": prefill_rate,
                    "window_seconds": window},
        "rolling": rolling,
    })
    # Cumulative averages from /stats, best effort — absent is absent.
    try:
        _, stats_body = _get("/stats", timeout=3)
        stats = json.loads(stats_body)
        tps = stats.get("throughput_tokens_per_second") or {}
        snap["cumulative_avg"] = {
            "decode_tok_s": tps.get("decode") if isinstance(tps, dict) else None,
            "prefill_tok_s": tps.get("prefill") if isinstance(tps, dict) else None,
            "peak_admitted": stats.get("requests", {}).get("peak_admitted"),
            "queue_depth": stats.get("queue", {}).get("depth"),
        }
        # /stats throughput block shape varies by build; keep raw keys the UI may use.
        snap["cumulative_avg"]["raw_throughput_block"] = tps
        # Host KV fallback: some builds leave host KV empty in /v1/load
        # but report it in /stats. Fill from /stats ONLY when /v1/load
        # left it empty, so a working primary source is never overridden.
        try:
            stats_cap = int(_num(stats.get("capacity", {}) or {}, "host_kv_bytes"))
            if stats_cap > 0:
                ctx = snap.get("context") or {}
                if not ctx.get("host_kv_capacity_bytes"):
                    ctx["host_kv_bytes"] = int(_num(
                        stats.get("occupancy", {}) or {}, "host_kv_bytes"))
                    ctx["host_kv_capacity_bytes"] = stats_cap
                    ctx["host_kv_source"] = "stats"
                    snap["context"] = ctx
        except Exception:
            pass
        # Context-cache pressure / reuse paths / transfers (hybrid builds).
        try:
            cc = stats.get("context_cache", {}) or {}
            pr = cc.get("pressure", {}) or {}

            def _opt(d, k):
                v = d.get(k)
                return v if isinstance(v, (int, float)) else None

            ev_parts = [v for v in (pr.get("private_owners_evicted"),
                                    pr.get("shared_owners_evicted"))
                        if isinstance(v, (int, float))]
            sel = cc.get("selections", {}) or {}
            sel = {k: v for k, v in sel.items()
                   if isinstance(v, (int, float))}

            def _xfer(direction):
                tot, seen = 0.0, False
                for block in ("main_kv_transfers", "backend_kv_transfers"):
                    leg = (cc.get(block, {}) or {}).get(direction, {}) or {}
                    b = leg.get("bytes")
                    if isinstance(b, (int, float)):
                        tot += b
                        seen = True
                return tot if seen else None

            snap["cache_extra"] = {
                "evicted": sum(ev_parts) if ev_parts else None,
                "demoted": _opt(pr, "private_owners_demoted"),
                "spill_pages": _opt(pr, "spill_pages"),
                "checkpoints_dropped": _opt(pr, "checkpoints_dropped"),
                "selections": sel or None,
                "transfer_d2h_bytes": _xfer("d2h"),
                "transfer_h2d_bytes": _xfer("h2d"),
            }
        except Exception:
            snap["cache_extra"] = None
    except Exception:
        snap["cumulative_avg"] = None
        snap["cache_extra"] = None
    accept_pct = None
    try:
        _, metrics_body = _get("/metrics", timeout=3)
        m = _parse_metrics(metrics_body)
        draft_total = m.get("ninfer:draft_tokens_total")
        draft_acc = m.get("ninfer:draft_accepted_tokens_total")
        if draft_total is not None and draft_acc is not None:
            if _prev_draft is not None:
                _pt, _ptot, _pacc = _prev_draft
                if (now - _pt) > 0 and draft_total > _ptot and draft_acc >= _pacc:
                    accept_pct = ((draft_acc - _pacc)
                                  / (draft_total - _ptot) * 100.0)
            _prev_draft = (now, draft_total, draft_acc)
        snap["metrics"] = {
            "requests_total": m.get("ninfer:requests_total"),
            "requests_failed_total": m.get("ninfer:requests_failed_total"),
            "requests_rejected_total": m.get("ninfer:requests_rejected_total"),
            "kv_cache_usage_ratio": m.get("llamacpp:kv_cache_usage_ratio"),
            "draft_accept_rate": (draft_acc / draft_total) if draft_total else None,
            "avg_decode_tok_s": m.get("llamacpp:predicted_tokens_seconds"),
            "avg_prefill_tok_s": m.get("llamacpp:prompt_tokens_seconds"),
        }
    except Exception:
        snap["metrics"] = None
    try:
        _, slots_body = _get("/slots", timeout=3)
        slots = json.loads(slots_body)
        if isinstance(slots, list):
            processing = [s for s in slots if s.get("is_processing")]
            # Per-request context length as NInfer reports it: prompt tokens
            # (cached tokens are the reused prefix part of that prompt).
            # Generated tokens so far are not in /slots, so this is prompt
            # length only — the UI labels it as such.
            details = [{"id": s.get("id"),
                        "prompt_tokens": int(s.get("n_prompt_tokens") or 0),
                        "cached_tokens": int(s.get("n_prompt_tokens_cache") or 0),
                        "n_ctx": int(s.get("n_ctx") or 0)}
                       for s in processing]
            snap["slots"] = {"total": len(slots),
                             "processing": len(processing),
                             "retained": sum(1 for s in slots if s.get("retained")),
                             "processing_details": details,
                             "processing_prompt_tokens": sum(d["prompt_tokens"] for d in details),
                             "max_processing_prompt_tokens": max(
                                 (d["prompt_tokens"] for d in details), default=0)}
            ctx = snap.get("context") or {}
            max_ctx = ctx.get("max_context") or 0
            ctx["max_processing_prompt_tokens"] = snap["slots"]["max_processing_prompt_tokens"]
            ctx["processing_prompt_tokens"] = snap["slots"]["processing_prompt_tokens"]
            ctx["max_prompt_share_percent"] = (
                snap["slots"]["max_processing_prompt_tokens"] / max_ctx * 100.0
                if max_ctx else None)
            snap["context"] = ctx
            _requests_update(details, now)
    except Exception:
        pass
    snap["gpu"] = _gpu()
    snap["host_memory"] = _host_mem()
    snap["host_cpu"] = _host_cpu()
    snap["host_disk"] = _host_disk()
    gpu_util = None
    vram_gb = temp_c = power_w = None
    if snap["gpu"].get("available") and snap["gpu"].get("cards"):
        card0 = snap["gpu"]["cards"][0]
        gpu_util = card0.get("util_percent")
        vram_gb = card0.get("mem_used_mb") / 1024.0 if card0.get("mem_used_mb") is not None else None
        temp_c = card0.get("temp_c")
        power_w = card0.get("power_w")
    cpu_pct = snap["host_cpu"].get("util_percent") if snap["host_cpu"].get("available") else None
    disk_read = snap["host_disk"].get("read_mbs") if snap["host_disk"].get("available") else None
    # Cumulative counter only when NInfer actually reported it; a missing
    # counter stays None (no invented zero) so the cumulative curve gaps.
    _cum = ctr.get("committed_decode_tokens")
    _cum = int(_cum) if _cum is not None else None
    _history_append(now, decode_rate, prefill_rate,
                    (kv_tokens / kv_cap * 100.0) if kv_cap else None, gpu_util,
                    vram_gb, temp_c, power_w, cpu_pct, disk_read, _cum,
                    accept_pct)
    return snap


def _poller():
    global _prev_event_state
    while True:
        try:
            snap = _poll_once()
        except Exception as exc:  # never kill the poller on a bad sample
            snap = {"ninfer_ok": False, "state": "offline", "error": str(exc),
                    "as_of": time.time(), "ninfer_base_url": _current_base(),
                    "last_ok": _last_ok}
        eff = snap.get("state") if snap.get("ninfer_ok") else "offline"
        if _prev_event_state is not None and eff != _prev_event_state:
            _events_append(snap.get("as_of", time.time()), _prev_event_state, eff, snap.get("error"))
        _prev_event_state = eff
        with _lock:
            _snapshot.clear()
            _snapshot.update(snap)
        _reqlog_poll()
        time.sleep(POLL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype, headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                url = _valid_base_url(data.get("ninfer_base_url", ""))
            except Exception:
                url = None
            if not url:
                self._send(400, json.dumps(
                    {"error": "invalid ninfer_base_url: expected http(s)://host[:port], no userinfo, no path"}),
                    "application/json")
                return
            try:
                _settings_save(url)
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}), "application/json")
                return
            self._send(200, json.dumps(
                {"ninfer_base_url": url, "source": "gui"}), "application/json")
            return
        if self.path in ("/api/console/preview", "/api/console/import",
                         "/api/console/draft", "/api/console/current",
                         "/api/console/import-bundle"):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                data = None
            if not isinstance(data, dict):
                self._send(400, json.dumps({"error": "expected a JSON object"}),
                           "application/json")
                return
            if self.path == "/api/console/preview":
                cfg = data.get("config")
                if not isinstance(cfg, dict):
                    self._send(400, json.dumps(
                        {"error": "config must be an object"}), "application/json")
                    return
                self._send(200, json.dumps(_console_preview(cfg)),
                           "application/json")
                return
            if self.path == "/api/console/import":
                cmd = data.get("command")
                if not isinstance(cmd, list) or not all(
                        isinstance(x, str) for x in cmd):
                    self._send(400, json.dumps(
                        {"error": "command must be an array of strings"}),
                        "application/json")
                    return
                models, _p, _pr, _c, _f = _operator_data()
                cfg, unparsed, model_file = console.parse_command(cmd, models=models)
                errors, warnings = console.validate_config(
                    cfg, models=models, limits=_console_limits())
                self._send(200, json.dumps(
                    {"config": cfg, "unparsed": unparsed,
                     "model_file": model_file,
                     "errors": errors, "warnings": warnings}),
                    "application/json")
                return
            if self.path == "/api/console/current":
                # Save the operator-declared config on record. This is
                # panel-local bookkeeping only — it applies nothing.
                cfg = data.get("config")
                if not isinstance(cfg, dict):
                    self._send(400, json.dumps(
                        {"error": "config must be an object"}), "application/json")
                    return
                models, _p, _pr, _c, _f = _operator_data()
                errors, warnings = console.validate_config(
                    cfg, models=models, limits=_console_limits())
                if errors:
                    self._send(400, json.dumps(
                        {"error": "config failed validation", "errors": errors,
                         "warnings": warnings}), "application/json")
                    return
                _console_record_current(cfg, "manual")
                self._send(200, json.dumps({"saved": True, "warnings": warnings}),
                           "application/json")
                return
            if self.path == "/api/console/import-bundle":
                # Import a config bundle exported by another panel (or
                # an earlier backup of this one). Sections are applied
                # independently and reported; the operator data files
                # are rewritten atomically. Panel-local data only —
                # nothing here touches the engine.
                models_in, presets_in, current_in, problems, fatal = \
                    console.parse_bundle(data)
                if fatal:
                    self._send(400, json.dumps(
                        {"error": "not an importable bundle",
                         "problems": problems}), "application/json")
                    return
                applied = {"models": None, "presets": None, "current": False}
                if models_in is not None:
                    if _save_json_file(MODELS_FILE, {"models": models_in}):
                        applied["models"] = len(models_in)
                    else:
                        problems.append("models: could not write the data file")
                if presets_in is not None:
                    if _save_json_file(PRESETS_FILE, {"presets": presets_in}):
                        applied["presets"] = len(presets_in)
                    else:
                        problems.append("presets: could not write the data file")
                cur_warnings = []
                if current_in is not None:
                    models, _p, _pr, _c, _f = _operator_data()
                    errors, cur_warnings = console.validate_config(
                        current_in, models=models, limits=_console_limits())
                    if errors:
                        problems.extend(
                            f"current: not applied ({e['code']}: "
                            f"{e['message_en']})" for e in errors)
                    else:
                        _console_record_current(current_in, "import")
                        applied["current"] = True
                self._send(200, json.dumps(
                    {"applied": applied, "problems": problems,
                     "warnings": cur_warnings}), "application/json")
                return
            # /api/console/draft — panel-local draft only; applies nothing.
            cfg = data.get("config")
            if cfg is not None and not isinstance(cfg, dict):
                self._send(400, json.dumps(
                    {"error": "config must be an object or null"}),
                    "application/json")
                return
            _console_save_draft(cfg)
            self._send(200, json.dumps({"draft_saved": cfg is not None}),
                       "application/json")
            return
        self._send(404, "not found", "text/plain")

    def do_GET(self):
        if self.path.startswith("/api/history"):
            from urllib.parse import urlparse, parse_qs
            try:
                minutes = int(parse_qs(urlparse(self.path).query).get("minutes", ["60"])[0])
            except ValueError:
                minutes = 60
            upper = max(5, HISTORY_RETENTION // 60)
            minutes = max(5, min(upper, minutes))
            now = time.time()
            with _lock:
                pts = [p for p in list(_series) if p[0] >= now - minutes * 60]
            # Downsample to at most 720 points (bucket averages, nulls skipped).
            if len(pts) > 720:
                span = max(1.0, pts[-1][0] - pts[0][0])
                bucket = span / 720.0
                grouped = {}
                for pt in pts:
                    grouped.setdefault(int((pt[0] - pts[0][0]) / bucket), []).append(pt)
                out = []
                for vals in grouped.values():
                    def avg(idx):
                        vs = [v[idx] for v in vals if v[idx] is not None]
                        return sum(vs) / len(vs) if vs else None
                    def last(idx):
                        for v in reversed(vals):
                            if v[idx] is not None:
                                return v[idx]
                        return None
                    out.append({"t": vals[len(vals) // 2][0], "decode": avg(1),
                                "prefill": avg(2), "kv": avg(3), "gpu": avg(4),
                                "vram": avg(5), "temp": avg(6), "power": avg(7),
                                "cpu": avg(8), "disk": avg(9), "out": last(10),
                                "accept": avg(11)})
                pts = out
            else:
                pts = [{"t": t, "decode": d, "prefill": p, "kv": k, "gpu": g,
                        "vram": v, "temp": e, "power": w, "cpu": c, "disk": s,
                        "out": o, "accept": a}
                       for t, d, p, k, g, v, e, w, c, s, o, a in pts]
            self._send(200, json.dumps({"samples": pts, "minutes": minutes,
                                        "retention_seconds": HISTORY_RETENTION}),
                       "application/json")
            return
        if self.path == "/api/version":
            self._send(200, json.dumps(
                {"version": _panel_version(),
                 "update_check_enabled": _UPDATE_ENABLED}), "application/json")
            return
        if self.path.startswith("/api/update-check"):
            from urllib.parse import urlparse, parse_qs
            force = parse_qs(urlparse(self.path).query).get("force", ["0"])[0] == "1"
            self._send(200, json.dumps(_update_check(force=force)), "application/json")
            return
        if self.path == "/api/events":
            self._send(200, json.dumps({"events": [
                {"t": t, "from": f, "to": to, "error": e}
                for t, f, to, e in list(_events)]}), "application/json")
            return
        if self.path == "/api/request-history":
            with _lock:
                reqs = [dict(r) for r in _requests]
                log_reqs = [dict(r) for r in _reqlog]
            self._send(200, json.dumps({"requests": reqs,
                                        "log_requests": log_reqs}),
                       "application/json")
            return
        if self.path.startswith("/api/test-connection"):
            from urllib.parse import urlparse, parse_qs
            raw = parse_qs(urlparse(self.path).query).get("base_url", [""])[0]
            url = _valid_base_url(raw)
            if not url:
                self._send(400, json.dumps({"error": "invalid base_url: expected http(s)://host[:port], no userinfo, no path"}), "application/json")
                return
            self._send(200, json.dumps(_test_connection(url)), "application/json")
            return
        if self.path == "/api/console/export":
            # Download the operator's own setup (registered models,
            # presets, saved current record) as one portable bundle.
            # Panel-local data only; nothing about the engine is in it.
            entries = []
            try:
                if Path(MODELS_FILE).is_file():
                    entries, _probs = console.load_models_json(
                        Path(MODELS_FILE).read_text())
            except Exception:
                entries = []
            _m, presets, _p, _c, _f = _operator_data()
            current = _console_current["config"] if _console_current else None
            bundle = console.build_bundle(
                entries, presets, current, console_version=CONSOLE_VERSION,
                exported_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            self._send(200, json.dumps(bundle, ensure_ascii=False, indent=2),
                       "application/json",
                       {"Content-Disposition":
                        'attachment; filename="ninfer-console-config.json"'})
            return
        if self.path == "/api/console":
            self._send(200, json.dumps(_console_payload()), "application/json")
            return
        if self.path == "/api/settings":
            with _lock:
                saved = _settings.get("ninfer_base_url")
            self._send(200, json.dumps({
                "ninfer_base_url": _current_base(),
                "source": "gui" if saved else "env_or_default",
                "default": BASE}), "application/json")
            return
        if self.path in ("/api/health", "/api/snapshot"):
            with _lock:
                body = json.dumps(_snapshot)
            self._send(200, body, "application/json")
            return
        if self.path in ("/", "/index.html"):
            self._send(200, (WEB_DIR / "index.html").read_text(), "text/html; charset=utf-8")
            return
        # i18n dictionary, kept out of index.html (see web/i18n.js header).
        # Exact path only — no directory listing, no path traversal.
        if self.path == "/i18n.js":
            self._send(200, (WEB_DIR / "i18n.js").read_text(), "application/javascript; charset=utf-8")
            return
        self._send(404, "not found", "text/plain")


if __name__ == "__main__":
    _history_load()
    _events_load()
    _requests_load()
    _reqlog_load()
    _settings_load()
    _console_load()
    threading.Thread(target=_poller, daemon=True).start()
    print(f"ninfer-live-panel: polling {_current_base()} every {POLL}s, listening on {BIND}:{PORT}",
          flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
