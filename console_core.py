#!/usr/bin/env python3
"""NInfer console (ninfer-console) — management core.

Pure logic only: no I/O, no HTTP, no Docker. Parameter edits are never
applied from here; the one live action (router model switching) is a
thin HTTP forward in app.py, not logic in this module.
This module is the single source of truth for the management view:

  * PARAM_SCHEMA — every serve parameter the form may touch, with its
    type / range / choices / group. The form, the validator, the diff
    and the command builder are all driven from this one table so the
    four can never drift apart.
  * blank_config() — a neutral, all-off starting config. There is NO
    seeded "current" configuration in this codebase: what the panel
    shows as current is either a record the operator saved, or fields
    the running engine itself reported (see app.py).
  * The model library is data, not code. Entries come from two
    operator-owned sources, merged by merge_models():
      - a models JSON file the operator maintains (load_models_json),
        which is the only place component capabilities (vision tower,
        MTP / DFlash2 components, NVFP4 requirement) can be declared;
      - *.ninfer files discovered in the models directory at runtime,
        whose capabilities are simply unknown.
    Nothing about any specific deployment's models, images or file
    names is baked into this module.
  * validate_config() — schema checks plus a compatibility engine.
    A conflict that is *known* (declared capability says no) is an
    error; a capability that is merely *unknown* is a warning, never
    a fabricated verdict. Host limits (RAM-derived host-cache cap,
    GPU VRAM budget) are measured by the caller at runtime and passed
    in via `limits`; when a limit is unknown the matching check is
    skipped rather than guessed.
  * diff_config() — old -> new, in schema order.
  * build_command() / build_compose_preview() — what an apply *would*
    run. Preview text only; there is no apply path.
  * parse_command() — the inverse: turn an existing serve command
    array (e.g. from the operator's own compose file) back into a
    config, so existing setups are imported, not re-typed.
  * load_presets_json() — operator-defined presets, validated and
    sanitized against the schema on load.
  * build_bundle() / parse_bundle() — one portable JSON file carrying
    the operator's registered models, presets and saved current
    record, for backup and for moving a setup between panels.
    Discovered models are machine state and never enter a bundle.

Safety boundary: a config that passes validation here is only
*well-formed and not a known-bad combination*. Nothing in this file —
or in the endpoints that call it — writes to NInfer, to Docker, or to
any compose file.
"""

import re

# ---------------------------------------------------------------- schema

# kind: "enum" | "int" | "int_or_auto" | "bool" | "string" | "model"
# flag: the serve CLI flag, or None for panel-side composite fields.
# "nullable" fields use None for "parameter absent" (not zero / not off).
# Ranges are wide sanity rails, not one machine's tuning: deployment
# limits that depend on the host are enforced separately via `limits`.
PARAM_SCHEMA = [
    {"key": "model", "kind": "model", "group": "basic", "flag": None},
    {"key": "image", "kind": "string", "group": "basic", "flag": None,
     "nullable": True},
    {"key": "image_nvfp4", "kind": "enum", "group": "basic", "flag": None,
     "choices": ["unknown", "yes", "no"], "nullable": True},
    {"key": "model_id", "kind": "string", "group": "basic", "flag": "--model-id",
     "nullable": True},
    {"key": "max_context", "kind": "int", "group": "context", "flag": "--max-context",
     "min": 128, "max": 4194304},
    {"key": "kv_capacity", "kind": "int_or_auto", "group": "context",
     "flag": "--kv-capacity", "min": 128, "max": 67108864},
    {"key": "kv_dtype", "kind": "enum", "group": "context", "flag": "--kv-dtype",
     "choices": ["rk4v4-e8", "rk4v4", "k4v2"]},
    {"key": "cache_mode", "kind": "enum", "group": "context", "flag": None,
     "choices": ["hybrid", "catalog"]},
    {"key": "host_cache_mib", "kind": "int", "group": "context",
     "flag": "--host-cache-mib", "min": 0, "nullable": True},
    {"key": "host_kv_mib", "kind": "int", "group": "context",
     "flag": "--host-kv-mib", "min": 0, "nullable": True},
    {"key": "max_concurrency", "kind": "int", "group": "sched",
     "flag": "--max-concurrency", "min": 1, "max": 1024},
    {"key": "max_pending_requests", "kind": "int", "group": "sched",
     "flag": "--max-pending-requests", "min": 0, "max": 100000},
    {"key": "pending_timeout_ms", "kind": "int", "group": "sched",
     "flag": "--pending-timeout-ms", "min": 0, "max": 86400000},
    {"key": "prefill_chunk", "kind": "int", "group": "sched",
     "flag": "--prefill-chunk", "min": 1, "max": 1048576},
    {"key": "spec", "kind": "enum", "group": "spec", "flag": "--spec",
     "choices": ["none", "dflash2", "mtp"]},
    {"key": "draft_tokens", "kind": "int", "group": "spec",
     "flag": "--draft-tokens", "min": 1, "max": 64, "nullable": True},
    {"key": "vision", "kind": "bool", "group": "spec", "flag": "--vision"},
    {"key": "vision_residency", "kind": "enum", "group": "spec",
     "flag": "--vision-residency", "choices": ["gpu", "overlay"], "nullable": True},
    {"key": "preserve_thinking", "kind": "bool", "group": "advanced",
     "flag": "--preserve-thinking"},
    {"key": "embedding_residency_host", "kind": "bool", "group": "advanced",
     "flag": "--embedding-residency"},
    {"key": "model_suspend", "kind": "bool", "group": "advanced",
     "flag": "--model-suspend"},
    {"key": "stats_and_log", "kind": "bool", "group": "advanced", "flag": None},
    {"key": "api_key_set", "kind": "bool", "group": "advanced", "flag": None},
]

