# shelfkeeper

Kometa-style automation for **Audiobookshelf**, run as a Docker container on a schedule:

- **Collections**: rule-based (`Recently Added`, `Quick Listens`, genre + year, and so on) and automatic (one collection per series, author, narrator, decade, genre, and so on). Collections are kept in sync, sorted, and deleted when you remove them from the config.
- **Artwork**: custom covers from an asset folder, plus Kometa-style overlays (series number, NEW, runtime, ABRIDGED banners, or your own PNG badges) drawn on the covers.
- **Metadata**: matches books that are missing data using ABS's own providers (Audible, Google and others, tried in order), then applies cleanup rules: genre splitting, mapping and dropping, author and narrator spelling fixes, initials normalization, series renames, language codes, and title junk such as `(Unabridged)`.
- **Folder correction**: renames book folders into one layout (default `Author/Series/01 - Title`). Every move is logged and can be undone.
- **Web UI** on port 8585: status, run buttons (all or chosen steps, dry run or live), a live log, a config editor that checks the config before saving, and a history of folder moves, unmatched books and managed collections.
- **Safe by default**: starts in dry-run mode. Every cover is backed up before it's changed, `--reset-covers` puts them all back, and `--undo-moves` reverses the last folder moves.

Each run goes in this order: **metadata → folders → collections → covers**. Each step only runs if its section is in the config.

## Setup

1. **Get an API key** in Audiobookshelf: **Settings → API Keys** (on older versions, **Settings → Users → your user → API Token**). The user needs update, upload and delete permissions.
2. Copy this folder to the Docker host (IP.ADDRESS) and create a `.env` file next to `docker-compose.yml`:
   ```
   ABS_TOKEN=paste-your-key-here
   WEBUI_PASSWORD=choose-a-password
   ```
3. Check `ABS_URL` in `docker-compose.yml`. It's set to `http://IP.ADDRESS:13378` (the usual ABS port mapping). If shelfkeeper runs on the same Docker network as ABS, use `http://audiobookshelf:80`.
4. Start it:
   ```bash
   docker compose up -d --build
   ```
5. Open **http://IP.ADDRESS:8585** and log in as `admin` with your `WEBUI_PASSWORD`.
6. On the **Config** tab, change the library name `Audiobooks` to your ABS library's exact name and trim the examples. Click **Validate**, then **Save**.
7. On the **Dashboard**, leave **Dry run** ticked and click **Run**. Read the log.
8. When it looks right, set `dry_run: false` in the config. Scheduled runs (03:00 by default) then make real changes. You can also untick **Dry run** for a single manual run.

## Web UI

- **Dashboard**: current status, the last run's result, the next scheduled run, and whether the config is in dry-run mode. **Run** lets you pick steps, a library and dry run or live. **Undo last folder moves** and **Restore original covers** follow the Dry run switch. The log streams live.
- **Config**: edit `config.yml` in the browser. **Validate** checks it without saving; **Save** validates, keeps the previous version as `config.yml.bak`, and applies on the next run. Ctrl+S saves.
- **History**: every folder move (from → to), books no provider matched, and the collections shelfkeeper manages.

Only one run happens at a time; a manual run started during a scheduled one is refused. The UI is protected with HTTP basic auth when `WEBUI_PASSWORD` is set. Without it, anyone on your network can use it, and a warning is logged. Don't expose port 8585 to the internet.

## Commands

```bash
docker compose run --rm shelfkeeper --run                 # run once now
docker compose run --rm shelfkeeper --run --dry-run       # preview only
docker compose run --rm shelfkeeper --run --only metadata    # or folders / collections / covers (repeatable)
docker compose run --rm shelfkeeper --run --library "Audiobooks"
docker compose run --rm shelfkeeper --reset-covers        # restore every original cover
docker compose run --rm shelfkeeper --undo-moves          # move back the last run's folder moves
docker compose logs -f shelfkeeper
```

These command-line runs are for when you don't use the web UI. While the web container is running, start runs from the UI instead, so two runs never overlap.

`--reset-covers` and `--undo-moves` also respect `dry_run: true` in the config, so set it to `false` (or check the log) before relying on them.

## Metadata

- **Matching** calls ABS's quick-match for books missing the fields in `when_missing`, trying each provider in order until one matches. With `override_details: false` it only fills empty fields. A book is matched once; unmatched books are retried after `retry_unmatched_days`. The results are stored in `state/state.json` under `matches`.
- **Cleanup** rules run on every book on every run, and only change something when a rule applies. Because the changes are written through the ABS API, ABS updates its own `metadata.json` and a rescan won't undo them.

## Folder correction

1. Mount the library into shelfkeeper with **the same host folder and the same container path** as your audiobookshelf service (see `docker-compose.yml`). If the container paths differ, set `folders.path_map`.
2. Run `--run --only folders` with `dry_run: true` and read the planned `move A -> B` lines.
3. When they look right, set `dry_run: false`.

How it works:
- Folders are **renamed on the same disk**, never copied. A renamed folder keeps its inode, and ABS uses the inode to recognise a moved book, so it keeps the same item, listening progress, bookmarks and collections. Moves across different disks are refused.
- If the target folder already exists, the book is skipped (it's never merged), and the skip is logged as a conflict.
- Single-file books lying loose in the library root are left alone.
- Empty folders left behind are removed.
- After moving, shelfkeeper triggers a library scan and waits `scan_wait_seconds` before the collection and cover steps.
- Every move is appended to `config/state/moves.jsonl`. `--undo-moves` reverses the most recent run's moves; run it again to step further back.
- Template variables are the same as for overlays, plus `{series_sequence_padded}` (`1` → `01`). A `/` in a value becomes `-`.
- The container's `user:` must be allowed to rename folders in the library.

## Custom covers (assets)

Put images in `config/assets/<Library name>/` (or straight into `config/assets/`). Files are matched by name, with or without an extension, in this order:

1. `B00ABCDEFG.jpg`: ASIN
2. `9780000000000.jpg`: ISBN
3. `<ABS item id>.jpg`
4. `Brandon Sanderson - The Way of Kings.jpg`
5. `The Way of Kings.jpg`

Overlays are drawn on top of asset covers too.

## How it behaves

- **Collection ownership:** shelfkeeper remembers which collections it created (in `config/state/state.json`). If a collection with the same name already exists, shelfkeeper *adopts* it and syncs its books, but never deletes it.
- **`sync_mode: sync`** (the default) makes the collection exactly match the filter. **`append`** only adds books and never removes them.
- **Overlays** are always drawn on the saved original, so they never stack. If you change a cover in ABS yourself, shelfkeeper treats your new cover as the original. If a book stops matching every overlay, its original cover is restored.
- ABS saves uploaded covers wherever your **Store covers with item** setting says. If that setting is on, the overlaid cover replaces `cover.jpg` in the book folder. The untouched original is kept in `config/state/originals/`.
- Edits to the schedule are picked up without a restart. Other config changes apply on the next run.

## Files

```
config/
  config.yml          your rules
  assets/             custom covers
  overlays/           your PNG badges (optional)
  state/state.json    what shelfkeeper manages
  state/originals/    backups of every cover it changed
  logs/shelfkeeper.log
```
