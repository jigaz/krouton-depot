#!/usr/bin/env python3
"""Krouton Depot: check the catalogue, fetch and checksum every image, build the site and the feed.

    python3 tools/depot.py validate          # catalogue is well formed
    python3 tools/depot.py verify [ids...]   # download each image, record size and SHA-256 in state/
    python3 tools/depot.py build [--check]   # write docs/index.html and docs/v1/manifest.json

The catalogue (catalog/depot.json) says where each image comes from. `verify` resolves that to
a concrete file (the latest GitHub release asset, SourceForge's latest file, or a fixed URL),
streams it through SHA-256, and for archives also records the disc image inside. Only verified
items reach the feed that Krouton reads, so a broken link never becomes a broken burn.

Standard library only, apart from the `7z` command for .7z archives (present on GitHub's
Ubuntu runners).
"""

import datetime
import fnmatch
import hashlib
import html
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog/depot.json"
STATE = ROOT / "state/verified.json"
DOCS = ROOT / "docs"
FEED = DOCS / "v1/manifest.json"
AGENT = "KroutonDepot/1 (+https://depot.krouton.app)"
SCHEMA_URL = "https://krouton.app/schema/depot-feed/v1.json"

CHANNEL_KEYS = {"id", "title", "about", "symbol"}
ITEM_KEYS = {"id", "channel", "kind", "title", "year", "era", "platform", "summary", "license",
             "homepage", "source", "member", "boot", "burn"}
BURN_KEYS = {"leave_open", "media", "note", "dvdrom_book_type"}
KINDS = {"disc", "files", "link"}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*$")

# Smallest disc each image fits on, in bytes of user data (2048-byte sectors).
MEDIA = [
    ("business-card", "50 MB business-card CD", 50 * 1024 * 1024),
    ("mini-cd", "8 cm mini CD (185 MB)", 94_500 * 2048),
    ("cd-74", "74-minute CD (650 MB)", 333_000 * 2048),
    ("cd-80", "80-minute CD (700 MB)", 360_000 * 2048),
    ("mini-dvd", "8 cm mini DVD (1.4 GB)", 712_880 * 2048),
    ("dvd-5", "DVD (4.7 GB)", 2_295_104 * 2048),
    ("dvd-9", "dual-layer DVD (8.5 GB)", 4_173_824 * 2048),
    ("bd-25", "Blu-ray (25 GB)", 12_219_392 * 2048),
]


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- validate

def validate(catalog):
    problems = []
    channels = {c.get("id") for c in catalog.get("channels", [])}
    for channel in catalog.get("channels", []):
        extra = set(channel) - CHANNEL_KEYS
        if extra or not CHANNEL_KEYS <= set(channel):
            problems.append(f"channel {channel.get('id')}: keys must be exactly {sorted(CHANNEL_KEYS)}")
    seen = set()
    for item in catalog.get("items", []):
        ident = item.get("id", "?")
        where = f"item {ident}"
        if not ID_PATTERN.match(ident):
            problems.append(f"{where}: id must be lower-case letters, digits, dots and hyphens")
        if ident in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(ident)
        if set(item) - ITEM_KEYS:
            problems.append(f"{where}: unknown keys {sorted(set(item) - ITEM_KEYS)}")
        for key in ("channel", "title", "year", "platform", "summary", "license", "homepage"):
            if key not in item:
                problems.append(f"{where}: missing {key}")
        if item.get("channel") not in channels:
            problems.append(f"{where}: unknown channel {item.get('channel')}")
        kind = item.get("kind", "disc")
        if kind not in KINDS:
            problems.append(f"{where}: kind must be one of {sorted(KINDS)}")
        source = item.get("source")
        if kind == "link":
            if source:
                problems.append(f"{where}: a link item has no source")
        elif not isinstance(source, dict) or len(source.keys() & {"url", "github", "sourceforge"}) != 1:
            problems.append(f"{where}: source needs exactly one of url, github or sourceforge")
        elif "github" in source and "asset" not in source:
            problems.append(f"{where}: a github source needs an asset pattern")
        if "member" in item and not (isinstance(item["member"], list) and item["member"]):
            problems.append(f"{where}: member is a non-empty list of patterns")
        if set(item.get("burn", {})) - BURN_KEYS:
            problems.append(f"{where}: unknown burn keys {sorted(set(item['burn']) - BURN_KEYS)}")
        if not str(item.get("homepage", "")).startswith(("https://", "http://")):
            problems.append(f"{where}: homepage must be a web address")
    return problems


# ---------------------------------------------------------------- resolve and fetch

