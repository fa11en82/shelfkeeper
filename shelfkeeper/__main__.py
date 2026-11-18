"""shelfkeeper - Kometa-style collections, artwork, metadata and folders for Audiobookshelf."""
import argparse
import logging
import os
import shutil
import sys
import threading
from logging.handlers import RotatingFileHandler

from . import __version__
from .runner import LOG_BUFFER, Runner, STEPS, safe_run

log = logging.getLogger("shelfkeeper")
EXAMPLE_CONFIG = "/app/config.example.yml"


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
    LOG_BUFFER.setFormatter(fmt)
    root.addHandler(LOG_BUFFER)
    for noisy in ("urllib3", "PIL", "waitress"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    p = argparse.ArgumentParser(prog="shelfkeeper", description=__doc__)
    p.add_argument("--config", default=os.environ.get("SHELFKEEPER_CONFIG", "/config/config.yml"))
    p.add_argument("--web", action="store_true", help="start the web UI together with the scheduler")
    p.add_argument("--run", action="store_true", help="run once now and exit")
    p.add_argument("--dry-run", action="store_true", default=None,
                   help="log what would change, change nothing")
    p.add_argument("--library", action="append", help="only this library (repeatable)")
    p.add_argument("--only", action="append", choices=list(STEPS),
                   help="run only this step (repeatable)")
    p.add_argument("--collections-only", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--covers-only", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--reset-covers", action="store_true",
                   help="restore every original cover shelfkeeper changed, then exit")
    p.add_argument("--undo-moves", action="store_true",
                   help="move back every folder from the last run that moved folders, then exit")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    config_dir = os.path.dirname(os.path.abspath(args.config))
    os.makedirs(config_dir, exist_ok=True)
    setup_logging(config_dir, args.debug)
    log.info("shelfkeeper %s", __version__)

    if not os.path.exists(args.config) and os.path.exists(EXAMPLE_CONFIG):
        shutil.copy(EXAMPLE_CONFIG, args.config)
        log.info("no config found - wrote the example to %s (dry_run is on)", args.config)

    if args.run or args.reset_covers or args.undo_moves:
        sys.exit(0 if safe_run(args) != "config error" else 2)

    runner = Runner(args.config)
    if not args.web:
        runner.schedule_forever()
        return

    from waitress import serve
    from .webui import create_app

    threading.Thread(target=runner.schedule_forever, daemon=True, name="scheduler").start()
    port = int(os.environ.get("WEBUI_PORT", "8585"))
    if not os.environ.get("WEBUI_PASSWORD"):
        log.warning("WEBUI_PASSWORD is not set - anyone on your network can use the web UI")
    log.info("web UI on http://0.0.0.0:%d", port)
    serve(create_app(runner, args.config), host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