_SCHEMA_BY_KEY = {p["key"]: p for p in PARAM_SCHEMA}

# Declared model requirements the validator understands. A model file
# entry may list any of these under "requires"; unknown values in an
# operator file are dropped with a problem note, never silently kept.
KNOWN_REQUIREMENTS = ("nvfp4",)

_IMAGE_REF_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-/]*(:[A-Za-z0-9._\-]+)?(@sha256:[0-9a-f]{64})?$")


def blank_config():
    """A neutral starting config: everything optional off or absent.

    Deliberately NOT any real deployment's values. app.py overlays the
    fields the running engine reports (model, max context, concurrency)
    when it builds the initial record from a live snapshot."""
    return {
        "model": None,
        "image": None,
        "image_nvfp4": "unknown",
        "model_id": None,
        "max_context": 32768,
        "kv_capacity": "auto",
        "kv_dtype": "rk4v4-e8",
        "cache_mode": "hybrid",
        "host_cache_mib": None,
        "host_kv_mib": None,
        "max_concurrency": 1,
        "max_pending_requests": 0,
        "pending_timeout_ms": 0,
        "prefill_chunk": 1024,
        "spec": "none",
        "draft_tokens": None,
        "vision": False,
        "vision_residency": None,
        "preserve_thinking": False,
        "embedding_residency_host": False,
        "model_suspend": False,
        "stats_and_log": False,
        "api_key_set": False,
    }


# ------------------------------------------------------- model library

def _stem(fname):
    return fname[:-len(".ninfer")] if fname and fname.endswith(".ninfer") else fname


