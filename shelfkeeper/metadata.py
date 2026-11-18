"""Metadata: match books with ABS's own providers, then clean fields up with rules.

Everything is written through the Audiobookshelf API (ABS then rewrites its own
metadata.json), so a later library scan won't undo the changes.
"""
import logging
import re
import time

from .api import ABSError
from .items import Book, matches, _as_list

log = logging.getLogger("shelfkeeper.metadata")

MISSING_CHECKS = {
    "asin": lambda b: not b.asin,
    "isbn": lambda b: not b.isbn,
    "description": lambda b: not b.description,
    "narrator": lambda b: not b.narrators,
    "author": lambda b: not b.authors,
    "genres": lambda b: not b.genres,
    "series": lambda b: not b.series,
    "year": lambda b: not b.year,
    "publisher": lambda b: not b.publisher,
    "cover": lambda b: not b.cover_path,
}


# ------------------------------------------------------------- matching --
def run_matching(api, books, mcfg, state, dry):
    """Quick-match books that are missing data. Returns how many were updated."""
    if not mcfg or not mcfg.get("enabled", True):
        return 0
    providers = _as_list(mcfg.get("providers", ["audible"]))
    missing = _as_list(mcfg.get("when_missing", ["asin"]))
    retry = float(mcfg.get("retry_unmatched_days", 30)) * 86400
    now = time.time()

    todo = []
    for b in books:
        if not matches(b, mcfg.get("filter")):
            continue
        if missing and not any(MISSING_CHECKS[m](b) for m in missing):
            continue
        st = state.matches.get(b.id)
        if st and st["result"] == "matched" and not mcfg.get("rematch"):
            continue  # already matched once; don't loop on fields the provider lacks
        if st and st["result"] == "unmatched" and now - st["at"] < retry:
            continue
        todo.append(b)

    limit = int(mcfg.get("max_per_run", 100))
    log.info("match: %d books need matching%s", len(todo),
             f" (doing {limit} this run)" if len(todo) > limit else "")
    updated = unmatched = 0
    for b in todo[:limit]:
        result, used = "unmatched", None
        for prov in providers:
            try:
                r = api.match_item(
                    b.id, b.title, prov,
                    title=b.title,
                    author=b.authors[0] if b.authors else None,
                    asin=b.asin or None,
                    isbn=b.isbn or None,
                    override_cover=bool(mcfg.get("override_cover", False)),
                    override_details=bool(mcfg.get("override_details", False)),
                )
            except ABSError as e:
                log.warning("match '%s' via %s failed: %s", b.title, prov, e)
                continue
            if r.get("dry_run"):
                result = None
                break
            if r.get("libraryItem") is not None:
                result, used = "matched", prov
                if r.get("updated"):
                    updated += 1
                    log.info("matched '%s' via %s", b.title, prov)
                break
        if result == "unmatched":
            unmatched += 1
            log.info("no match for '%s' (tried %s)", b.title, ", ".join(providers))
        if result and not dry:
            state.matches[b.id] = {"result": result, "at": now, "provider": used,
                                   "title": b.title, "author": ", ".join(b.authors)}
    log.info("match: %d updated, %d unmatched", updated, unmatched)
    return updated


# -------------------------------------------------------------- cleanup --
def normalize_initials(name):
    """'J R R Tolkien' / 'J. R. R. Tolkien' / 'J.R.R.Tolkien' -> 'J.R.R. Tolkien'."""
    name = re.sub(r"(?<=[A-Z]\.)(?=[A-Z][a-z])", " ", name.strip())  # 'J.R.R.Tolkien'
    out, buf = [], ""
    for tok in name.split():
        if re.fullmatch(r"(?:[A-Z]\.?)+", tok) and (len(tok) == 1 or "." in tok):
            buf += "".join(c + "." for c in tok if c != ".")
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(tok)
    if buf:
        out.append(buf)
    return " ".join(out)


def _smart_title(s):
    return " ".join(w[:1].upper() + w[1:] if w.islower() else w for w in s.split(" "))


def _lower_map(m):
    return {str(k).lower(): v for k, v in (m or {}).items()}


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


