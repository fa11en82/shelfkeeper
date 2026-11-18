"""shelfkeeper - Kometa-style collections and cover artwork for Audiobookshelf."""
import argparse
import datetime as dt
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from . import __version__
from .api import ABS, ABSError
from .artwork import AssetIndex, process_covers, reset_covers
from .collections import sync_collections
from .config import load
from .items import Book, ConfigError
from .state import State

log = logging.getLogger("shelfkeeper")


def setup_logging(config_dir, debug):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    log_dir = os.path.join(config_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = RotatingFileHandler(os.path.join(log_dir, "shelfkeeper.log"), maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


def run_once(args):
    cfg = load(args.config)
    settings = cfg.settings
    if args.dry_run:
        settings["dry_run"] = True
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
            books = [Book.from_api(i) for i in api.items(library["id"])]
            books = [b for b in books if not b.missing]
            log.info("[%s] %d books", name, len(books))

            if not args.covers_only:
                sync_collections(api, library, books, lib_cfg, state, settings)

            if not args.collections_only:
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


def main():
    p = argparse.ArgumentParser(prog="shelfkeeper", description=__doc__)
    p.add_argument("--config", default=os.environ.get("SHELFKEEPER_CONFIG", "/config/config.yml"))
    p.add_argument("--run", action="store_true", help="run once now and exit")
    p.add_argument("--dry-run", action="store_true", help="log what would change, change nothing")
    p.add_argument("--library", action="append", help="only this library (repeatable)")
    p.add_argument("--collections-only", action="store_true")
    p.add_argument("--covers-only", action="store_true")
    p.add_argument("--reset-covers", action="store_true",
                   help="restore every original cover shelfkeeper changed, then exit")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    setup_logging(os.path.dirname(os.path.abspath(args.config)), args.debug)
    log.info("shelfkeeper %s", __version__)

    def safe_run():
        try:
            run_once(args)
        except ConfigError as e:
            log.error("config error: %s", e)
            return False
        except ABSError as e:
            log.error("Audiobookshelf error: %s", e)
        except Exception:
            log.exception("unexpected error")
        return True

    if args.run or args.reset_covers:
        sys.exit(0 if safe_run() else 2)

    try:
        cfg = load(args.config)
    except ConfigError as e:
        log.error("config error: %s", e)
        sys.exit(2)
    if cfg.settings["run_on_start"]:
        safe_run()
    while True:
        try:
            times = load(args.config).settings["schedule"]  # pick up schedule edits
        except ConfigError as e:
            log.error("config error: %s (keeping previous schedule)", e)
            times = cfg.settings["schedule"]
        nxt = next_run(times)
        log.info("next run at %s", nxt.strftime("%Y-%m-%d %H:%M"))
        while (remaining := (nxt - dt.datetime.now()).total_seconds()) > 0:
            time.sleep(min(remaining, 60))
        safe_run()


if __name__ == "__main__":
    main()
