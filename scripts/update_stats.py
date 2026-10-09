#!/usr/bin/env python3
"""Refresh the portfolio's GitHub, NuGet and pub.dev numbers in index.html.

Usage:
    python3 scripts/update_stats.py            # update index.html (and bump sw.js cache)
    python3 scripts/update_stats.py --dry-run  # only show what would change

Set GITHUB_TOKEN to avoid the unauthenticated GitHub API rate limit.
Forks are not counted as repositories. New public repos are added to the
githubRepos list with their GitHub description and language (the page shows that
description until a translation is added to the `repos:` blocks); repos that are
no longer public are removed from the list.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

GITHUB_USER = "berkbb"
NUGET_OWNER = "berkbaba"
PUB_PUBLISHER = "berk.babadogan.net"

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
SW = ROOT / "sw.js"


def fetch_json(url):
    headers = {"User-Agent": f"{GITHUB_USER}-portfolio-stats", "Accept": "application/json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
        return json.load(resp)


def fetch_github_list(path):
    items, page = [], 1
    while True:
        batch = fetch_json(f"https://api.github.com/{path}?per_page=100&page={page}")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def collect_stats():
    user = fetch_json(f"https://api.github.com/users/{GITHUB_USER}")
    repos = [r for r in fetch_github_list(f"users/{GITHUB_USER}/repos") if not r["fork"]]
    starred = fetch_github_list(f"users/{GITHUB_USER}/starred")

    nuget = fetch_json(
        f"https://azuresearch-usnc.nuget.org/query?q=owner:{NUGET_OWNER}&take=100&prerelease=true"
    )["data"]
    pub = fetch_json(f"https://pub.dev/api/search?q=publisher:{PUB_PUBLISHER}")["packages"]

    return {
        "repos": {r["name"]: r for r in repos},
        "github.stat1": str(len(repos)),
        "github.stat2": str(len(starred)),
        "github.stat3": str(user["followers"]),
        "github.stat4": str(user["following"]),
        "packages.pub": str(len(pub)),
        "packages.nuget": str(len(nuget)),
        "nuget.downloads": f"{sum(p['totalDownloads'] for p in nuget):,}",
    }


def current_value(html, key):
    match = re.search(rf'data-i18n="{re.escape(key)}">([^<]*)<', html)
    if not match:
        sys.exit(f"Could not find data-i18n=\"{key}\" in index.html")
    return match.group(1).strip()


def set_value(html, key, new):
    """Update both the static fallback and every translation's `value: "..."`."""
    section, name = key.split(".")
    html = re.sub(rf'(data-i18n="{key}.value">)[^<]*(<)', rf"\g<1>{new}\g<2>", html)
    # Translation objects: `<section>: { ... <name>: {\n value: "X"` (TR, EN, RO).
    pattern = rf'({section}: \{{(?:(?!\n        \}},).)*?\n\s*{name}: \{{\n\s*value: )"[^"]*"'
    html, count = re.subn(pattern, rf'\g<1>"{new}"', html, flags=re.S)
    if count != 3:
        sys.exit(f"Expected 3 translations for {key}, found {count}")
    return html


def replace_in_regions(html, name, old, new):
    """Replace a bare number inside one package platform's static card and translations."""
    regions = [
        rf'data-i18n="packages\.{name}\.summaryTitle".*?</article>',  # static summary card
        rf"\n          {name}: \{{.*?\n          \}},",  # TR / EN / RO translation objects
    ]
    number = re.compile(rf"(?<![\d,]){re.escape(old)}(?![\d,])")
    for region in regions:
        html = re.sub(region, lambda m: number.sub(new, m.group(0)), html, flags=re.S)
    return html


def js_string(text):
    return "'" + (text or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


def sync_repo_list(html, repos, changes):
    """Add new public repos to / drop removed ones from the githubRepos array."""
    match = re.search(r"(const githubRepos = \[\n)(.*?)(\n\s*\];)", html, re.S)
    if not match:
        sys.exit("Could not find the githubRepos array in index.html")
    entries = {re.search(r"name: '([^']+)'", line).group(1): line
               for line in match.group(2).split("\n") if "name: '" in line}
    indent = re.match(r"\s*", next(iter(entries.values()))).group(0)

    for name in sorted(set(repos) - set(entries)):
        repo = repos[name]
        entries[name] = (f"{indent}{{ name: {js_string(name)}, desc: {js_string(repo['description'])}, "
                         f"lang: {js_string(repo['language'])}, url: {js_string(repo['html_url'])} }}")
        changes.append(f"repo added: {name}")
    for name in sorted(set(entries) - set(repos)):
        del entries[name]
        changes.append(f"repo removed: {name}")

    lines = [entries[name].rstrip(",") for name in sorted(entries, key=str.lower)]
    body = ",\n".join(lines)
    return html[:match.start(2)] + body + html[match.end(2):]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing files")
    args = parser.parse_args()

    stats = collect_stats()
    html = original = INDEX.read_text(encoding="utf-8")
    changes = []

    for key in ("github.stat1", "github.stat2", "github.stat3", "github.stat4",
                "packages.pub", "packages.nuget"):
        old, new = current_value(html, f"{key}.value"), stats[key]
        if old != new:
            changes.append(f"{key}: {old} -> {new}")
            if key.startswith("packages."):
                name = key.split(".")[1]
                html = re.sub(rf'(data-i18n="{key}.value">)[^<]*(<)', rf"\g<1>{new}\g<2>", html)
                html = replace_in_regions(html, name, old, new)
            else:
                html = set_value(html, key, new)

    old_dl, new_dl = current_value(html, "packages.nuget.downloads"), stats["nuget.downloads"]
    if old_dl != new_dl:
        changes.append(f"nuget downloads: {old_dl} -> {new_dl}")
        html = html.replace(old_dl, new_dl)

    html = sync_repo_list(html, stats["repos"], changes)
    if any(k.startswith("packages.") for k in (c.split(":")[0] for c in changes)):
        print("! Package count changed: also check the package names/number words in the descriptions.")

    if not changes:
        print("Everything is up to date.")
        return
    print("\n".join(changes))
    if args.dry_run:
        print("(dry run, nothing written)")
        return

    INDEX.write_text(html, encoding="utf-8")
    sw = SW.read_text(encoding="utf-8")
    sw = re.sub(r"(cache-v)(\d+)", lambda m: f"{m.group(1)}{int(m.group(2)) + 1}", sw, count=1)
    SW.write_text(sw, encoding="utf-8")
    print("index.html updated, sw.js cache version bumped.")


if __name__ == "__main__":
    main()
