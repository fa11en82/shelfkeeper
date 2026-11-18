"""Folder correction: move book folders into a consistent layout such as
Author/Series/01 - Title.

Folders are *renamed* (never copied), so they keep their inode and
Audiobookshelf keeps the same book, id, progress and collections after its
rescan. Every move is written to state/moves.jsonl and can be undone with
--undo-moves.
"""
import errno
import json
import logging
import os
import posixpath
import re
import time

from .items import SafeDict, matches

log = logging.getLogger("shelfkeeper.folders")

INVALID = re.compile(r'[<>:"|?*\x00-\x1f]')

DEFAULT_TEMPLATES = {
    "template": "{author}/{series}/{series_sequence_padded} - {title}",
    "template_no_sequence": "{author}/{series}/{title}",
    "template_no_series": "{author}/{title}",
}


def _clean_value(v):
    v = str(v).replace(": ", " - ").replace("/", "-").replace("\\", "-")
    return INVALID.sub("", v)


def _clean_component(s, maxlen=150):
    s = " ".join(INVALID.sub("", s).split()).strip(" .-_")
    return s[:maxlen].rstrip(" .-_")


def target_parts(book, fcfg):
    vars_ = {k: _clean_value(v) for k, v in book.template_vars().items()}
    if not vars_["author"]:
        vars_["author"] = "Unknown Author"
    s = book.series[0] if book.series else None
    if s and s.sequence:
        key = "template"
    elif s:
        key = "template_no_sequence"
    else:
        key = "template_no_series"
    tpl = fcfg.get(key) or DEFAULT_TEMPLATES[key]
    rendered = str(tpl).format_map(SafeDict(vars_))
    parts = [_clean_component(p) for p in rendered.replace("\\", "/").split("/")]
    return [p for p in parts if p]


class PathMap:
    """Translate between paths as ABS sees them and paths inside this container."""

    def __init__(self, mapping):
        self.pairs = sorted(((a.rstrip("/"), l.rstrip("/")) for a, l in (mapping or {}).items()),
                            key=lambda p: -len(p[0]))

    def local(self, abs_path):
        for a, l in self.pairs:
            if abs_path == a or abs_path.startswith(a + "/"):
                return l + abs_path[len(a):]
        return abs_path


def _prune_empty(d, stop):
    stop = os.path.normpath(stop)
    d = os.path.normpath(d)
    while d != stop and d.startswith(stop + os.sep) and os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d)
        log.debug("removed empty folder %s", d)
        d = os.path.dirname(d)


def run_folders(api, library, books, fcfg, state, dry):
    """Returns the number of folders moved (or that would move in a dry run)."""
    if not fcfg or not fcfg.get("enabled", True):
        return 0
    lib_folders = {f["id"]: f["fullPath"].rstrip("/")
                   for f in api.get_library(library["id"]).get("folders", [])}
    pm = PathMap(fcfg.get("path_map"))
    limit = int(fcfg.get("max_moves_per_run", 50))
    run_id = time.strftime("%Y%m%d-%H%M%S")
    planned, moved, conflicts, unreachable, singles = set(), 0, 0, 0, 0

    for b in books:
        if not matches(b, fcfg.get("filter")):
            continue
        if b.is_file:
            singles += 1  # loose single-file books are left where they are
            continue
        root = lib_folders.get(b.folder_id) or max(
            (r for r in lib_folders.values() if b.path.startswith(r + "/")), key=len, default=None)
        if not root:
            continue
        parts = target_parts(b, fcfg)
        if not parts:
            continue
        tgt_abs = posixpath.join(root, *parts)
        if posixpath.normpath(b.path) == posixpath.normpath(tgt_abs):
            continue
        if moved >= limit:
            log.info("reached max_moves_per_run (%d); the rest will move next run", limit)
            break

        cur, tgt, local_root = pm.local(b.path), pm.local(tgt_abs), pm.local(root)
        if not os.path.isdir(cur):
            unreachable += 1
            if unreachable <= 3:
                log.warning("can't see '%s' inside the container - check the library volume "
                            "and folders.path_map", cur)
            continue
        if cur.lower() != tgt.lower() and (tgt in planned or os.path.exists(tgt)):
            conflicts += 1
            log.warning("skip '%s': target already exists: %s", b.title, tgt)
            continue
        if tgt.startswith(cur + "/") or cur.startswith(tgt + "/"):
            log.warning("skip '%s': target is inside the current folder: %s", b.title, tgt)
            continue

        planned.add(tgt)
        rel_from = os.path.relpath(cur, local_root)
        rel_to = os.path.relpath(tgt, local_root)
        if dry:
            log.info("[dry-run] move  %s  ->  %s", rel_from, rel_to)
            moved += 1
            continue
        try:
            os.makedirs(os.path.dirname(tgt), exist_ok=True)
            if cur.lower() == tgt.lower():  # case-only rename: go via a temp name
                tmp = cur + ".shelfkeeper-tmp"
                os.rename(cur, tmp)
                os.rename(tmp, tgt)
            else:
                os.rename(cur, tgt)
        except OSError as e:
            if e.errno == errno.EXDEV:
                log.error("can't move '%s': source and target are on different filesystems "
                          "(copying would lose ABS progress) - skipped", b.title)
            else:
                log.error("move '%s' failed: %s", b.title, e)
            continue
        with open(state.moves_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"run": run_id, "item": b.id, "title": b.title, "from": cur,
                                "to": tgt, "root": local_root, "at": time.time()}) + "\n")
        _prune_empty(os.path.dirname(cur), local_root)
        log.info("moved  %s  ->  %s", rel_from, rel_to)
        moved += 1

    if unreachable > 3:
        log.warning("... %d book folders in total were not visible in the container", unreachable)
    log.info("[%s] folders: %d %s, %d conflicts, %d single-file books left in place",
             library["name"], moved, "would move" if dry else "moved", conflicts, singles)
    if moved and not dry:
        api.scan_library(library["id"], library["name"])
    return moved


def undo_moves(state, dry):
    """Reverse the most recent run's moves. Returns how many were undone."""
    if not os.path.exists(state.moves_log):
        log.info("no moves recorded")
        return 0
    with open(state.moves_log, encoding="utf-8") as f:
        entries = [json.loads(line) for line in f if line.strip()]
    if not entries:
        log.info("no moves recorded")
        return 0
    last = entries[-1]["run"]
    batch = [e for e in entries if e["run"] == last]
    keep = [e for e in entries if e["run"] != last]
    undone = 0
    for e in reversed(batch):
        if not os.path.isdir(e["to"]) or os.path.exists(e["from"]):
            log.warning("can't undo '%s': %s is gone or %s exists", e["title"], e["to"], e["from"])
            keep.append(e)
            continue
        if dry:
            log.info("[dry-run] move back  %s  ->  %s", e["to"], e["from"])
            undone += 1
            continue
        try:
            os.makedirs(os.path.dirname(e["from"]), exist_ok=True)
            os.rename(e["to"], e["from"])
            _prune_empty(os.path.dirname(e["to"]), e["root"])
            log.info("moved back  %s  ->  %s", e["to"], e["from"])
            undone += 1
        except OSError as err:
            log.error("undo '%s' failed: %s", e["title"], err)
            keep.append(e)
    if not dry:
        keep.sort(key=lambda e: e["at"])
        tmp = state.moves_log + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(e) + "\n" for e in keep)
        os.replace(tmp, state.moves_log)
    log.info("undid %d moves from run %s", undone, last)
    return undone
