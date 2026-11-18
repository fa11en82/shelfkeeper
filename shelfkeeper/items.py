"""Book model, filters, sorting and text templates."""
import os
import re
import time
from dataclasses import dataclass


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- model --
@dataclass
class Series:
    name: str
    sequence: str
    number: float


def _split(s):
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def _seq_num(seq):
    m = re.match(r"\d+(?:\.\d+)?", str(seq or ""))
    return float(m.group()) if m else float("inf")


_SERIES_RE = re.compile(r"^(.*?)\s+#\s*(\S+)$")


def _parse_series(meta):
    out = []
    if isinstance(meta.get("series"), list):
        for s in meta["series"]:
            if isinstance(s, dict) and s.get("name"):
                seq = str(s.get("sequence") or "")
                out.append(Series(s["name"], seq, _seq_num(seq)))
        return out
    for part in _split(meta.get("seriesName")):
        m = _SERIES_RE.match(part)
        name, seq = (m.group(1), m.group(2)) if m else (part, "")
        out.append(Series(name.strip(), seq, _seq_num(seq)))
    return out


def _names(meta, list_key, str_key):
    v = meta.get(list_key)
    if isinstance(v, list):
        return [x["name"] if isinstance(x, dict) else str(x) for x in v if x]
    return _split(meta.get(str_key))


@dataclass
class Book:
    id: str
    title: str
    sort_title: str
    subtitle: str
    authors: list
    author_sort: str
    narrators: list
    series: list
    genres: list
    tags: list
    year: int
    published_date: str
    publisher: str
    language: str
    explicit: bool
    abridged: bool
    asin: str
    isbn: str
    duration: float
    added_at: int
    updated_at: int
    cover_path: str
    missing: bool

    @classmethod
    def from_api(cls, item):
        media = item.get("media") or {}
        meta = media.get("metadata") or {}
        title = meta.get("title") or os.path.basename(item.get("path") or "") or item["id"]
        year = None
        m = re.search(r"\d{4}", str(meta.get("publishedYear") or meta.get("publishedDate") or ""))
        if m:
            year = int(m.group())
        authors = _names(meta, "authors", "authorName")
        return cls(
            id=item["id"],
            title=title,
            sort_title=meta.get("titleIgnorePrefix") or title,
            subtitle=meta.get("subtitle") or "",
            authors=authors,
            author_sort=meta.get("authorNameLF") or ", ".join(authors),
            narrators=_names(meta, "narrators", "narratorName"),
            series=_parse_series(meta),
            genres=list(meta.get("genres") or []),
            tags=list(media.get("tags") or []),
            year=year,
            published_date=meta.get("publishedDate") or "",
            publisher=meta.get("publisher") or "",
            language=meta.get("language") or "",
            explicit=bool(meta.get("explicit")),
            abridged=bool(meta.get("abridged")),
            asin=meta.get("asin") or "",
            isbn=meta.get("isbn") or "",
            duration=float(media.get("duration") or 0),
            added_at=int(item.get("addedAt") or 0),
            updated_at=int(item.get("updatedAt") or 0),
            cover_path=media.get("coverPath") or "",
            missing=bool(item.get("isMissing") or item.get("isInvalid")),
        )

    # values used by filters / dynamic grouping
    def values(self, fld):
        if fld == "author":
            return self.authors
        if fld == "narrator":
            return self.narrators
        if fld == "series":
            return [s.name for s in self.series]
        if fld == "genre":
            return self.genres
        if fld == "tag":
            return self.tags
        if fld == "decade":
            return [f"{self.year // 10 * 10}s"] if self.year else []
        if fld == "year":
            return [str(self.year)] if self.year else []
        v = getattr(self, fld, "")
        return [v] if v else []

    def template_vars(self):
        s = self.series[0] if self.series else None
        h, rem = divmod(int(self.duration), 3600)
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "author": self.authors[0] if self.authors else "",
            "authors": ", ".join(self.authors),
            "narrator": self.narrators[0] if self.narrators else "",
            "narrators": ", ".join(self.narrators),
            "series": s.name if s else "",
            "series_sequence": s.sequence if s else "",
            "year": self.year or "",
            "decade": f"{self.year // 10 * 10}s" if self.year else "",
            "publisher": self.publisher,
            "language": self.language,
            "genre": self.genres[0] if self.genres else "",
            "duration_hours": round(self.duration / 3600) if self.duration else "",
            "duration": f"{h}h {rem // 60}m" if self.duration else "",
            "asin": self.asin,
        }


class SafeDict(dict):
    def __missing__(self, key):
        return ""


def render_template(template, book=None, **extra):
    vars_ = book.template_vars() if book else {}
    vars_.update(extra)
    try:
        return str(template).format_map(SafeDict(vars_))
    except (ValueError, IndexError) as e:
        raise ConfigError(f"bad template '{template}': {e}") from e


# -------------------------------------------------------------- filters --
TEXT_FIELDS = {"author", "narrator", "series", "genre", "tag", "title", "subtitle",
               "publisher", "language", "asin", "isbn", "decade"}
