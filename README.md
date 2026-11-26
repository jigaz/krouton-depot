# Krouton Depot

The catalogue behind the Disc Image Depot in [Krouton](https://krouton.app): disc images worth burning, published at **[depot.krouton.app](https://depot.krouton.app)**.

It leans towards the late 1990s and 2000s, when a CD burner was cutting-edge: the first Ubuntu, Damn Small Linux, Knoppix, FreeDOS. Alongside those are odd little systems, including a Puppy Linux that saves itself back onto the disc it booted from, fix-it discs for broken computers, and open-source console homebrew. There are no games or other commercial software.

## How it works

- `catalog/depot.json` is the hand-written list: where each image comes from, and a short description.
- `tools/depot.py verify` runs monthly in GitHub Actions. For each item it:
  - resolves the source: a fixed URL, the newest matching GitHub release asset, or SourceForge's latest file;
  - downloads the image and records its size and SHA-256 in `state/verified.json`;
  - for a zip, tar or 7z archive, also records the disc image inside it.
- `tools/depot.py build` writes the site (`docs/index.html`) and the feed Krouton reads (`docs/v1/manifest.json`). Only items that have been downloaded and checked reach the feed. The feed's format is described at [krouton.app/schema/depot-feed/v1.json](https://krouton.app/schema/depot-feed/v1.json).
- Krouton downloads from each project's own servers and refuses a file whose checksum doesn't match the feed.

The depot links rather than hosts. Each project stays in control of its own downloads, and nothing here needs gigabytes of storage.

## Adding an image

Open an issue or a pull request that adds an entry to `catalog/depot.json`. An image belongs here if:

1. it may be freely redistributed (open source, public domain, or a licence that says so);
2. its own project offers it for download at a stable address;
3. it's interesting to burn: it boots, plays, rescues, or remembers something.

Then run:

```bash
python3 tools/depot.py validate
python3 tools/depot.py verify <your-id>   # downloads it, so this takes a while
python3 tools/depot.py build
```

## Licence

The code is MIT. The catalogue text is CC0. Each image keeps its own project's licence, which is listed with it.
