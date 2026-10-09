#!/usr/bin/env python3
"""Refresh the portfolio's GitHub, NuGet and pub.dev numbers in index.html.

Usage:
    python3 scripts/update_stats.py            # update index.html (and bump sw.js cache)
    python3 scripts/update_stats.py --dry-run  # only show what would change

Set GITHUB_TOKEN to avoid the unauthenticated GitHub API rate limit.
Forks are not counted as repositories. New public repos are added to the
githubRepos list and to the TR / EN / RO `repos:` translations; their GitHub
description is translated with Google Translate (if that fails, the page falls
back to the English description). Repos that are no longer public are removed.
When the set of packages changes, the package description sentences (number
words and package lists) are rewritten in all three languages.
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

GITHUB_USER = "berkbb"
NUGET_OWNER = "berkbaba"
PUB_PUBLISHER = "berk.babadogan.net"

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
SW = ROOT / "sw.js"
LANGS = ("tr", "en", "ro")  # order of the translation objects in index.html

NUMBER_WORDS = {
    "tr": ["sıfır", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz", "on",
           "on bir", "on iki", "on üç", "on dört", "on beş", "on altı", "on yedi", "on sekiz", "on dokuz"],
    "en": ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
           "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"],
    # Neuter forms, as used with "pachet".
    "ro": ["zero", "un", "două", "trei", "patru", "cinci", "șase", "șapte", "opt", "nouă", "zece",
           "unsprezece", "douăsprezece", "treisprezece", "paisprezece", "cincisprezece", "șaisprezece",
           "șaptesprezece", "optsprezece", "nouăsprezece"],
}

# Package description sentences: (static HTML fallback, {lang: translation}).
PACKAGE_TEMPLATES = {
    "pub": ("Verified publisher on pub.dev with {count} Dart {en_noun}: {list}.", {
        "tr": "pub.dev üzerinde doğrulanmış yayıncı berk.babadogan.net altında {count} Dart paketi bulunuyor: {list}.",
        "en": "The verified pub.dev publisher berk.babadogan.net owns {count} Dart {en_noun}: {list}.",
        "ro": "Publicatorul verificat pub.dev berk.babadogan.net are {count} {ro_noun} Dart: {list}.",
    }),
    "nuget": ("NuGet profile with {count} .NET {en_noun}: {list}.", {
        "tr": "NuGet profilinde {count} .NET paketi bulunuyor: {list}.",
        "en": "The NuGet profile includes {count} .NET {en_noun}: {list}.",
        "ro": "Profilul NuGet include {count} {ro_noun} .NET: {list}.",
    }),
}


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
        "package_names": {"pub": [p["package"] for p in pub], "nuget": [p["id"] for p in nuget]},
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
    html = html[:match.start(2)] + body + html[match.end(2):]
    return sync_repo_translations(html, repos, list(entries))


def translate(text, lang):
    """Translate English text with Google Translate's keyless endpoint; None on failure."""
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&dt=t"
           f"&tl={lang}&q={urllib.parse.quote(text)}")
    try:
        return "".join(part[0] for part in fetch_json(url)[0]).strip() or None
    except Exception as exc:  # network / format problems: keep the English fallback
        print(f"! Translation to {lang} failed ({exc}); the English description will be shown.")
        return None


def js_key(name):
    return name if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else json.dumps(name)


def sync_repo_translations(html, repos, listed):
    """Keep the TR / EN / RO `repos:` objects in line with the githubRepos list."""
    blocks = list(re.finditer(r"(\n        repos: \{\n)(.*?)(\n        \},)", html, re.S))
    if len(blocks) != len(LANGS):
        sys.exit(f"Expected {len(LANGS)} `repos:` translation blocks, found {len(blocks)}")

    for lang, block in reversed(list(zip(LANGS, blocks))):  # reversed: keep offsets valid
        entries = {}
        for line in block.group(2).split("\n"):
            key = re.match(r'\s*("[^"]+"|[^:\s]+):', line)
            if key:
                entries[key.group(1).strip('"')] = line.rstrip(",")
        indent = re.match(r"\s*", next(iter(entries.values()))).group(0)

        for name in sorted(set(listed) - set(entries)):
            desc = (repos.get(name) or {}).get("description")
            if not desc:
                continue
            text = desc if lang == "en" else translate(desc, lang)
            if text:
                entries[name] = f"{indent}{js_key(name)}: {json.dumps(text, ensure_ascii=False)}"
        for name in set(entries) - set(listed):
            del entries[name]

        body = ",\n".join(entries[name] for name in sorted(entries, key=str.lower))
        html = html[:block.start(2)] + body + html[block.end(2):]
    return html


def join_list(items, lang):
    if len(items) < 2:
        return "".join(items)
    last = {"tr": " ve ", "en": ", and " if len(items) > 2 else " and ", "ro": " și "}[lang]
    return ", ".join(items[:-1]) + last + items[-1]


def package_sentence(template, names, lang):
    n = len(names)
    words = NUMBER_WORDS[lang]
    count = words[n] if n < len(words) else str(n)
    ro_noun = "pachet" if n == 1 else ("de pachete" if n >= 20 else "pachete")
    return template.format(count=count, list=join_list(names, lang),
                           en_noun="package" if n == 1 else "packages", ro_noun=ro_noun)


def sync_package_descriptions(html, package_names, changes):
    """Rewrite the package sentences when the set of packages changes (keeps the existing order)."""
    for name, (static_template, templates) in PACKAGE_TEMPLATES.items():
        static = re.search(rf'(data-i18n="packages\.{name}\.desc">\s*)(.*?)(\s*</p>)', html, re.S)
        if not static:
            sys.exit(f"Could not find the static packages.{name}.desc paragraph")
        current = [n.strip() for n in re.split(r",\s*(?:and\s+)?|\s+and\s+",
                                                static.group(2).split(":", 1)[1].rstrip("."))]
        actual = package_names[name]
        if set(current) == set(actual):
            continue
        names = [n for n in current if n in actual] + [n for n in actual if n not in current]
        changes.append(f"{name} packages: {', '.join(names)}")

        html = (html[:static.start(2)] + package_sentence(static_template, names, "en")
                + html[static.end(2):])
        for lang, block in reversed(list(zip(LANGS, re.finditer(
                rf"\n          {name}: \{{.*?\n          \}},", html, re.S)))):
            new_block = re.sub(r'(\n            desc: )"[^"]*"',
                               lambda m: m.group(1) + json.dumps(
                                   package_sentence(templates[lang], names, lang), ensure_ascii=False),
                               block.group(0), count=1)
            html = html[:block.start()] + new_block + html[block.end():]
    return html


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
    html = sync_package_descriptions(html, stats["package_names"], changes)

    if html == original and not changes:
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
