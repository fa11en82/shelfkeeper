"""Load and validate config.yml."""
import os
import re
from dataclasses import dataclass, field

import yaml

from .items import validate_filter, parse_sort, ConfigError, GROUP_TYPES, _as_list
from .metadata import MISSING_CHECKS

ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_SETTINGS = {
    "schedule": ["03:00"],          # daily run times (HH:MM, container TZ)
    "run_on_start": True,
    "dry_run": False,
    "delete_removed_collections": True,   # delete collections we created that left the config
    "delete_below_minimum": True,         # delete our collections that fall under minimum_items
    "asset_directory": "/config/assets",
    "request_timeout": 60,
}

COLLECTION_KEYS = {"filter", "sort", "limit", "description", "minimum_items", "sync_mode"}
DYNAMIC_KEYS = {"type", "name", "filter", "exclude", "include", "min_items", "sort",
                "limit", "description", "sync_mode"}
OVERLAY_KEYS = {"name", "filter", "text", "image", "image_width", "position", "font",
                "font_size", "color", "background", "padding", "margin", "radius", "banner"}
POSITIONS = {"top-left", "top", "top-right", "left", "center", "right",
             "bottom-left", "bottom", "bottom-right"}
MATCH_KEYS = {"enabled", "providers", "when_missing", "filter", "override_cover",
              "override_details", "retry_unmatched_days", "max_per_run", "rematch"}
CLEANUP_KEYS = {"enabled", "filter", "genre_map", "drop_genres", "split_genres", "genre_case",
                "max_genres", "tag_map", "drop_tags", "add_tags", "author_map", "narrator_map",
                "series_map", "publisher_map", "language_map", "normalize_initials",
                "strip_title_patterns"}
FOLDER_KEYS = {"enabled", "filter", "template", "template_no_sequence", "template_no_series",
               "path_map", "max_moves_per_run", "scan_wait_seconds"}
LIBRARY_KEYS = {"collections", "dynamic_collections", "overlays", "assets", "metadata", "folders"}


def _check_metadata(m, where):
    _check_keys(m, {"match", "cleanup"}, where)
    if m.get("match"):
        mc = m["match"]
        _check_keys(mc, MATCH_KEYS, f"{where}.match")
        bad = set(_as_list(mc.get("when_missing", []))) - set(MISSING_CHECKS)
        if bad:
            raise ConfigError(f"{where}.match.when_missing: unknown {sorted(bad)}; "
                              f"allowed: {sorted(MISSING_CHECKS)}")
        if mc.get("filter") is not None:
            validate_filter(mc["filter"], f"{where}.match.filter")
    if m.get("cleanup"):
        cc = m["cleanup"]
        _check_keys(cc, CLEANUP_KEYS, f"{where}.cleanup")
        if cc.get("genre_case", "none") not in ("none", "title"):
            raise ConfigError(f"{where}.cleanup.genre_case must be 'none' or 'title'")
        for p in _as_list(cc.get("strip_title_patterns") or []):
            try:
                re.compile(p)
            except re.error as e:
                raise ConfigError(f"{where}.cleanup.strip_title_patterns: bad regex '{p}': {e}") from e
        if cc.get("filter") is not None:
            validate_filter(cc["filter"], f"{where}.cleanup.filter")


def _check_folders(fc, where):
    _check_keys(fc, FOLDER_KEYS, where)
    if fc.get("filter") is not None:
        validate_filter(fc["filter"], f"{where}.filter")
    if fc.get("path_map") is not None and not isinstance(fc["path_map"], dict):
        raise ConfigError(f"{where}.path_map: expected 'ABS path: container path' pairs")


@dataclass
class LibraryConfig:
    name: str
    collections: dict = field(default_factory=dict)
    dynamic_collections: list = field(default_factory=list)
    overlays: list = field(default_factory=list)
    assets: bool = True
    metadata: dict = field(default_factory=dict)
    folders: dict = field(default_factory=dict)


@dataclass
class Config:
    url: str
    token: str
    settings: dict
    libraries: dict
    config_dir: str


