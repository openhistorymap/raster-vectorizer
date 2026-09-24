#!/usr/bin/env python3
"""One-shot copyright fixer for OFM worlds.

For every <world>/timeline.json under OFM_ROOT, look up the world's slug
against a known-IP mapping and rewrite `copyright`. Also patches
`map.json#metadata.ofm.copyright` to match.

Idempotent: only writes if the target value differs from what's on disk.
Run with --dry-run to preview changes, omit it to apply.

Conventions for the copyright strings:
  - Use the canonical IP holder of record (not "the property of all the
    concepts in X" boilerplate); short, factual one-line.
  - For dual-IP worlds (e.g. Alien RPG = Free League licensing 20th
    Century Studios IP), list both.
  - For homebrew / Azgaar exports / real-world maps: attribute the tool
    or data source, not a non-existent IP holder.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

OFM_ROOT = Path(os.environ.get("OFM_ROOT", "/ofm"))

# Order matters — first regex match wins.
RULES: list[tuple[str, str]] = [
    # --- D&D family ---
    (r"^toril(_.*|-.*)?$",
        "Forgotten Realms / Toril is the property of Wizards of the Coast"),
    (r"^golarion$",
        "Golarion and the Pathfinder setting are the property of Paizo Inc."),
    (r"^krynn$",
        "Krynn / Dragonlance is the property of Wizards of the Coast"),
    (r"^barovia.*$",
        "Barovia / Ravenloft is the property of Wizards of the Coast"),
    (r"^eberron.*$",
        "Eberron is the property of Wizards of the Coast"),
    (r"^athas$",
        "Athas (Dark Sun) is the property of Wizards of the Coast"),
    (r"^flow(-realmspace.*)?$",
        "Spelljammer / Realmspace is the property of Wizards of the Coast"),
    (r"^rock_of_bral.*$",
        "Spelljammer is the property of Wizards of the Coast"),

    # --- Literary / film ---
    (r"^middle-earth$",
        "Middle-earth is the property of the Tolkien Estate"),
    (r"^planetos$",
        "Planetos (Westeros, Essos, Sothoryos) is the property of George R. R. Martin"),
    (r"^tamriel$",
        "Tamriel (The Elder Scrolls) is the property of Bethesda Softworks / ZeniMax"),
    (r"^discworld$",
        "Discworld is the property of the Terry Pratchett Estate"),
    (r"^dune(-arrakis)?$",
        "Dune is the property of Herbert Properties LLC; the Dune RPG is by Modiphius Entertainment"),
    (r"^blade_runner$",
        "Blade Runner is the property of Alcon Entertainment / Warner Bros."),
    (r"^silent_hill$",
        "Silent Hill is the property of Konami"),

    # --- Video games ---
    (r"^cyberpunk$",
        "Cyberpunk 2077 is the property of CD Projekt RED; the Cyberpunk RPG is the property of R. Talsorian Games"),
    (r"^cbrpnk-map$",
        "Cyberpunk RPG is the property of R. Talsorian Games"),
    (r"^zelda-botw$",
        "The Legend of Zelda: Breath of the Wild is the property of Nintendo"),
    (r"^valheim$|^vh_.+$",
        "Valheim is the property of Iron Gate Studio"),
    (r"^wow$",
        "World of Warcraft is the property of Blizzard Entertainment"),
    (r"^ogame$",
        "OGame is the property of Gameforge AG"),
    (r"^fallout$",
        "Fallout is the property of Bethesda Softworks / ZeniMax"),

    # --- Tabletop sci-fi ---
    (r"^doskvol$",
        "Doskvol / Blades in the Dark is the property of Evil Hat Productions and John Harper"),
    (r"^coriolis$|^coriolis-.+$",
        "Coriolis: The Third Horizon is the property of Free League Publishing"),
    (r"^alien$|^alien-.+$",
        "Alien is the property of 20th Century Studios / The Walt Disney Company; the Alien RPG is the property of Free League Publishing"),
    (r"^sta$|^sta-.+$|^sta_.+$",
        "Star Trek is the property of CBS / Paramount; Star Trek Adventures is the property of Modiphius Entertainment"),
    (r"^starfleet-.+$",
        "Star Trek is the property of CBS / Paramount"),
    (r"^fleetcommand$",
        "Star Trek is the property of CBS / Paramount"),
    (r"^sw$",
        "Star Wars is the property of Lucasfilm Ltd. / The Walt Disney Company"),
    (r"^wh4k(-terra)?$",
        "Warhammer 40,000 is the property of Games Workshop"),

    # --- Real-world / homebrew / tooling ---
    (r"^naples$",
        "Map data © OpenStreetMap contributors (ODbL)"),
    (r"^luxastra$",
        "Luxastra — original setting by the OFM project"),
    (r"^ogres$",
        "Original homebrew setting"),
    (r"^test_edway$",
        "Test world — no IP"),
    (r"^\d{13}$",
        "Azgaar's Fantasy Map Generator export (https://azgaar.github.io/Fantasy-Map-Generator/) — the world is the user's creation"),
    (r"^azgaar-importer$",
        "Azgaar's Fantasy Map Generator (https://azgaar.github.io/Fantasy-Map-Generator/)"),
]
COMPILED = [(re.compile(rx), val) for rx, val in RULES]


def lookup(slug: str) -> Optional[str]:
    for rx, val in COMPILED:
        if rx.match(slug):
            return val
    return None


def patch_json(path: Path, key_path: list[str], new_value: str, *, dry_run: bool) -> Optional[str]:
    """Update obj[key_path[0]][key_path[1]]... in `path`. Return old value or None
    if no change applied. Walks structure; if path doesn't exist for map.json's
    metadata.ofm.copyright, doesn't create it."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    cur = data
    for i, k in enumerate(key_path[:-1]):
        if not isinstance(cur, dict) or k not in cur:
            return None  # path doesn't exist; don't fabricate
        cur = cur[k]
    last = key_path[-1]
    old = cur.get(last) if isinstance(cur, dict) else None
    if old == new_value:
        return None
    if not isinstance(cur, dict):
        return None
    cur[last] = new_value
    if not dry_run:
        bak = path.with_suffix(path.suffix + ".bak")
        if not bak.exists():
            shutil.copy2(path, bak)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return old if old is not None else "(missing)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show changes, don't write")
    ap.add_argument("--only", help="only process slugs matching this regex")
    args = ap.parse_args()

    only_re = re.compile(args.only) if args.only else None
    unknown: list[str] = []
    changed: list[tuple[str, str, str, str]] = []
    skipped_no_match: list[str] = []

    for tl_path in sorted(OFM_ROOT.glob("*/timeline.json")):
        if tl_path.stat().st_size == 0:
            continue
        slug = tl_path.parent.name
        if only_re and not only_re.search(slug):
            continue
        target = lookup(slug)
        if target is None:
            unknown.append(slug)
            continue
        # 1. timeline.json
        old_tl = patch_json(tl_path, ["copyright"], target, dry_run=args.dry_run)
        # 2. map.json metadata.ofm.copyright (if it exists)
        mj = tl_path.parent / "map.json"
        old_mj = None
        if mj.is_file():
            old_mj = patch_json(mj, ["metadata", "ofm", "copyright"], target, dry_run=args.dry_run)
        if old_tl is None and old_mj is None:
            skipped_no_match.append(slug)
            continue
        changed.append((slug, old_tl or "(no-op)", target, "tl+mj" if old_mj else "tl"))

    print(f"--- {'DRY-RUN' if args.dry_run else 'APPLIED'} ---")
    print(f"changed:   {len(changed)}")
    print(f"unchanged: {len(skipped_no_match)} (slug matched a rule but value already correct)")
    print(f"unknown:   {len(unknown)} (no rule matched — left alone)")
    print()
    for slug, old, new, where in changed:
        print(f"  [{where}] {slug}")
        print(f"        - {old[:90]}")
        print(f"        + {new[:90]}")
    if unknown:
        print()
        print("Unknown slugs (add to RULES if you want them patched):")
        for s in unknown:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
