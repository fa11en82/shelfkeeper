"""Build the desired collections from config and sync them to Audiobookshelf."""
import logging
from dataclasses import dataclass

from .api import ABSError
from .items import matches, parse_sort, sort_key, render_template, _as_list

log = logging.getLogger("shelfkeeper.collections")

DEFAULT_DYNAMIC_SORT = {
    "author": "series.asc",
    "narrator": "author.asc",
    "series": "series.asc",
    "genre": "title.asc",
    "tag": "title.asc",
    "publisher": "author.asc",
    "language": "title.asc",
    "decade": "published.asc",
    "year": "title.asc",
}


@dataclass
class Desired:
    ids: list
    description: str  # None = leave description alone
    minimum: int
    sync_mode: str


def _single_series(f):
    if isinstance(f, dict) and isinstance(f.get("series"), str):
        return f["series"]
    return None


def _finish(books, spec, prefer_series, desc_vars):
    fld, reverse = parse_sort(spec.get("sort", "title.asc"))
    books = sorted(books, key=lambda b: sort_key(b, fld, prefer_series), reverse=reverse)
    if spec.get("limit"):
        books = books[: int(spec["limit"])]
    desc = spec.get("description")
    if desc is not None:
        desc = render_template(desc, count=len(books), **desc_vars)
    return Desired(
        ids=[b.id for b in books],
        description=desc,
        minimum=int(spec.get("minimum_items", 1)),
        sync_mode=spec.get("sync_mode", "sync"),
    )


def build_desired(books, lib_cfg):
    desired = {}

    for name, spec in lib_cfg.collections.items():
        sel = [b for b in books if matches(b, spec.get("filter"))]
        desired[name] = _finish(sel, spec, _single_series(spec.get("filter")), {"value": name})

    for dyn in lib_cfg.dynamic_collections:
        gtype = dyn["type"]
        base = [b for b in books if matches(b, dyn.get("filter"))]
        groups = {}
        for b in base:
            for v in dict.fromkeys(b.values(gtype)):  # de-dupe per book
                groups.setdefault(v.lower(), [v, []])[1].append(b)
        exclude = {x.lower() for x in _as_list(dyn.get("exclude") or [])}
        include = {x.lower() for x in _as_list(dyn.get("include") or [])}
        min_items = int(dyn.get("min_items", 2))
        spec = {
            "sort": dyn.get("sort", DEFAULT_DYNAMIC_SORT[gtype]),
            "limit": dyn.get("limit"),
            "description": dyn.get("description"),
            "sync_mode": dyn.get("sync_mode", "sync"),
            "minimum_items": 1,
        }
        made = 0
        for key, (value, group) in sorted(groups.items()):
            if key in exclude or (include and key not in include) or len(group) < min_items:
                continue
            name = render_template(dyn.get("name", "{value}"), value=value).strip()
            if not name:
                continue
            if name in desired:
                log.warning("dynamic collection '%s' (%s) overrides an earlier definition", name, gtype)
            prefer = value if gtype == "series" else None
            desired[name] = _finish(group, spec, prefer, {"value": value})
            made += 1
        log.info("dynamic %s: %d collections from %d candidate values", gtype, made, len(groups))

    return desired


def sync_collections(api, library, books, lib_cfg, state, settings):
    lib_id, lib_name = library["id"], library["name"]
    desired = build_desired(books, lib_cfg)
    existing = {}
    for c in api.collections(lib_id):
        existing.setdefault(c["name"], c)
    managed = state.collections(lib_id)
    stats = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0, "skipped": 0, "errors": 0}

    for name, d in desired.items():
        col = existing.get(name)
        try:
            if len(d.ids) < d.minimum:
                if col and name in managed and settings["delete_below_minimum"] \
                        and not managed[name].get("adopted"):
                    api.delete_collection(col["id"], name)
                    managed.pop(name, None)
                    stats["deleted"] += 1
                else:
                    stats["skipped"] += 1
                log.debug("'%s' has %d books (< %d), skipped", name, len(d.ids), d.minimum)
                continue

            if col is None:
                if not d.ids:  # ABS can't create an empty collection
                    stats["skipped"] += 1
                    continue
                created = api.create_collection(lib_id, name, d.description, d.ids)
                managed[name] = {"id": created["id"], "adopted": False}
                log.info("created '%s' (%d books)", name, len(d.ids))
                stats["created"] += 1
                continue

            if name not in managed:
                managed[name] = {"id": col["id"], "adopted": True}
                log.info("adopting existing collection '%s'", name)
            managed[name]["id"] = col["id"]

            current = [b["id"] if isinstance(b, dict) else b for b in col.get("books") or []]
            cur_set = set(current)
            if d.sync_mode == "append":
                want = set(d.ids)
                final = d.ids + [i for i in current if i not in want]
            else:
                final = d.ids
            final_set = set(final)
            to_add = [i for i in final if i not in cur_set]
            to_remove = [i for i in current if i not in final_set]

            if to_add:
                api.add_books(col["id"], name, to_add)
            if to_remove:
                api.remove_books(col["id"], name, to_remove)
            after = [i for i in current if i in final_set] + to_add
            reorder = after != final
            new_desc = None
            if d.description is not None and (col.get("description") or "") != d.description:
                new_desc = d.description
            if reorder or new_desc is not None:
                api.update_collection(col["id"], name, books=final if reorder else None,
                                      description=new_desc)

            if to_add or to_remove or reorder or new_desc is not None:
                log.info("updated '%s' (+%d -%d%s%s)", name, len(to_add), len(to_remove),
                         ", reordered" if reorder else "", ", description" if new_desc else "")
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1
        except ABSError as e:
            log.error("collection '%s': %s", name, e)
            stats["errors"] += 1

    # collections we created that are no longer wanted
    for name in list(managed):
        if name in desired:
            continue
        m = managed[name]
        col = existing.get(name)
        if col and col["id"] == m["id"] and not m.get("adopted") and settings["delete_removed_collections"]:
            try:
                api.delete_collection(col["id"], name)
                log.info("deleted '%s' (no longer in config)", name)
                stats["deleted"] += 1
            except ABSError as e:
                log.error("delete '%s': %s", name, e)
                stats["errors"] += 1
                continue
        managed.pop(name)

    log.info("[%s] collections: %s", lib_name, ", ".join(f"{k} {v}" for k, v in stats.items()))
