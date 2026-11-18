"""Cover artwork: custom asset covers and Kometa-style overlays (badges/banners).

Every cover we change is backed up first (state/originals), so overlays are
always drawn on the clean original and can be removed again with --reset-covers.
"""
import hashlib
import io
import json
import logging
import os
import re
from collections import defaultdict

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from .api import ABSError
from .items import matches, render_template

log = logging.getLogger("shelfkeeper.artwork")

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def sha(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _ext(path, default=".jpg"):
    e = os.path.splitext(path or "")[1].lower()
    return e if e in IMAGE_EXTS else default


def sanitize(name):
    return re.sub(r'[<>:"/\\|?*]', "", str(name)).strip().rstrip(".")


# --------------------------------------------------------------- assets --
class AssetIndex:
    """Finds a custom cover in the asset folder for a book.

    Lookup order (file name without extension, case-insensitive, any subfolder):
    ASIN, ISBN, ABS item id, "Author - Title", "Title".
    """

    def __init__(self, root):
        self.map = {}
        if root and os.path.isdir(root):
            for dirpath, _, files in os.walk(root):
                for fn in files:
                    stem, ext = os.path.splitext(fn)
                    if ext.lower() in IMAGE_EXTS:
                        self.map.setdefault(stem.lower(), os.path.join(dirpath, fn))
        log.info("asset folder %s: %d images", root, len(self.map))

    def find(self, book):
        keys = [book.asin, book.isbn, book.id]
        if book.authors:
            keys.append(f"{book.authors[0]} - {book.title}")
        keys.append(book.title)
        for k in keys:
            if k:
                p = self.map.get(sanitize(k).lower())
                if p:
                    return p
        return None


# ------------------------------------------------------------- overlays --
_font_cache = {}


def _font(path, size, config_dir):
    if path and not os.path.isabs(path):
        path = os.path.join(config_dir, path)
    key = (path, size)
    if key not in _font_cache:
        for candidate in (path, DEFAULT_FONT):
            if candidate and os.path.exists(candidate):
                _font_cache[key] = ImageFont.truetype(candidate, size)
                break
        else:
            _font_cache[key] = ImageFont.load_default(size=size)
    return _font_cache[key]


def _rgba(color):
    c = ImageColor.getrgb(color)
    return c if len(c) == 4 else (*c, 255)


def _position(pos):
    vert = "top" if pos.startswith("top") else "bottom" if pos.startswith("bottom") else "middle"
    horiz = "left" if pos.endswith("left") else "right" if pos.endswith("right") else "center"
    return vert, horiz


def render(base_bytes, overlays, config_dir):
    """overlays: list of (overlay dict, text or None). Returns (bytes, ext)."""
    src = Image.open(io.BytesIO(base_bytes))
    fmt = (src.format or "JPEG").upper()
    img = ImageOps.exif_transpose(src).convert("RGBA")
    W, H = img.size
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    stack = defaultdict(int)

    for ov, text in overlays:
        banner = bool(ov.get("banner"))
        margin = 0 if banner else int(W * float(ov.get("margin", 0.03)))
        vert, horiz = _position(ov.get("position", "top-right"))

        badge = font = None
        if ov.get("image"):
            p = ov["image"] if os.path.isabs(ov["image"]) else os.path.join(config_dir, ov["image"])
            badge = Image.open(p).convert("RGBA")
            bw = W if banner else max(1, int(W * float(ov.get("image_width", 0.25))))
            badge = badge.resize((bw, max(1, round(badge.height * bw / badge.width))), Image.Resampling.LANCZOS)
            box_w, box_h = badge.size
        else:
            font = _font(ov.get("font"), max(8, int(W * float(ov.get("font_size", 0.07)))), config_dir)
            l, t, r, b = draw.textbbox((0, 0), text, font=font)
            tw, th = r - l, b - t
            pad = int(font.size * float(ov.get("padding", 0.35)))
            box_w = W if banner else tw + 2 * pad
            box_h = th + 2 * pad

        if banner or horiz == "center":
            x = (W - box_w) // 2
        elif horiz == "left":
            x = margin
        else:
            x = W - box_w - margin

        key = (vert, "banner" if banner else horiz)
        off = stack[key]
        if vert == "top":
            y = margin + off
        elif vert == "bottom":
            y = H - box_h - margin - off
        else:
            y = (H - box_h) // 2 + off
        stack[key] += box_h + max(2, margin // 2)

        if badge is not None:
            layer.alpha_composite(badge, (x, y))
        else:
            radius = 0 if banner else int(box_h * float(ov.get("radius", 0.3)))
            draw.rounded_rectangle([x, y, x + box_w - 1, y + box_h - 1], radius=radius,
                                   fill=_rgba(ov.get("background", "#000000B3")))
            draw.text((x + (box_w - tw) // 2 - l, y + pad - t), text, font=font,
                      fill=_rgba(ov.get("color", "#FFFFFF")))

    out = Image.alpha_composite(img, layer)
    buf = io.BytesIO()
    if fmt == "PNG":
        out.save(buf, "PNG", optimize=True)
        return buf.getvalue(), ".png"
    out.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue(), ".jpg"


# ------------------------------------------------------------- pipeline --
def _matched_overlays(book, overlays):
    out = []
    for ov in overlays:
        if not matches(book, ov.get("filter")):
            continue
        text = None
        if not ov.get("image"):
            text = render_template(ov["text"], book).strip()
            if not text:
                continue
        out.append((ov, text))
    return out


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def process_covers(api, library, books, lib_cfg, state, settings, config_dir, assets):
    covers, dry = state.covers, settings["dry_run"]
    stats = defaultdict(int)
    for book in books:
        try:
            result = _process_one(api, book, lib_cfg.overlays, assets, covers,
                                  state.originals_dir, config_dir, dry)
            stats[result] += 1
        except (ABSError, OSError, ValueError) as e:
            log.error("cover for '%s': %s", book.title, e)
            stats["errors"] += 1
    stats.pop("skip", None)
    log.info("[%s] covers: %s", library["name"],
             ", ".join(f"{k} {v}" for k, v in sorted(stats.items())) or "nothing to do")


def _process_one(api, book, overlays, assets, covers, orig_dir, config_dir, dry):
    matched = _matched_overlays(book, overlays)
    asset_path = assets.find(book) if assets else None
    asset = _read(asset_path) if asset_path else None
    st = covers.get(book.id)
    if not matched and asset is None and st is None:
        return "skip"

    sig = sha(json.dumps({"asset": sha(asset), "overlays": matched},
                         sort_keys=True, default=str).encode())
    if st and st.get("sig") == sig and st.get("updated_at") == book.updated_at:
        return "unchanged"

    current = api.get_cover_raw(book.id) if book.cover_path else None
    cur_hash = sha(current)

    if st and cur_hash == st.get("applied_hash"):
        # cover is still the one we uploaded: work from the saved original
        orig_file = st.get("original_file")
        original = _read(os.path.join(orig_dir, orig_file)) if orig_file else None
        orig_ext = st.get("original_ext", ".jpg")
    else:
        # first time, or someone changed the cover in ABS: that is the new original
        original, orig_ext = current, _ext(book.cover_path)
        orig_file = f"{book.id}{orig_ext}" if original is not None else None
        if not dry:
            for e in IMAGE_EXTS:
                p = os.path.join(orig_dir, f"{book.id}{e}")
                if os.path.exists(p):
                    os.remove(p)
            if original is not None:
                with open(os.path.join(orig_dir, orig_file), "wb") as f:
                    f.write(original)

    if not matched and asset is None:
        # nothing applies any more: put the original back
        if original is not None:
            if sha(original) != cur_hash:
                api.upload_cover(book.id, original, "cover" + orig_ext, book.title)
        else:
            api.delete_cover(book.id, book.title)
        covers.pop(book.id, None)
        if not dry and orig_file and os.path.exists(os.path.join(orig_dir, orig_file)):
            os.remove(os.path.join(orig_dir, orig_file))
        log.info("restored original cover: %s", book.title)
        return "restored"

    if asset is not None:
        base, base_ext = asset, _ext(asset_path)
    else:
        base, base_ext = original, orig_ext
    if base is None:
        return "no cover"

    out, out_ext = render(base, matched, config_dir) if matched else (base, base_ext)
    out_hash = sha(out)
    updated_at = book.updated_at
    if out_hash != cur_hash:
        api.upload_cover(book.id, out, "cover" + out_ext, book.title)
        if not dry:
            updated_at = api.get_item(book.id).get("updatedAt", updated_at)
        log.info("cover %s: %s", "asset + overlays" if asset and matched else
                 "asset" if asset else "overlays", book.title)
        result = "updated"
    else:
        result = "unchanged"

    covers[book.id] = {
        "sig": sig,
        "applied_hash": out_hash,
        "updated_at": updated_at,
        "original_file": orig_file,
        "original_ext": orig_ext,
    }
    return result


def reset_covers(api, state, dry):
    """Restore every cover we changed and forget them."""
    n = 0
    for item_id, st in list(state.covers.items()):
        try:
            orig_file = st.get("original_file")
            path = os.path.join(state.originals_dir, orig_file) if orig_file else None
            if path and os.path.exists(path):
                api.upload_cover(item_id, _read(path), "cover" + st.get("original_ext", ".jpg"))
                if not dry:
                    os.remove(path)
            else:
                api.delete_cover(item_id)
            state.covers.pop(item_id)
            n += 1
        except ABSError as e:
            log.error("reset %s: %s", item_id, e)
    log.info("restored %d covers", n)
