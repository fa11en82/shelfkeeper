"""Small web UI: status, run buttons, live log, config editor, history."""
import hmac
import json
import logging
import os
import shutil
import time

from flask import Flask, Response, jsonify, request, send_file

from . import __version__
from .config import load
from .items import ConfigError
from .runner import LOG_BUFFER, STEPS
from .state import State

log = logging.getLogger("shelfkeeper.web")
HERE = os.path.dirname(os.path.abspath(__file__))


def create_app(runner, config_path):
    app = Flask(__name__, static_folder=None)
    config_dir = os.path.dirname(os.path.abspath(config_path))
    state_dir = os.path.join(config_dir, "state")
    user = os.environ.get("WEBUI_USER", "admin")
    password = os.environ.get("WEBUI_PASSWORD", "")

    @app.before_request
    def guard():
        if password:
            a = request.authorization
            ok = a is not None and hmac.compare_digest(a.username or "", user) \
                and hmac.compare_digest(a.password or "", password)
            if not ok:
                return Response("Login required", 401,
                                {"WWW-Authenticate": 'Basic realm="shelfkeeper"'})
        # blocks cross-site form posts: browsers can't add custom headers without CORS
        if request.method != "GET" and request.headers.get("X-Shelfkeeper") != "1":
            return jsonify(error="missing X-Shelfkeeper header"), 400
        return None

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        return send_file(os.path.join(HERE, "web", "index.html"))

    # ------------------------------------------------------------ status --
    @app.get("/api/status")
    def status():
        try:
            c = load(config_path)
            cfg = {"ok": True, "dry_run": c.settings["dry_run"], "schedule": c.settings["schedule"],
                   "libraries": list(c.libraries), "url": c.url}
        except ConfigError as e:
            cfg = {"ok": False, "error": str(e)}
        return jsonify(version=__version__, running=runner.current, last=runner.last,
                       next_run=runner.next_run, config=cfg, steps=list(STEPS), now=time.time())

    @app.post("/api/run")
    def run():
        body = request.get_json(silent=True) or {}
        action = body.get("action", "run")
        if action not in ("run", "undo_moves", "reset_covers"):
            return jsonify(error="unknown action"), 400
        only = [s for s in body.get("only") or [] if s in STEPS]
        dry = body.get("dry_run")
        kw = {"dry_run": None if dry is None else bool(dry)}
        if action == "undo_moves":
            kw["undo_moves"] = True
            label = "undo folder moves"
        elif action == "reset_covers":
            kw["reset_covers"] = True
            label = "reset covers"
        else:
            kw["only"] = only or None
            if body.get("library"):
                kw["library"] = [str(body["library"])]
            label = "manual: " + (", ".join(only) if only and len(only) < len(STEPS) else "all steps")
        if kw["dry_run"]:
            label += " (dry run)"
        if not runner.start(label, **kw):
            return jsonify(error="a run is already in progress"), 409
        return jsonify(started=label)

    @app.get("/api/logs")
    def logs():
        try:
            after = int(request.args.get("after", 0))
        except ValueError:
            after = 0
        lines, last = LOG_BUFFER.since(after)
        return jsonify(lines=lines[-1000:], last=last)

    # ------------------------------------------------------------ config --
    def _validate(text):
        tmp = os.path.join(config_dir, ".config.check.yml")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            c = load(tmp)
            return None, c
        except ConfigError as e:
            return str(e), None
        except Exception as e:  # noqa: BLE001  (YAML syntax errors etc.)
            return f"{type(e).__name__}: {e}", None
        finally:
            os.remove(tmp)

    @app.get("/api/config")
    def get_config():
        if not os.path.exists(config_path):
            return jsonify(text="", exists=False)
        with open(config_path, encoding="utf-8") as f:
            return jsonify(text=f.read(), exists=True)

    @app.post("/api/config/validate")
    def validate_config():
        err, c = _validate((request.get_json(silent=True) or {}).get("text", ""))
        if err:
            return jsonify(ok=False, error=err)
        return jsonify(ok=True, libraries=list(c.libraries), dry_run=c.settings["dry_run"])

    @app.put("/api/config")
    def save_config():
        text = (request.get_json(silent=True) or {}).get("text", "")
        err, _ = _validate(text)
        if err:
            return jsonify(ok=False, error=err), 400
        if os.path.exists(config_path):
            shutil.copy(config_path, config_path + ".bak")
        tmp = config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, config_path)
        log.info("config saved from the web UI (previous version in config.yml.bak)")
        return jsonify(ok=True)

    # ----------------------------------------------------------- history --
    @app.get("/api/state")
    def state():
        st = State(state_dir).data
        collections = []
        for lib_id, cols in st.get("collections", {}).items():
            for name, m in cols.items():
                collections.append({"library": lib_id, "name": name,
                                    "owner": "adopted" if m.get("adopted") else "created"})
        matches = st.get("matches", {})
        unmatched = sorted(
            ({"title": v.get("title") or k, "author": v.get("author", ""), "at": v.get("at")}
             for k, v in matches.items() if v.get("result") == "unmatched"),
            key=lambda x: x["title"].lower())
        moves = []
        moves_log = os.path.join(state_dir, "moves.jsonl")
        if os.path.exists(moves_log):
            with open(moves_log, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            moves.append(json.loads(line))
                        except ValueError:
                            pass
        for m in moves:
            for k in ("from", "to"):
                if m.get("root") and m[k].startswith(m["root"]):
                    m[k] = m[k][len(m["root"]):].lstrip("/")
        return jsonify(
            collections=sorted(collections, key=lambda c: c["name"].lower()),
            covers=len(st.get("covers", {})),
            matched=sum(1 for v in matches.values() if v.get("result") == "matched"),
            unmatched=unmatched,
            moves=list(reversed(moves))[:300],
            moves_total=len(moves),
        )

    return app