GROUP_TYPES = {"author", "narrator", "series", "genre", "tag", "publisher", "language",
               "decade", "year"}
NUM_KEYS = {"year_min", "year_max", "duration_min", "duration_max",
            "added_within_days", "added_before_days", "published_within_years"}
BOOL_KEYS = {"explicit", "abridged", "has_series", "has_cover"}
LOGIC_KEYS = {"any", "all", "not"}


def _filter_keys():
    keys = set(NUM_KEYS | BOOL_KEYS | LOGIC_KEYS)
    for f in TEXT_FIELDS:
        keys |= {f, f + "_contains", f + "_regex"}
    return keys


ALL_FILTER_KEYS = _filter_keys()


def _as_list(v):
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def validate_filter(f, where="filter"):
    if not isinstance(f, dict):
        raise ConfigError(f"{where}: expected a mapping")
    for k, v in f.items():
        if k not in ALL_FILTER_KEYS:
            raise ConfigError(f"{where}: unknown filter '{k}'")
        if k in ("any", "all"):
            if not isinstance(v, list):
                raise ConfigError(f"{where}.{k}: expected a list of filters")
            for i, sub in enumerate(v):
                validate_filter(sub, f"{where}.{k}[{i}]")
        elif k == "not":
            validate_filter(v, f"{where}.not")
        elif k in NUM_KEYS and not isinstance(v, (int, float)):
            raise ConfigError(f"{where}.{k}: expected a number")
        elif k in BOOL_KEYS and not isinstance(v, bool):
            raise ConfigError(f"{where}.{k}: expected true/false")
        elif k.endswith("_regex"):
            for p in _as_list(v):
                try:
                    re.compile(p)
                except re.error as e:
                    raise ConfigError(f"{where}.{k}: bad regex '{p}': {e}") from e


def matches(book, f, now=None):
    if not f:
        return True
    now = now or time.time()
    for k, want in f.items():
        if k == "any":
            if not any(matches(book, sub, now) for sub in want):
                return False
        elif k == "all":
            if not all(matches(book, sub, now) for sub in want):
                return False
        elif k == "not":
            if matches(book, want, now):
                return False
        elif k == "year_min":
            if not book.year or book.year < want:
                return False
        elif k == "year_max":
            if not book.year or book.year > want:
                return False
        elif k == "duration_min":
            if book.duration < want * 3600:
                return False
        elif k == "duration_max":
            if book.duration > want * 3600:
                return False
        elif k == "added_within_days":
            if book.added_at / 1000 < now - want * 86400:
                return False
        elif k == "added_before_days":
            if book.added_at / 1000 > now - want * 86400:
                return False
        elif k == "published_within_years":
            if not book.year or book.year < time.localtime(now).tm_year - want:
                return False
        elif k == "explicit":
            if book.explicit != want:
                return False
        elif k == "abridged":
            if book.abridged != want:
                return False
        elif k == "has_series":
            if bool(book.series) != want:
                return False
        elif k == "has_cover":
            if bool(book.cover_path) != want:
                return False
        else:
            if k.endswith("_contains"):
                fld, mode = k[: -len("_contains")], "contains"
            elif k.endswith("_regex"):
                fld, mode = k[: -len("_regex")], "regex"
            else:
                fld, mode = k, "exact"
            vals = [v.lower() for v in book.values(fld)]
            wants = _as_list(want)
            if mode == "exact":
                ok = any(w.lower() in vals for w in wants)
            elif mode == "contains":
                ok = any(w.lower() in v for w in wants for v in vals)
            else:
                ok = any(re.search(w, v, re.I) for w in wants for v in book.values(fld))
            if not ok:
                return False
    return True


# -------------------------------------------------------------- sorting --
SORT_FIELDS = {"title", "author", "narrator", "published", "added", "duration", "series"}


def parse_sort(s, where="sort"):
    s = str(s).strip().replace(" ", ".")
    fld, _, direction = s.partition(".")
    direction = direction or "asc"
    if fld not in SORT_FIELDS or direction not in ("asc", "desc"):
        raise ConfigError(f"{where}: sort must be <{'|'.join(sorted(SORT_FIELDS))}>.<asc|desc>, got '{s}'")
    return fld, direction == "desc"


def sort_key(book, fld, prefer_series=None):
    t = book.sort_title.lower()
    if fld == "title":
        return (t,)
    if fld == "author":
        return (book.author_sort.lower(), t)
    if fld == "narrator":
        return ((book.narrators[0] if book.narrators else "").lower(), t)
    if fld == "published":
        return (book.year or 0, book.published_date, t)
    if fld == "added":
        return (book.added_at, t)
    if fld == "duration":
        return (book.duration, t)
    # series
    s = None
    if prefer_series:
        s = next((x for x in book.series if x.name.lower() == prefer_series.lower()), None)
    if s is None and book.series:
        s = book.series[0]
    if s is None:
        return (1, "", 0.0, t)
    return (0, s.name.lower(), s.number, t)