def _sub_env(obj):
    if isinstance(obj, str):
        def rep(m):
            val = os.environ.get(m.group(1))
            if val is None:
                if m.group(2) is not None:
                    return m.group(2)
                raise ConfigError(f"environment variable {m.group(1)} is not set")
            return val
        return ENV_RE.sub(rep, obj)
    if isinstance(obj, dict):
        return {k: _sub_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sub_env(v) for v in obj]
    return obj


def _check_keys(d, allowed, where):
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: expected a mapping")
    extra = set(d) - allowed
    if extra:
        raise ConfigError(f"{where}: unknown key(s) {sorted(extra)}; allowed: {sorted(allowed)}")


def _check_common(d, where):
    if d.get("filter") is not None:
        validate_filter(d["filter"], f"{where}.filter")
    if "sort" in d:
        parse_sort(d["sort"], where)
    if d.get("sync_mode", "sync") not in ("sync", "append"):
        raise ConfigError(f"{where}: sync_mode must be 'sync' or 'append'")


def load(path):
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw = _sub_env(raw)

    abs_cfg = raw.get("abs") or {}
    url = abs_cfg.get("url") or os.environ.get("ABS_URL")
    token = abs_cfg.get("token") or os.environ.get("ABS_TOKEN")
    if not url or not token:
        raise ConfigError("abs.url and abs.token (or ABS_URL / ABS_TOKEN env vars) are required")

    settings = dict(DEFAULT_SETTINGS)
    settings.update(raw.get("settings") or {})
    if isinstance(settings["schedule"], str):
        settings["schedule"] = [settings["schedule"]]
    for t in settings["schedule"]:
        if not re.fullmatch(r"\d{1,2}:\d{2}", str(t)):
            raise ConfigError(f"settings.schedule: '{t}' is not HH:MM")

    libraries = {}
    for lib_name, lc in (raw.get("libraries") or {}).items():
        lc = lc or {}
        _check_keys(lc, LIBRARY_KEYS, f"libraries.{lib_name}")
        if lc.get("metadata"):
            _check_metadata(lc["metadata"], f"libraries.{lib_name}.metadata")
        if lc.get("folders"):
            _check_folders(lc["folders"], f"libraries.{lib_name}.folders")
        cols = lc.get("collections") or {}
        for cname, c in cols.items():
            c = c or {}
            cols[cname] = c
            where = f"libraries.{lib_name}.collections.{cname}"
            _check_keys(c, COLLECTION_KEYS, where)
            _check_common(c, where)
        dyns = lc.get("dynamic_collections") or []
        for i, d in enumerate(dyns):
            where = f"libraries.{lib_name}.dynamic_collections[{i}]"
            _check_keys(d, DYNAMIC_KEYS, where)
            if d.get("type") not in GROUP_TYPES:
                raise ConfigError(f"{where}: type must be one of {sorted(GROUP_TYPES)}")
            _check_common(d, where)
        ovs = lc.get("overlays") or []
        for i, o in enumerate(ovs):
            where = f"libraries.{lib_name}.overlays[{i}]"
            _check_keys(o, OVERLAY_KEYS, where)
            if not o.get("text") and not o.get("image"):
                raise ConfigError(f"{where}: needs 'text' or 'image'")
            if o.get("position", "top-right") not in POSITIONS:
                raise ConfigError(f"{where}: position must be one of {sorted(POSITIONS)}")
            if o.get("filter") is not None:
                validate_filter(o["filter"], f"{where}.filter")
        libraries[lib_name] = LibraryConfig(
            name=lib_name,
            collections=cols,
            dynamic_collections=dyns,
            overlays=ovs,
            assets=bool(lc.get("assets", True)),
            metadata=lc.get("metadata") or {},
            folders=lc.get("folders") or {},
        )
    if not libraries:
        raise ConfigError("no libraries configured")

    return Config(url=url, token=token, settings=settings, libraries=libraries,
                  config_dir=os.path.dirname(os.path.abspath(path)))
