"""Minimal Audiobookshelf API client."""
import logging
import mimetypes

import requests

log = logging.getLogger("shelfkeeper.api")


class ABSError(Exception):
    pass


class ABS:
    def __init__(self, url, token, timeout=60, dry_run=False):
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.dry_run = dry_run
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.s.headers["User-Agent"] = "shelfkeeper"

    # ---- low level -------------------------------------------------------
    def _req(self, method, path, ok404=False, **kw):
        url = self.base + path
        try:
            r = self.s.request(method, url, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise ABSError(f"{method} {path} failed: {e}") from e
        if ok404 and r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise ABSError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    def _write(self, desc):
        """Return True if a write should be skipped (dry run)."""
        if self.dry_run:
            log.info("[dry-run] %s", desc)
            return True
        return False

    # ---- reads -----------------------------------------------------------
    def libraries(self):
        data = self._req("GET", "/api/libraries").json()
        return data["libraries"] if isinstance(data, dict) else data

    def items(self, library_id, page_size=500):
        out, page = [], 0
        while True:
            data = self._req(
                "GET",
                f"/api/libraries/{library_id}/items",
                params={"limit": page_size, "page": page},
            ).json()
            results = data.get("results", [])
            out.extend(results)
            total = data.get("total", len(out))
            if not results or len(out) >= total:
                return out
            page += 1

    def collections(self, library_id):
        data = self._req("GET", f"/api/libraries/{library_id}/collections").json()
        return data.get("results", data.get("collections", []))

    def get_item(self, item_id):
        return self._req("GET", f"/api/items/{item_id}").json()

    def get_cover_raw(self, item_id):
        r = self._req("GET", f"/api/items/{item_id}/cover", ok404=True, params={"raw": 1})
        return r.content if r is not None else None

    # ---- collection writes ----------------------------------------------
    def create_collection(self, library_id, name, description, book_ids):
        if self._write(f"create collection '{name}' with {len(book_ids)} books"):
            return {"id": "dry-run", "name": name, "books": []}
        body = {"libraryId": library_id, "name": name, "books": book_ids}
        if description is not None:
            body["description"] = description
        return self._req("POST", "/api/collections", json=body).json()

    def add_books(self, collection_id, name, book_ids):
        if self._write(f"add {len(book_ids)} books to '{name}'"):
            return
        self._req("POST", f"/api/collections/{collection_id}/batch/add", json={"books": book_ids})

    def remove_books(self, collection_id, name, book_ids):
        if self._write(f"remove {len(book_ids)} books from '{name}'"):
            return
        self._req("POST", f"/api/collections/{collection_id}/batch/remove", json={"books": book_ids})

    def update_collection(self, collection_id, name, books=None, description=None):
        body = {}
        if books is not None:
            body["books"] = books  # reorders existing members only
        if description is not None:
            body["description"] = description
        if not body or self._write(f"update '{name}' ({', '.join(body)})"):
            return
        self._req("PATCH", f"/api/collections/{collection_id}", json=body)

    def delete_collection(self, collection_id, name):
        if self._write(f"delete collection '{name}'"):
            return
        self._req("DELETE", f"/api/collections/{collection_id}")

    # ---- cover writes ---------------------------------------------------
    def upload_cover(self, item_id, data, filename, label=""):
        if self._write(f"upload cover for {label or item_id}"):
            return
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self._req("POST", f"/api/items/{item_id}/cover", files={"cover": (filename, data, mime)})

    def delete_cover(self, item_id, label=""):
        if self._write(f"remove cover for {label or item_id}"):
            return
        self._req("DELETE", f"/api/items/{item_id}/cover", ok404=True)

    # ---- metadata / library ---------------------------------------------
    def get_library(self, library_id):
        return self._req("GET", f"/api/libraries/{library_id}").json()

    def match_item(self, item_id, label, provider, title=None, author=None, asin=None,
                   isbn=None, override_cover=False, override_details=False):
        """Run ABS's own quick match. Returns {"updated":..,"libraryItem":..} or {"warning":..}."""
        if self._write(f"match '{label}' via {provider}"):
            return {"dry_run": True}
        body = {"provider": provider, "overrideCover": override_cover,
                "overrideDetails": override_details}
        for k, v in (("title", title), ("author", author), ("asin", asin), ("isbn", isbn)):
            if v:
                body[k] = v
        return self._req("POST", f"/api/items/{item_id}/match", json=body).json()

    def update_media(self, item_id, label, metadata=None, tags=None):
        body = {}
        if metadata:
            body["metadata"] = metadata
        if tags is not None:
            body["tags"] = tags
        if not body or self._write(f"update metadata of '{label}': {body}"):
            return
        self._req("PATCH", f"/api/items/{item_id}/media", json=body)

    def scan_library(self, library_id, name):
        if self._write(f"scan library '{name}'"):
            return
        self._req("POST", f"/api/libraries/{library_id}/scan")