class Cleaner:
    def __init__(self, c):
        self.genre_map = {k: _as_list(v) for k, v in _lower_map(c.get("genre_map")).items()}
        self.drop_genres = {x.lower() for x in _as_list(c.get("drop_genres") or [])}
        self.split_genres = bool(c.get("split_genres", False))
        self.genre_case = c.get("genre_case", "none")
        self.max_genres = c.get("max_genres")
        self.tag_map = {k: _as_list(v) for k, v in _lower_map(c.get("tag_map")).items()}
        self.drop_tags = {x.lower() for x in _as_list(c.get("drop_tags") or [])}
        self.add_tags = _as_list(c.get("add_tags") or [])
        self.author_map = _lower_map(c.get("author_map"))
        self.narrator_map = _lower_map(c.get("narrator_map"))
        self.series_map = _lower_map(c.get("series_map"))
        self.publisher_map = _lower_map(c.get("publisher_map"))
        self.language_map = _lower_map(c.get("language_map"))
        self.initials = bool(c.get("normalize_initials", False))
        self.title_patterns = [re.compile(p) for p in _as_list(c.get("strip_title_patterns") or [])]

    def _person(self, name, mapping):
        name = " ".join(name.split())
        if self.initials:
            name = normalize_initials(name)
        return str(mapping.get(name.lower(), name))

    def _genres(self, genres):
        out = []
        for g in genres:
            g = " ".join(str(g).split())
            if g.lower() in self.genre_map:
                out += self.genre_map[g.lower()]
                continue
            parts = re.split(r"\s*(?:&|/|;)\s*", g) if self.split_genres else [g]
            for p in parts:
                out += self.genre_map.get(p.lower(), [p])
        if self.genre_case == "title":
            out = [_smart_title(g) for g in out]
        out = [g for g in _dedupe(out) if g.lower() not in self.drop_genres]
        return out[: int(self.max_genres)] if self.max_genres else out

    def _tags(self, tags):
        out = []
        for t in tags:
            out += self.tag_map.get(str(t).lower(), [t])
        out = [t for t in _dedupe(out + self.add_tags) if t.lower() not in self.drop_tags]
        return out

    def clean(self, b):
        """Return (metadata changes dict, new tags list or None)."""
        meta = {}
        title = b.title
        for p in self.title_patterns:
            title = p.sub("", title)
        title = " ".join(title.split())
        if title and title != b.title:
            meta["title"] = title

        authors = _dedupe([self._person(a, self.author_map) for a in b.authors])
        if authors != b.authors:
            meta["authors"] = [{"name": a} for a in authors]

        narrators = _dedupe([self._person(n, self.narrator_map) for n in b.narrators])
        if narrators != b.narrators:
            meta["narrators"] = narrators

        series = [(str(self.series_map.get(s.name.lower(), s.name)), s.sequence) for s in b.series]
        if [n for n, _ in series] != [s.name for s in b.series]:
            seen, out = set(), []
            for n, seq in series:
                if n.lower() not in seen:
                    seen.add(n.lower())
                    out.append({"name": n, "sequence": seq or None})
            meta["series"] = out

        genres = self._genres(b.genres)
        if genres != b.genres:
            meta["genres"] = genres

        if b.publisher and b.publisher.lower() in self.publisher_map:
            v = str(self.publisher_map[b.publisher.lower()])
            if v != b.publisher:
                meta["publisher"] = v
        if b.language and b.language.lower() in self.language_map:
            v = str(self.language_map[b.language.lower()])
            if v != b.language:
                meta["language"] = v

        tags = self._tags(b.tags)
        return meta, (tags if tags != b.tags else None)


def run_cleanup(api, books, ccfg, dry):
    if not ccfg or not ccfg.get("enabled", True):
        return 0
    cleaner = Cleaner(ccfg)
    changed = 0
    for b in books:
        if not matches(b, ccfg.get("filter")):
            continue
        meta, tags = cleaner.clean(b)
        if not meta and tags is None:
            continue
        try:
            if "authors" in meta or "narrators" in meta or "series" in meta:
                # list results join names with commas; re-check against the full item
                full = Book.from_api(api.get_item(b.id))
                meta, tags = cleaner.clean(full)
                if not meta and tags is None:
                    continue
            api.update_media(b.id, b.title, meta or None, tags)
            if not dry:
                log.info("cleaned '%s': %s", b.title,
                         ", ".join(list(meta) + (["tags"] if tags is not None else [])))
            changed += 1
        except ABSError as e:
            log.error("cleanup '%s': %s", b.title, e)
    log.info("cleanup: %d books %s", changed, "would change" if dry else "changed")
    return changed
