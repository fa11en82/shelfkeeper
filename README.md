# shelfkeeper

Kometa-style automation for **Audiobookshelf**, run as a Docker container on a schedule:

- **Collections**: rule-based (`Recently Added`, `Quick Listens`, genre + year, and so on) and automatic (one collection per series, author, narrator, decade, genre, and so on). Collections are kept in sync, sorted, and deleted when you remove them from the config.
- **Artwork**: custom covers from an asset folder, plus Kometa-style overlays (series number, NEW, runtime, ABRIDGED banners, or your own PNG badges) drawn on the covers.
- **Safe by default**: starts in dry-run mode. Every cover is backed up before it's changed, and `--reset-covers` puts them all back.

## Setup

1. **Get an API key** in Audiobookshelf: **Settings → API Keys** (on older versions, **Settings → Users → your user → API Token**). The user needs update, upload and delete permissions.
2. Copy this folder to the Docker host (IP.ADDRESS) and create a `.env` file next to `docker-compose.yml`:
   ```
   ABS_TOKEN=paste-your-key-here
   ```
3. Check `ABS_URL` in `docker-compose.yml`. It's set to `http://IP.ADDRESS:13378` (the usual ABS port mapping). If shelfkeeper runs on the same Docker network as ABS, use `http://audiobookshelf:80`.
4. In `config/config.yml`, change the library name `Audiobooks` to your ABS library's exact name, then trim the examples.
5. Build the image and run it once:
   ```bash
   docker compose build
   docker compose run --rm shelfkeeper --run
   ```
   `dry_run: true` is set in the config, so this only **logs** what it would do. Read the output.
6. When it looks right, set `dry_run: false` and start it on its schedule:
   ```bash
   docker compose up -d
   ```

## Commands

```bash
docker compose run --rm shelfkeeper --run                 # run once now
docker compose run --rm shelfkeeper --run --dry-run       # preview only
docker compose run --rm shelfkeeper --run --collections-only
docker compose run --rm shelfkeeper --run --covers-only
docker compose run --rm shelfkeeper --run --library "Audiobooks"
docker compose run --rm shelfkeeper --reset-covers        # restore every original cover
docker compose logs -f shelfkeeper
```

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
