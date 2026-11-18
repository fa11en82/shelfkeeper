"""One run of every step, plus a scheduler and a lock so only one run happens at a time."""
import datetime as dt
import logging
import os
import threading
import time
import types
from collections import deque

from .api import ABS, ABSError
from .artwork import AssetIndex, process_covers, reset_covers
from .collections import sync_collections
from .config import load
from .folders import run_folders, undo_moves
from .items import Book, ConfigError
from .metadata import run_matching, run_cleanup
from .state import State

log = logging.getLogger("shelfkeeper")

STEPS = ("metadata", "folders", "collections", "covers")


# ------------------------------------------------------------ log buffer --
class LogBuffer(logging.Handler):
    """Keeps recent log lines in memory for the web UI."""

    def __init__(self, size=5000):
        super().__init__()
        self.lines = deque(maxlen=size)
        self.seq = 0
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self.seq += 1
            self.lines.append((self.seq, record.levelname, msg))

    def since(self, after):
        with self._lock:
            return [{"seq": s, "level": lv, "text": m} for s, lv, m in self.lines if s > after], self.seq


LOG_BUFFER = LogBuffer()


def make_args(config, **kw):
    d = dict(config=config, run=True, dry_run=None, library=None, only=None,
             collections_only=False, covers_only=False, reset_covers=False,
             undo_moves=False, debug=False)
    d.update(kw)
    return types.SimpleNamespace(**d)


# -------------------------------------------------------------- one run --
def run_once(args):
    cfg = load(args.config)
    settings = cfg.settings
    if args.dry_run is not None:
        settings["dry_run"] = bool(args.dry_run)
    dry = settings["dry_run"]
    started = time.time()
    log.info("=== run started%s ===", " (DRY RUN - nothing will be changed)" if dry else "")

    api = ABS(cfg.url, cfg.token, timeout=settings["request_timeout"], dry_run=dry)
    state = State(os.path.join(cfg.config_dir, "state"))

    if args.reset_covers:
        reset_covers(api, state, dry)
        if not dry:
            state.save()
        return

    libs = {lib["name"]: lib for lib in api.libraries()}

    if args.undo_moves:
        if undo_moves(state, dry) and not dry:
            for name in cfg.libraries:
                if name in libs:
                    api.scan_library(libs[name]["id"], name)
        return

    only = set(args.only or [])
    if args.collections_only:
        only.add("collections")
    if args.covers_only:
        only.add("covers")

    def want(step):
        return not only or step in only

    assets_root = settings["asset_directory"]
    for name, lib_cfg in cfg.libraries.items():
        if args.library and name not in args.library:
            continue
        library = libs.get(name)
        if library is None:
            log.error("library '%s' not found in Audiobookshelf (have: %s)", name, ", ".join(libs))
            continue
        if library.get("mediaType", "book") != "book":
            log.warning("library '%s' is not a book library, skipped", name)
            continue
        try:
            def load_books():
                bs = [Book.from_api(i) for i in api.items(library["id"])]
                return [b for b in bs if not b.missing]

            books = load_books()
            log.info("[%s] %d books", name, len(books))

            # 1. metadata: match against providers, then rule-based cleanup
            if want("metadata") and lib_cfg.metadata:
                changed = run_matching(api, books, lib_cfg.metadata.get("match"), state, dry)
                if changed:
                    books = load_books()
                if not dry:
                    state.save()
                if run_cleanup(api, books, lib_cfg.metadata.get("cleanup"), dry) and not dry:
                    books = load_books()

            # 2. folders: rename into the template layout, then let ABS rescan
            if want("folders") and lib_cfg.folders:
                if run_folders(api, library, books, lib_cfg.folders, state, dry) and not dry:
                    wait = int(lib_cfg.folders.get("scan_wait_seconds", 60))
                    log.info("waiting %ds for the library scan", wait)
                    time.sleep(wait)
                    books = load_books()

            # 3. collections
            if want("collections"):
                sync_collections(api, library, books, lib_cfg, state, settings)

            # 4. artwork
            if want("covers"):
                assets = None
                if lib_cfg.assets:
                    lib_assets = os.path.join(assets_root, name)
                    assets = AssetIndex(lib_assets if os.path.isdir(lib_assets) else assets_root)
                if lib_cfg.overlays or (assets and assets.map) or state.covers:
                    process_covers(api, library, books, lib_cfg, state, settings, cfg.config_dir, assets)
        except ABSError as e:
            log.error("[%s] %s", name, e)
        finally:
            if not dry:
                state.save()
    log.info("=== run finished in %.1fs ===", time.time() - started)


def safe_run(args):
    """Run and never raise. Returns 'ok', 'config error' or 'error'."""
    try:
        run_once(args)
        return "ok"
    except ConfigError as e:
        log.error("config error: %s", e)
        return "config error"
    except ABSError as e:
        log.error("Audiobookshelf error: %s", e)
    except Exception:  # noqa: BLE001
        log.exception("unexpected error")
    return "error"


def next_run(times):
    now = dt.datetime.now()
    candidates = []
    for t in times:
        h, m = map(int, str(t).split(":"))
        c = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if c <= now:
            c += dt.timedelta(days=1)
        candidates.append(c)
    return min(candidates)


# --------------------------------------------------------------- runner --
class Runner:
    def __init__(self, config_path):
        self.config_path = config_path
        self._lock = threading.Lock()
        self.current = None
        self.last = None
        self.next_run = None
        self._times = ["03:00"]

    @property
    def busy(self):
        return self._lock.locked()

    def start(self, label, **kw):
        """Start a run in the background. False if one is already running."""
        if not self._lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._go, args=(label, kw), daemon=True).start()
        return True

    def run_now(self, label, **kw):
        """Run in this thread, waiting for any run already in progress."""
        self._lock.acquire()
        self._go(label, kw)

    def _go(self, label, kw):
        try:
            self.current = {"label": label, "started": time.time()}
            result = safe_run(make_args(self.config_path, **kw))
            self.last = {"label": label, "started": self.current["started"],
                         "finished": time.time(), "result": result}
        finally:
            self.current = None
            self._lock.release()

    def _schedule(self):
        try:
            self._times = load(self.config_path).settings["schedule"]
        except ConfigError as e:
            log.debug("schedule: config error (%s); keeping %s", e, self._times)
        return self._times

    def schedule_forever(self):
        try:
            if load(self.config_path).settings["run_on_start"]:
                self.run_now("scheduled (start)")
        except ConfigError as e:
            log.error("config error: %s", e)
        logged = None
        while True:
            nxt = next_run(self._schedule())  # re-read every loop, so schedule edits apply
            self.next_run = nxt.timestamp()
            if nxt != logged:
                log.info("next scheduled run at %s", nxt.strftime("%Y-%m-%d %H:%M"))
                logged = nxt
            remaining = (nxt - dt.datetime.now()).total_seconds()
            if remaining > 30:
                time.sleep(30)
                continue
            time.sleep(max(0.0, remaining))
            self.run_now("scheduled")
