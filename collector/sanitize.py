"""
Scrub environment-specific identifiers out of a graph.json so it is safe to publish
(README screenshot, example output). You supply the strings to replace on the command
line (machine name, path prefix, username, real server names, ...) as OLD=>NEW pairs;
the tool replaces them case-insensitively everywhere (node ids, names, citations) and
re-renders DOT. No identifiers are baked into this source file.

  python sanitize.py graph.json --out clean.json --render \
      --replace "D:\\Users\\me\\share=>\\\\FILESERVER\\Legacy" \
      --replace "MYBOX=>SQLHOST01" --replace "myuser=>olddev"

It is a convenience, not a guarantee -- review the output before publishing.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, List, Tuple


def build_replacements(pairs: List[str]) -> List[Tuple[re.Pattern, str]]:
    out = []
    for p in pairs:
        if "=>" not in p:
            raise SystemExit(f"--replace must be OLD=>NEW, got: {p!r}")
        old, new = p.split("=>", 1)
        out.append((re.compile(re.escape(old), re.I), new))
    return out


def scrub(s: str, replacements: List[Tuple[re.Pattern, str]]) -> str:
    for pat, repl in replacements:
        s = pat.sub(lambda _m, r=repl: r, s)  # literal replacement (no template parsing)
    return s


def walk(o: Any, repl: List[Tuple[re.Pattern, str]]) -> Any:
    if isinstance(o, str):
        return scrub(o, repl)
    if isinstance(o, list):
        return [walk(x, repl) for x in o]
    if isinstance(o, dict):
        return {scrub(k, repl) if isinstance(k, str) else k: walk(v, repl)
                for k, v in o.items()}
    return o


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Sanitize a graph.json for publishing.")
    ap.add_argument("graph_json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--replace", action="append", default=[], metavar="OLD=>NEW",
                    help="A string to scrub, repeatable. Case-insensitive.")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args(argv)

    replacements = build_replacements(args.replace)
    if not replacements:
        print("[sanitize] no --replace pairs given; output will be an unchanged copy")

    with open(args.graph_json, encoding="utf-8") as f:
        graph = json.load(f)
    clean = walk(graph, replacements)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
    print(f"[sanitize] wrote {args.out}")

    # sanity check: warn if any supplied OLD token survived
    text = json.dumps(clean)
    for p in args.replace:
        old = p.split("=>", 1)[0]
        if old and re.search(re.escape(old), text, re.I):
            print(f"[sanitize] WARNING: '{old}' still present -- review manually")

    if args.render:
        from render import render
        import os
        base = args.out[:-5] if args.out.lower().endswith(".json") else args.out
        render(clean, base, title="Orphaned-system dependency graph (example)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