def request(url, method="GET", headers=None, attempts=3):
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, method=method, headers={"User-Agent": AGENT, **(headers or {})})
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as error:
            if error.code not in (429, 502, 503, 504) or attempt == attempts:
                raise
            time.sleep(min(int(error.headers.get("Retry-After", "0") or 0) or 20 * attempt, 120))
        except urllib.error.URLError:
            if attempt == attempts:
                raise
            time.sleep(15 * attempt)


def resolve(source):
    """Return (download URL to publish, version label) for a catalogue source."""
    if "url" in source:
        return source["url"], None
    if "github" in source:
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with request(f"https://api.github.com/repos/{source['github']}/releases/latest", headers=headers) as r:
            release = json.load(r)
        for asset in release.get("assets", []):
            if fnmatch.fnmatch(asset["name"], source["asset"]):
                return asset["browser_download_url"], release.get("tag_name")
        raise LookupError(f"no asset matching {source['asset']} in {source['github']} {release.get('tag_name')}")
    project = source["sourceforge"]
    with request(f"https://sourceforge.net/projects/{project}/files/latest/download") as r:
        final = r.geturl()
    # A mirror URL (https://<mirror>.dl.sourceforge.net/project/<p>/<path>) becomes the stable
    # downloads.sourceforge.net address of the same file.
    path = urllib.parse.urlsplit(final).path
    if "/project/" not in path:
        raise LookupError(f"SourceForge sent {final}, not a file")
    return "https://downloads.sourceforge.net" + path[path.index("/project/"):], None


def fetch(url, target):
    digest = hashlib.sha256()
    size = 0
    with request(url) as response, open(target, "wb") as out:
        final = response.geturl()
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            out.write(chunk)
            size += len(chunk)
    return size, digest.hexdigest(), final


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_kind(name):
    lower = name.lower()
    for suffix, kind in ((".zip", "zip"), (".7z", "7z"), (".tar.gz", "tar"), (".tgz", "tar"),
                         (".tar.xz", "tar"), (".tar.bz2", "tar"), (".tar", "tar")):
        if lower.endswith(suffix):
            return kind
    return None


def list_members(path, kind):
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            return [i.filename for i in z.infolist() if not i.is_dir()]
    if kind == "tar":
        with tarfile.open(path) as t:
            return [m.name for m in t.getmembers() if m.isfile()]
    out = subprocess.run(["7z", "l", "-slt", "-ba", str(path)], check=True, capture_output=True, text=True).stdout
    names, current, is_dir = [], None, False
    for line in out.splitlines() + [""]:
        if line.startswith("Path = "):
            current = line[7:]
        elif line == "Folder = +":
            is_dir = True
        elif not line.strip() and current is not None:
            if not is_dir:
                names.append(current)
            current, is_dir = None, False
    return names