def _clean_components(raw, problems, where):
    """Tri-state components: True / False / None (unknown) per key."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        problems.append(f"{where}: components must be an object; ignored")
        return None
    out = {}
    for key in ("vision", "mtp", "dflash2"):
        val = raw.get(key)
        if val is not None and not isinstance(val, bool):
            problems.append(f"{where}: components.{key} must be true/false; treated as unknown")
            val = None
        out[key] = val
    return out


def load_models_json(text):
    """Parse an operator-maintained models file.

    Returns (entries, problems). Malformed input never raises: bad
    entries are skipped and every problem is reported as a string so
    the UI can show exactly what was ignored and why."""
    import json
    entries, problems = [], []
    try:
        data = json.loads(text)
    except Exception as exc:
        return [], [f"models file is not valid JSON: {exc}"]
    if isinstance(data, dict):
        raw_list = data.get("models")
    else:
        raw_list = data
    if not isinstance(raw_list, list):
        return [], ["models file: top level must be an object with a 'models' list"]
    seen_ids = set()
    for idx, raw in enumerate(raw_list):
        where = f"models[{idx}]"
        if not isinstance(raw, dict):
            problems.append(f"{where}: not an object; skipped")
            continue
        fname = raw.get("file")
        if fname is not None and not isinstance(fname, str):
            problems.append(f"{where}: file must be a string; skipped")
            continue
        raw_id = raw.get("id")
        if raw_id is not None and not isinstance(raw_id, str):
            problems.append(f"{where}: id must be a string; skipped")
            continue
        mid = raw_id or (_stem(fname) if fname else None)
        if not mid:
            problems.append(f"{where}: needs at least an id or a file; skipped")
            continue
        if mid in seen_ids:
            problems.append(f"{where}: duplicate id {mid!r}; skipped")
            continue
        size_gb = raw.get("size_gb")
        if size_gb is not None and (isinstance(size_gb, bool)
                                    or not isinstance(size_gb, (int, float))
                                    or size_gb <= 0):
            problems.append(f"{where}: size_gb must be a positive number; ignored")
            size_gb = None
        requires = raw.get("requires") or []
        if not isinstance(requires, list):
            problems.append(f"{where}: requires must be a list; ignored")
            requires = []
        clean_requires = []
        for req in requires:
            if req in KNOWN_REQUIREMENTS:
                clean_requires.append(req)
            else:
                problems.append(f"{where}: unknown requirement {req!r}; dropped")
        default_spec = raw.get("default_spec")
        if default_spec is not None and default_spec not in ("none", "dflash2", "mtp"):
            problems.append(f"{where}: default_spec must be none/dflash2/mtp; ignored")
            default_spec = None
        seen_ids.add(mid)
        entries.append({
            "id": mid,
            "name": raw.get("name") if isinstance(raw.get("name"), str) else mid,
            "file": fname,
            "size_gb": float(size_gb) if size_gb is not None else None,
            "components": _clean_components(raw.get("components"), problems, where),
            "default_spec": default_spec,
            "requires": clean_requires,
            "note": raw.get("note") if isinstance(raw.get("note"), str) else None,
            "source": "file",
        })
    return entries, problems


def merge_models(registered, discovered):
    """Merge operator-file entries with runtime-discovered *.ninfer files.

    `discovered` maps file name -> size in bytes. A registered entry
    whose file is discovered gains a measured size and source "both";
    a discovered file with no entry becomes an entry whose components
    are unknown. Inputs are not mutated."""
    out = []
    by_file = {}
    for entry in registered or []:
        entry = dict(entry)
        if entry.get("file"):
            by_file[entry["file"]] = entry
        out.append(entry)
    for fname, size in sorted((discovered or {}).items()):
        size_gb = size / 1e9 if isinstance(size, (int, float)) and size >= 0 else None
        if fname in by_file:
            entry = by_file[fname]
            entry["size_gb"] = size_gb
            entry["source"] = "both"
        else:
            out.append({"id": _stem(fname), "name": fname, "file": fname,
                        "size_gb": size_gb, "components": None,
                        "default_spec": None, "requires": [], "note": None,
                        "source": "discovered"})
    return out


def model_by_id(models, mid):
    for m in models or []:
        if m.get("id") == mid:
            return m
    return None


def model_by_file(models, fname):
    if not fname:
        return None
    for m in models or []:
        if m.get("file") == fname or m.get("id") == _stem(fname):
            return m
    return None


def available_specs(model):
    """Spec modes a model artifact can serve.

    Unknown components mean "not declared", so every mode is offered
    and validate_config() warns instead of pretending certainty."""
    specs = ["none"]
    comps = (model or {}).get("components") or {}
    if comps.get("dflash2") is not False:
        specs.append("dflash2")
    if comps.get("mtp") is not False:
        specs.append("mtp")
    return specs


def resolve_image_ref(cfg):
    """The image reference a config would use (a plain string field)."""
    return (cfg or {}).get("image") or None


# -------------------------------------------------------------- presets

def sanitize_config(raw, problems=None, where="config"):
    """Coerce an arbitrary mapping into a schema-shaped config.

    Starts from blank_config(); known keys with correctly-typed values
    are kept, everything else is dropped and (when `problems` is a
    list) reported. Enum values outside their choices are dropped."""
    cfg = blank_config()
    if not isinstance(raw, dict):
        if problems is not None:
            problems.append(f"{where}: not an object; ignored")
        return cfg
    for key, val in raw.items():
        entry = _SCHEMA_BY_KEY.get(key)
        if entry is None:
            if problems is not None:
                problems.append(f"{where}: unknown key {key!r}; dropped")
            continue
        kind = entry["kind"]
        ok = True
        if val is None:
            ok = bool(entry.get("nullable")) or key in ("model", "image")
            if ok:
                cfg[key] = None
        elif kind == "int":
            ok = isinstance(val, int) and not isinstance(val, bool)
            if ok:
                cfg[key] = val
        elif kind == "int_or_auto":
            ok = val == "auto" or (isinstance(val, int) and not isinstance(val, bool))
            if ok:
                cfg[key] = val
        elif kind == "bool":
            ok = isinstance(val, bool)
            if ok:
                cfg[key] = val
        elif kind == "enum":
            ok = val in entry["choices"]
            if ok:
                cfg[key] = val
        elif kind in ("string", "model"):
            ok = isinstance(val, str)
            if ok:
                cfg[key] = val
        if not ok and problems is not None:
            problems.append(f"{where}: bad value for {key!r}; dropped")
    return cfg


def load_presets_json(text, models=()):
    """Parse an operator-maintained presets file -> (presets, problems).

    Each preset's config is sanitized against the schema. Presets are
    operator data; this codebase ships none."""
    import json
    presets, problems = [], []
    try:
        data = json.loads(text)
    except Exception as exc:
        return [], [f"presets file is not valid JSON: {exc}"]
    raw_list = data.get("presets") if isinstance(data, dict) else data
    if not isinstance(raw_list, list):
        return [], ["presets file: top level must be an object with a 'presets' list"]
    seen = set()
    for idx, raw in enumerate(raw_list):
        where = f"presets[{idx}]"
        if not isinstance(raw, dict):
            problems.append(f"{where}: not an object; skipped")
            continue
        pid = raw.get("id")
        if not isinstance(pid, str) or not pid or pid in seen:
            problems.append(f"{where}: needs a unique string id; skipped")
            continue
        seen.add(pid)
        sub = []
        cfg = sanitize_config(raw.get("config"), sub, f"{where}.config")
        problems.extend(sub)
        presets.append({"id": pid,
                        "label": raw.get("label") if isinstance(raw.get("label"), str) else pid,
                        "config": cfg})
    return presets, problems


# ------------------------------------------------------------ config bundle

BUNDLE_FORMAT = "ninfer-console-bundle"
BUNDLE_VERSION = 1


def build_bundle(models, presets, current, console_version="",
                 exported_at=""):
    """Assemble the portable config bundle: the operator's registered
    models, presets and saved config on record — nothing else. Models
    discovered in a directory are that machine's state, not setup, and
    are never included."""
    return {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION,
            "console_version": console_version, "exported_at": exported_at,
            "models": models, "presets": presets, "current": current}


def parse_bundle(data):
    """Parse an imported bundle -> (models, presets, current, problems,
    fatal). A section that is absent (or null) comes back None, meaning
    "not part of this bundle, leave it untouched". Present sections are
    cleaned exactly like the operator data files: bad entries are
    dropped and reported, never fatal. fatal=True means refuse the
    whole file: not a bundle, or a bundle version this build does not
    understand."""
    import json
    if not isinstance(data, dict):
        return None, None, None, ["bundle is not a JSON object"], True
    if data.get("format") != BUNDLE_FORMAT:
        return None, None, None, \
            ["missing or wrong format marker (not a ninfer-console bundle)"], True
    if data.get("version") != BUNDLE_VERSION:
        return None, None, None, \
            [f"unsupported bundle version {data.get('version')!r}"], True
    problems = []
    models = presets = current = None
    if data.get("models") is not None:
        if isinstance(data["models"], list):
            models, probs = load_models_json(json.dumps({"models": data["models"]}))
            problems.extend(f"models: {p}" for p in probs)
        else:
            problems.append("models: section is not a list; skipped")
    if data.get("presets") is not None:
        if isinstance(data["presets"], list):
            presets, probs = load_presets_json(json.dumps({"presets": data["presets"]}))
            problems.extend(f"presets: {p}" for p in probs)
        else:
            problems.append("presets: section is not a list; skipped")
    if data.get("current") is not None:
        if isinstance(data["current"], dict):
            sub = []
            current = sanitize_config(data["current"], sub, "current")
            problems.extend(f"current: {p}" for p in sub)
        else:
            problems.append("current: section is not an object; skipped")
    return models, presets, current, problems, False


def normalize_router_models(payload):
    """Normalize an engine router's GET /models payload into
    [{"id": str, "status": str}]. Tolerant of the shapes llama.cpp-family
    routers emit: a bare list or {"data"|"models": [...]}, the id under
    "id" or "model", the status as a string or {"value": ...}. Entries
    without a usable id are skipped — never invented."""
    if isinstance(payload, dict):
        raw = payload.get("data") or payload.get("models") or []
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id") or entry.get("model")
        if not isinstance(mid, str) or not mid:
            continue
        status = entry.get("status", entry.get("state"))
        if isinstance(status, dict):
            status = status.get("value") or status.get("status")
        if not isinstance(status, str) or not status:
            status = "unknown"
        out.append({"id": mid, "status": status.lower()})
    return out


# -------------------------------------------------------------- validation

def _issue(field, code, zh, en):
    return {"field": field, "code": code, "message_zh": zh, "message_en": en}


def validate_config(cfg, models=(), limits=None):
    """Return (errors, warnings): lists of issue dicts. Pure function.

    `models` is the merged model library (may be empty). `limits` may
    carry runtime-measured deployment limits:
      {"host_cache_max_mib": int|None, "vram_budget_gb": float|None}
    A limit of None means "not measurable here" and skips that check."""
    errors, warnings = [], []
    limits = limits or {}
    if not isinstance(cfg, dict):
        return [_issue("", "not_a_config", "配置不是对象", "Config is not an object")], warnings

    for key in cfg:
        if key not in _SCHEMA_BY_KEY:
            errors.append(_issue(key, "unknown_field",
                                 f"未知参数 {key}（不在 schema 内，拒绝）",
                                 f"Unknown parameter {key} (not in the schema)"))

    for entry in PARAM_SCHEMA:
        key, kind = entry["key"], entry["kind"]
        val = cfg.get(key)
        if val is None:
            if key in cfg and not entry.get("nullable") and key not in ("model", "image"):
                errors.append(_issue(key, "null_not_allowed",
                                     f"{key} 不能为空",
                                     f"{key} must not be null"))
            continue
        if kind == "int":
            if isinstance(val, bool) or not isinstance(val, int):
                errors.append(_issue(key, "bad_type", f"{key} 必须是整数",
                                     f"{key} must be an integer"))
            elif (entry.get("min") is not None and val < entry["min"]) or \
                    (entry.get("max") is not None and val > entry["max"]):
                errors.append(_issue(key, "out_of_range",
                                     f"{key} 超出范围 {entry.get('min')}–{entry.get('max')}",
                                     f"{key} is out of range {entry.get('min')}–{entry.get('max')}"))
        elif kind == "int_or_auto":
            if val != "auto":
                if isinstance(val, bool) or not isinstance(val, int):
                    errors.append(_issue(key, "bad_type",
                                         f"{key} 必须是 auto 或整数",
                                         f"{key} must be 'auto' or an integer"))
                elif (entry.get("min") is not None and val < entry["min"]) or \
                        (entry.get("max") is not None and val > entry["max"]):
                    errors.append(_issue(key, "out_of_range",
                                         f"{key} 超出范围 {entry.get('min')}–{entry.get('max')}",
                                         f"{key} is out of range {entry.get('min')}–{entry.get('max')}"))
        elif kind == "enum":
            if val not in entry["choices"]:
                errors.append(_issue(key, "bad_choice",
                                     f"{key} 只能是 {' / '.join(entry['choices'])}",
                                     f"{key} must be one of {' / '.join(entry['choices'])}"))
        elif kind == "bool":
            if not isinstance(val, bool):
                errors.append(_issue(key, "bad_type", f"{key} 必须是开关值",
                                     f"{key} must be a boolean"))
        elif kind in ("string", "model"):
            if not isinstance(val, str) or not val.strip():
                errors.append(_issue(key, "bad_type", f"{key} 必须是非空文本",
                                     f"{key} must be a non-empty string"))

    model = model_by_id(models, cfg.get("model") or "")
    if cfg.get("model") is None:
        errors.append(_issue("model", "model_required",
                             "必须选一个模型", "A model must be selected"))
    elif models and model is None:
        errors.append(_issue("model", "unknown_model",
                             "所选模型不在模型库里（先在模型目录放入文件，或在模型文件里登记）",
                             "Selected model is not in the library (add the file to the models dir or register it in the models file)"))

    image = cfg.get("image")
    if image:
        if not isinstance(image, str) or not _IMAGE_REF_RE.match(image.strip()):
            errors.append(_issue("image", "bad_image_ref",
                                 "镜像格式不对（应为 仓库/镜像:标签 或带 @sha256: 摘要）",
                                 "Image ref is malformed (want repo/image:tag, optionally @sha256:…)"))

    if model is not None:
        comps = model.get("components") or {}
        if "nvfp4" in (model.get("requires") or []):
            nv = cfg.get("image_nvfp4")
            if nv == "no":
                errors.append(_issue("image_nvfp4", "nvfp4_needs_capable_image",
                                     "该模型需要支持 NVFP4 的引擎构建，但镜像被标为不支持，会加载失败",
                                     "This model needs an NVFP4-capable engine build, but the image is marked as not supporting it"))
            elif nv != "yes":
                warnings.append(_issue("image_nvfp4", "nvfp4_image_unverified",
                                       "该模型需要支持 NVFP4 的引擎构建；当前镜像的支持情况未确认，请自行核对",
                                       "This model needs an NVFP4-capable engine build; whether this image has it is unconfirmed"))
        spec = cfg.get("spec")
        if spec in ("dflash2", "mtp"):
            if comps.get(spec) is False:
                errors.append(_issue("spec", "spec_not_in_model",
                                     f"所选模型不带 {spec} 组件，不能用这种投机解码",
                                     f"The selected model has no {spec} component"))
            elif comps.get(spec) is None:
                warnings.append(_issue("spec", "spec_unverified",
                                       f"模型是否带 {spec} 组件未登记，无法确认，请自行核对",
                                       f"Whether the model carries a {spec} component is undeclared; please verify"))
        if cfg.get("vision"):
            if comps.get("vision") is False:
                errors.append(_issue("vision", "vision_not_in_model",
                                     "所选模型没有视觉塔，--vision 必须关",
                                     "The selected model has no vision tower; --vision must be off"))
            elif comps.get("vision") is None:
                warnings.append(_issue("vision", "vision_unverified",
                                       "模型是否有视觉塔未登记，无法确认，请自行核对",
                                       "Whether the model has a vision tower is undeclared; please verify"))
        size_gb = model.get("size_gb")
        budget = limits.get("vram_budget_gb")
        if size_gb and budget and size_gb > budget:
            warnings.append(_issue("model", "model_too_large",
                                   "模型文件本身已超过可用显存预算，大概率装不下",
                                   "The model file alone exceeds the usable VRAM budget"))

    spec = cfg.get("spec")
    if spec != "dflash2" and cfg.get("draft_tokens") is not None:
        errors.append(_issue("draft_tokens", "draft_tokens_with_non_dflash",
                             "草稿 token 数只有 dflash2 用；当前投机方式下必须留空",
                             "Draft tokens only apply to dflash2; must be empty otherwise"))
    if spec == "dflash2" and cfg.get("draft_tokens") is None:
        warnings.append(_issue("draft_tokens", "draft_tokens_default",
                               "dflash2 未填草稿 token 数，将走引擎默认",
                               "dflash2 without draft tokens falls back to the engine default"))
    if not cfg.get("vision") and cfg.get("vision_residency") is not None:
        errors.append(_issue("vision_residency", "residency_without_vision",
                             "视觉已关，视觉常驻方式必须留空",
                             "Vision residency must be empty while vision is off"))

    mode = cfg.get("cache_mode")
    if mode == "hybrid" and cfg.get("host_kv_mib") is not None:
        errors.append(_issue("host_kv_mib", "hybrid_with_catalog_param",
                             "hybrid 模式与 catalog 参数互斥：--host-kv-mib 必须留空",
                             "Hybrid mode excludes catalog params: --host-kv-mib must be empty"))
    if mode == "catalog" and cfg.get("host_cache_mib") is not None:
        errors.append(_issue("host_cache_mib", "catalog_with_hybrid_param",
                             "catalog 模式与 hybrid 参数互斥：--host-cache-mib 必须留空",
                             "Catalog mode excludes hybrid params: --host-cache-mib must be empty"))
    host_max = limits.get("host_cache_max_mib")
    if host_max:
        for key in ("host_cache_mib", "host_kv_mib"):
            val = cfg.get(key)
            if isinstance(val, int) and not isinstance(val, bool) and val > host_max:
                errors.append(_issue(key, "host_cache_over_limit",
                                     f"{key} 超过本机上限 {host_max} MiB（按宿主内存动态计算）",
                                     f"{key} exceeds this host's limit of {host_max} MiB (derived from host RAM)"))

    kv, mc = cfg.get("kv_capacity"), cfg.get("max_context")
    if isinstance(kv, int) and isinstance(mc, int) and kv < mc:
        warnings.append(_issue("kv_capacity", "kv_below_context",
                               "KV 容量小于最大上下文：单个满长请求就可能装不下",
                               "KV capacity is below max context: one full-length request may not fit"))
    return errors, warnings


# ------------------------------------------------------------------- diff

def _display_value(key, val, models):
    if val is None:
        return None
    if key == "model":
        m = model_by_id(models, val)
        return m["name"] if m else val
    return val


def diff_config(old, new, models=()):
    """Changed keys in schema order: [{key, old, new, old_label, new_label}]."""
    out = []
    old = old or {}
    new = new or {}
    for entry in PARAM_SCHEMA:
        key = entry["key"]
        o, n = old.get(key), new.get(key)
        if o != n:
            out.append({"key": key, "old": o, "new": n,
                        "old_label": _display_value(key, o, models),
                        "new_label": _display_value(key, n, models)})
    return out


# --------------------------------------------------------- command builder

def build_command(cfg):
    """The serve argument list this config would run (preview only)."""
    cfg = cfg or {}
    args = ["serve", "/models/model.ninfer",
            "--host", "0.0.0.0", "--port", "8080"]
    if cfg.get("model_id"):
        args += ["--model-id", str(cfg["model_id"])]
    args += ["--max-context", str(cfg.get("max_context")),
             "--kv-capacity", str(cfg.get("kv_capacity")),
             "--kv-dtype", str(cfg.get("kv_dtype"))]
    if cfg.get("cache_mode") == "hybrid":
        args += ["--use-alt-prefix-caching"]
        if cfg.get("host_cache_mib") is not None:
            args += ["--host-cache-mib", str(cfg["host_cache_mib"])]
    else:
        if cfg.get("host_kv_mib") is not None:
            args += ["--host-kv-mib", str(cfg["host_kv_mib"])]
    args += ["--max-concurrency", str(cfg.get("max_concurrency")),
             "--max-pending-requests", str(cfg.get("max_pending_requests")),
             "--pending-timeout-ms", str(cfg.get("pending_timeout_ms")),
             "--prefill-chunk", str(cfg.get("prefill_chunk"))]
    if cfg.get("spec") and cfg["spec"] != "none":
        args += ["--spec", str(cfg["spec"])]
    if cfg.get("draft_tokens") is not None:
        args += ["--draft-tokens", str(cfg["draft_tokens"])]
    if cfg.get("preserve_thinking"):
        args += ["--preserve-thinking"]
    if cfg.get("vision"):
        args += ["--vision"]
    if cfg.get("vision_residency"):
        args += ["--vision-residency", str(cfg["vision_residency"])]
    if cfg.get("embedding_residency_host"):
        args += ["--embedding-residency", "host"]
    if cfg.get("model_suspend"):
        args += ["--model-suspend"]
    if cfg.get("stats_and_log"):
        args += ["--stats-port", "8081",
                 "--request-log-jsonl", "/cache/requests.jsonl"]
    if cfg.get("api_key_set"):
        args += ["--api-key", "<from panel secrets, never shown>"]
    return args


def build_compose_preview(cfg, models=(), host_models_dir="/path/to/models"):
    """A fixed-structure compose rendering of the config (preview only).

    Structure (service/container/ports/mounts/healthcheck) is constant;
    only the image, the model mount and the command can vary, and only
    through the schema. This is the exact text an apply path would
    write — shown now so the operator can audit it beforehand."""
    model = model_by_id(models, (cfg or {}).get("model") or "")
    model_file = (model or {}).get("file") or "<model file unknown>"
    lines = [
        "name: ninfer-infer",
        "",
        "services:",
        "  infer:",
        f"    image: {resolve_image_ref(cfg) or '<image not set>'}",
        "    container_name: ninfer",
        "    restart: unless-stopped",
        "",
        "    command:",
    ]
    for a in build_command(cfg):
        lines.append(f'      - "{a}"')
    lines += [
        "",
        '    ports:',
        '      - "8080:8080"',
        "",
        "    volumes:",
        f'      - "{host_models_dir.rstrip("/")}/{model_file}:/models/model.ninfer:ro"',
        '      - "./infer-cache:/cache"',
        '      - "./infer-grafts:/grafts"',
        "",
        "    gpus: all",
        "    shm_size: 64m",
        "",
        "    healthcheck:",
        '      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:8080/health > /dev/null || exit 1"]',
        "      interval: 30s",
        "      timeout: 5s",
        "      start_period: 120s",
        "      retries: 3",
        "",
        "    logging:",
        "      driver: json-file",
        "      options:",
        '        max-size: "50m"',
        '        max-file: "3"',
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------- command parser

_FLAG_TO_KEY = {}
for _e in PARAM_SCHEMA:
    if _e.get("flag"):
        _FLAG_TO_KEY[_e["flag"]] = _e


def parse_command(args, models=()):
    """Inverse of build_command: serve args -> (config, unparsed, model_file).

    Starts from blank_config() so a partial command still yields a full
    config; flags a file leaves out simply stay off/absent. Unknown
    flags are reported in `unparsed`, never silently dropped. The
    positional model path is reported as `model_file` and resolved to
    a library id when `models` knows the file."""
    cfg = blank_config()
    cfg["cache_mode"] = "catalog"  # until --use-alt-prefix-caching proves hybrid
    unparsed = []
    model_file = None
    if not isinstance(args, (list, tuple)):
        return cfg, ["<command is not a list>"], None
    args = [a for a in args if isinstance(a, str)]
    i = 0
    if args and args[0] == "serve":
        i = 1
    if i < len(args) and not args[i].startswith("--"):
        model_file = args[i].rsplit("/", 1)[-1]
        i += 1  # the mount target; the file name is what identifies the model
    while i < len(args):
        tok = args[i]
        if tok == "--use-alt-prefix-caching":
            cfg["cache_mode"] = "hybrid"
            i += 1
            continue
        if tok in ("--host", "--port"):
            i += 2  # structural constants of build_command, not schema fields
            continue
        if tok in ("--stats-port", "--request-log-jsonl"):
            cfg["stats_and_log"] = True
            i += 2
            continue
        if tok == "--api-key":
            cfg["api_key_set"] = True  # the value itself is never stored
            i += 2
            continue
        if tok == "--host-cache-mib":
            cfg["cache_mode"] = "hybrid"
        entry = _FLAG_TO_KEY.get(tok)
        if entry is None:
            unparsed.append(tok)
            i += 1
            continue
        key, kind = entry["key"], entry["kind"]
        if kind == "bool":
            if key == "embedding_residency_host":
                val = args[i + 1] if i + 1 < len(args) else None
                cfg[key] = (val == "host")
                i += 2 if val is not None else 1
            else:
                cfg[key] = True
                i += 1
            continue
        if i + 1 >= len(args):
            unparsed.append(tok)
            i += 1
            continue
        raw = args[i + 1]
        if kind == "int":
            try:
                cfg[key] = int(raw)
            except (TypeError, ValueError):
                unparsed.append(tok)
        elif kind == "int_or_auto":
            if raw == "auto":
                cfg[key] = "auto"
            else:
                try:
                    cfg[key] = int(raw)
                except (TypeError, ValueError):
                    unparsed.append(tok)
        else:
            cfg[key] = raw
        i += 2
    if model_file:
        m = model_by_file(models, model_file)
        if m:
            cfg["model"] = m["id"]
    return cfg, unparsed, model_file


# ---------------------------------------------------------------------------
# Engine router preset file (presets.ini) validation
#
# In router mode the engine reads per-model serve parameters from an INI
# file at startup. The console's online editor validates the text before
# anything is written: structure errors are hard failures, while sections
# matching no known model are warnings only (the engine ignores sections
# it cannot use, and newer engines may know ids this build does not).

PRESET_INI_MAX_BYTES = 256 * 1024


def validate_preset_ini(text, known_ids=None):
    """Validate engine preset INI text.

    Returns {"ok", "errors", "sections", "unknown_sections"}: sections
    is [{"name", "keys"}] in file order ('*' is the shared section);
    unknown_sections lists section names matching no known id (warning
    grade, never blocks)."""
    import configparser
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "errors": ["empty content"], "sections": [],
                "unknown_sections": []}
    if len(text.encode("utf-8")) > PRESET_INI_MAX_BYTES:
        return {"ok": False,
                "errors": [f"file too large (> {PRESET_INI_MAX_BYTES} bytes)"],
                "sections": [], "unknown_sections": []}
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        return {"ok": False, "errors": [str(exc)], "sections": [],
                "unknown_sections": []}
    sections = [{"name": name, "keys": len(parser[name])}
                for name in parser.sections()]
    if not sections:
        return {"ok": False, "errors": ["no sections"], "sections": [],
                "unknown_sections": []}
    known = set(known_ids or [])
    unknown = [s["name"] for s in sections
               if s["name"] != "*" and known and s["name"] not in known]
    return {"ok": True, "errors": [], "sections": sections,
            "unknown_sections": unknown}
