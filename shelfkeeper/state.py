"""Persistent state: which collections we manage and which covers we changed."""
import json
import os


class State:
    def __init__(self, state_dir):
        self.dir = state_dir
        self.originals_dir = os.path.join(state_dir, "originals")
        os.makedirs(self.originals_dir, exist_ok=True)
        self.path = os.path.join(state_dir, "state.json")
        self.moves_log = os.path.join(state_dir, "moves.jsonl")
        self.data = {"collections": {}, "covers": {}, "matches": {}}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))

    def collections(self, library_id):
        """{collection name: {"id": ..., "adopted": bool}} for one library."""
        return self.data["collections"].setdefault(library_id, {})

    @property
    def covers(self):
        """{item id: {"sig", "applied_hash", "updated_at", "original_file", "original_ext"}}"""
        return self.data["covers"]

    @property
    def matches(self):
        """{item id: {"result": "matched"|"unmatched", "at": epoch, "provider": ...}}"""
        return self.data["matches"]

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)