def extract_member(path, kind, member, workdir):
    target = workdir / "member"
    if kind == "zip":
        with zipfile.ZipFile(path) as z, z.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
    elif kind == "tar":
        with tarfile.open(path) as t, t.extractfile(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
    else:
        with open(target, "wb") as out:
            subprocess.run(["7z", "e", "-so", str(path), member], check=True, stdout=out)
    return target


def pick_member(names, patterns):
    for pattern in patterns:
        matches = sorted(n for n in names if fnmatch.fnmatch(n.lower(), pattern.lower())
                         or fnmatch.fnmatch(pathlib.PurePosixPath(n).name.lower(), pattern.lower()))
        if matches:
            return min(matches, key=len)
    return None


def sniff(path):
    """What the image looks like, from its first sectors: helps Krouton label it."""
    with open(path, "rb") as handle:
        head = handle.read(64 * 1024)
    if head[0x1C:0x20] == b"\xc2\x33\x9f\x3d":
        return "gamecube"
    if head[0x18:0x1C] == b"\x5d\x1c\x9e\xa3":
        return "wii"
    if b"SEGA SEGAKATANA" in head[:0x8000]:
        return "dreamcast"
    if len(head) >= 0x8006 and head[0x8001:0x8006] == b"CD001":
        return "iso9660"
    return None


def verify_item(item, previous):
    url, version = resolve(item["source"])
    with tempfile.TemporaryDirectory() as tmp:
        workdir = pathlib.Path(tmp)
        download = workdir / "download"
        size, digest, final = fetch(url, download)
        name = urllib.parse.unquote(pathlib.PurePosixPath(urllib.parse.urlsplit(final).path).name)
        record = {
            "url": url,
            "file": name,
            "size": size,
            "sha256": digest,
            "version": version,
            "verified": now(),
        }
        kind = archive_kind(name)
        if kind:
            names = list_members(download, kind)
            patterns = item.get("member") or ["*.iso"]
            member = pick_member(names, patterns)
            if member is None and item.get("kind", "disc") == "disc":
                raise LookupError(f"nothing in {name} matches {patterns}")
            record["archive"] = kind
            if member:
                image = extract_member(download, kind, member, workdir)
                record["image"] = {"path": member, "size": image.stat().st_size, "sha256": sha256_file(image)}
                if member.lower().endswith(".cue"):
                    record["image"]["companions"] = sorted(
                        n for n in names if n.lower().endswith(".bin")
                        and pathlib.PurePosixPath(n).parent == pathlib.PurePosixPath(member).parent)
                record["image"]["looks_like"] = sniff(image)
        elif item.get("kind", "disc") == "disc":
            record["image"] = {"path": None, "size": size, "sha256": digest, "looks_like": sniff(download)}
    if previous and previous.get("url") == url and previous.get("sha256") != record["sha256"]:
        record["changed_upstream"] = now()
    return record


def verify(catalog, only):
    state = load(STATE, {})
    failures = 0
    for item in catalog["items"]:
        if item.get("kind") == "link" or (only and item["id"] not in only):
            continue
        try:
            record = verify_item(item, state.get(item["id"], {}).get("ok"))
            state[item["id"]] = {"ok": record}
            print(f"ok   {item['id']}: {record['file']} {record['size']:,} bytes {record['sha256'][:16]}")
        except Exception as error:  # noqa: BLE001 - every failure is reported and recorded
            failures += 1
            entry = state.setdefault(item["id"], {})
            entry["problem"] = f"{type(error).__name__}: {error}"[:300]
            entry["checked"] = now()
            print(f"BAD  {item['id']}: {entry['problem']}")
            continue
        state[item["id"]].pop("problem", None)
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{failures} problem(s)")
    return failures


# ---------------------------------------------------------------- feed and site

def fits_on(size, burn):
    if burn.get("media") == "mini-dvd":
        return "mini-dvd"
    for ident, _, capacity in MEDIA:
        if size <= capacity:
            return ident
    return None


def media_label(ident):
    return next((label for i, label, _ in MEDIA if i == ident), ident)


def feed_item(item, record):
    entry = {key: item[key] for key in ("id", "channel", "title", "year", "platform", "summary",
                                        "license", "homepage") if key in item}
    entry["kind"] = item.get("kind", "disc")
    for key in ("era", "boot"):
        if key in item:
            entry[key] = item[key]
    if item.get("burn"):
        entry["burn"] = item["burn"]
    if record:
        entry["download"] = {key: record[key] for key in ("url", "file", "size", "sha256", "verified")}
        if record.get("version"):
            entry["version"] = record["version"]
        if record.get("archive"):
            entry["download"]["archive"] = record["archive"]
        image = record.get("image")
        if image:
            entry["image"] = {k: v for k, v in image.items() if v is not None}
            entry["fits_on"] = fits_on(image["size"], item.get("burn", {}))
    return entry


def build_feed(catalog, state):
    items = []
    for item in catalog["items"]:
        record = state.get(item["id"], {}).get("ok")
        if item.get("kind", "disc") != "link" and not record:
            continue  # never verified: keep it out of the app
        items.append(feed_item(item, record))
    verified = [i["download"]["verified"] for i in items if "download" in i]
    return {
        "$schema": SCHEMA_URL,
        "format_version": 1,
        "generated": max(verified) if verified else None,
        "about": catalog["about"],
        "channels": catalog["channels"],
        "items": items,
    }


def human_size(size):
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
        size /= 1000
    return str(size)


def render_item(entry):
    e = html.escape
    facts = [("Platform", e(entry["platform"])), ("Released", str(entry["year"]))]
    if entry.get("era") and entry["era"] != entry["year"]:
        facts[1] = ("Released", f"{entry['year']} (project dates from {entry['era']})")
    if entry.get("version"):
        facts.append(("Version", e(entry["version"])))
    if entry.get("fits_on"):
        facts.append(("Fits on", e(media_label(entry["fits_on"]))))
    if entry.get("image"):
        facts.append(("Image", e(human_size(entry["image"]["size"]))))
    facts.append(("Licence", e(entry["license"])))
    rows = "\n".join(f"        <dt>{k}</dt><dd>{v}</dd>" for k, v in facts)
    links = [f'<a href="{e(entry["homepage"])}">Project site</a>']
    if entry.get("download"):
        d = entry["download"]
        links.insert(0, f'<a href="{e(d["url"])}">Download {e(d["file"])}</a>')
        checksum = (f'<p class="checksum">SHA-256 of the download: <code>{d["sha256"]}</code>'
                    f'<br>Last fetched and checked {d["verified"][:10]}.</p>')
    else:
        checksum = ""
    note = ""
    if entry.get("burn", {}).get("note"):
        note = f'      <p class="burn-note">{e(entry["burn"]["note"])}</p>\n'
    kind_line = {"files": "<p class=\"kind\">Not a disc: copy these files to an SD card or USB drive.</p>",
                 "link": "<p class=\"kind\">Download it from the project's own site.</p>"}.get(entry["kind"], "")
    return f"""    <article class="item" id="{e(entry['id'])}">
      <h3>{e(entry['title'])}</h3>
      <p>{e(entry['summary'])}</p>
      {kind_line}
{note}      <dl class="facts">
{rows}
      </dl>
      <p class="links">{' · '.join(links)}</p>
      {checksum}
    </article>"""


def build_site(feed):
    e = html.escape
    sections = []
    for channel in feed["channels"]:
        items = [i for i in feed["items"] if i["channel"] == channel["id"]]
        if not items:
            continue
        body = "\n".join(render_item(i) for i in items)
        sections.append(f"""  <section id="{e(channel['id'])}">
    <h2>{e(channel['title'])}</h2>
    <p class="lede">{e(channel['about'])}</p>
{body}
  </section>""")
    nav = "\n".join(f'      <a href="#{e(c["id"])}">{e(c["title"])}</a>' for c in feed["channels"])
    count = sum(1 for i in feed["items"] if i["kind"] == "disc")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Krouton Depot: disc images worth burning</title>
<meta name="description" content="{e(feed['about'])}">
<link rel="stylesheet" href="/assets/site.css">
<link rel="stylesheet" href="/assets/depot.css">
<link rel="icon" href="/assets/disc.svg" type="image/svg+xml">
<link rel="alternate" type="application/json" href="/v1/manifest.json" title="Krouton Depot feed">
</head>
<body>
<div class="site">
  <header class="masthead">
    <a class="name" href="https://krouton.app/">Krouton</a>
    <nav aria-label="Site">
      <a href="https://krouton.app/">The app</a>
      <a href="https://krouton.app/manual/">Manual</a>
      <a href="/" aria-current="page">Depot</a>
      <a href="https://archive.krouton.app/">Archive</a>
      <a href="https://github.com/jigaz/krouton-depot">Source</a>
    </nav>
  </header>

  <main>
    <h1>Disc images worth burning</h1>
    <p class="lede">{e(feed['about'])}</p>
    <p>{count} disc images, checked {e((feed['generated'] or 'not yet')[:10])}. Krouton shows the same list in its Disc Image Depot; the feed is at <a href="/v1/manifest.json">/v1/manifest.json</a> (<a href="https://krouton.app/schema/depot-feed/v1.json">format</a>).</p>
    <nav class="channels" aria-label="Sections">
{nav}
    </nav>
{chr(10).join(sections)}
  </main>

  <footer class="colophon">
    <p>Suggest an image at <a href="https://github.com/jigaz/krouton-depot/issues">github.com/jigaz/krouton-depot</a>. It must be freely redistributable and downloadable from its own project.</p>
    <p>This site sets no cookies and loads nothing from anyone else. The downloads come from each project's own servers.</p>
  </footer>
</div>
</body>
</html>
"""


def build(catalog, check):
    state = load(STATE, {})
    feed = build_feed(catalog, state)
    outputs = {
        FEED: json.dumps(feed, indent=2, ensure_ascii=False) + "\n",
        DOCS / "index.html": build_site(feed),
    }
    stale = [p for p, text in outputs.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
    if check:
        for path in stale:
            print(f"out of date: {path.relative_to(ROOT)}")
        return 1 if stale else 0
    for path in stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(outputs[path], encoding="utf-8")
    print(f"feed: {len(feed['items'])} items; wrote {len(stale)} file(s)")
    return 0


def main(argv):
    if not argv:
        sys.exit(__doc__)
    catalog = load(CATALOG, None)
    problems = validate(catalog)
    if problems:
        print("\n".join(problems))
        sys.exit(1)
    command = argv[0]
    if command == "validate":
        print(f"catalogue ok: {len(catalog['items'])} items in {len(catalog['channels'])} channels")
    elif command == "verify":
        # Problems are recorded in state/verified.json (and keep an item out of the feed); one
        # broken upstream must not stop the others from being refreshed.
        verify(catalog, set(argv[1:]))
    elif command == "build":
        sys.exit(build(catalog, "--check" in argv))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
