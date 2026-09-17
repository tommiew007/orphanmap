"""
Render a collector graph (the dict from Graph.to_dict, or a graph.json file) to
Graphviz DOT, and optionally to SVG if the `dot` binary is on PATH.

DOT has no runtime dependency -- it is plain text anyone can render later. The SVG
step is best-effort and skipped with a note if Graphviz isn't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

# node type -> (graphviz shape, fillcolor)
STYLE = {
    "server":          ("box3d",     "#2c3e50", "#ffffff"),
    "database":        ("cylinder",  "#2980b9", "#ffffff"),
    "table":           ("box",       "#d6eaf8", "#000000"),
    "view":            ("box",       "#aed6f1", "#000000"),
    "procedure":       ("component", "#f9e79f", "#000000"),
    "scalar_function": ("component", "#fdebd0", "#000000"),
    "inline_table_function": ("component", "#fdebd0", "#000000"),
    "table_function":  ("component", "#fdebd0", "#000000"),
    "agent_job":       ("folder",    "#a9dfbf", "#000000"),
    "agent_job_step":  ("note",      "#d5f5e3", "#000000"),
    "scheduled_task":  ("folder",    "#d7bde2", "#000000"),
    "executable":      ("box",       "#e5e7e9", "#000000"),
    "file":            ("note",      "#fcf3cf", "#000000"),
    "linked_server":   ("box",       "#f5b7b1", "#000000"),
    "sql_server_ref":  ("box",       "#f5b7b1", "#000000"),
    "object":          ("box",       "#eaecee", "#000000"),
}
DEFAULT_STYLE = ("box", "#ffffff", "#000000")

# short edge labels
EDGE_LABEL = {
    "HOSTS_DATABASE": "hosts", "HAS_LINKED_SERVER": "linked",
    "CONTAINS": "contains", "DEPENDS_ON": "depends on",
    "REFERENCES_LINKED_SERVER": "-> server", "HAS_JOB": "job",
    "HAS_STEP": "step", "INVOKES": "invokes", "RUNS_ON": "runs on",
    "RUNS": "runs", "REFERENCES_SERVER": "-> server",
    "CONNECTS_TO": "connects", "REFERENCES": "reads", "REFERENCES_TABLE": "reads",
}


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def to_dot(graph: Dict[str, Any], title: str = "Dependency graph") -> str:
    lines = ["digraph collector {",
             '  graph [rankdir=LR, fontname="Segoe UI", labelloc=t, '
             f'label="{_esc(title)}", fontsize=18, bgcolor="white", nodesep=0.35, ranksep=0.8];',
             '  node [fontname="Segoe UI", fontsize=10, style="filled,rounded"];',
             '  edge [fontname="Segoe UI", fontsize=8, color="#7f8c8d"];']
    for n in graph["nodes"]:
        shape, fill, font = STYLE.get(n["type"], DEFAULT_STYLE)
        label = f'{n["name"]}\\n({n["type"]})'
        lines.append(
            f'  "{_esc(n["id"])}" [label="{_esc(label)}", shape={shape}, '
            f'fillcolor="{fill}", fontcolor="{font}"];')
    for e in graph["edges"]:
        lbl = EDGE_LABEL.get(e["type"], e["type"].lower())
        lines.append(
            f'  "{_esc(e["src"])}" -> "{_esc(e["dst"])}" [label="{_esc(lbl)}"];')
    lines.append("}")
    return "\n".join(lines)


def render(graph: Dict[str, Any], out_base: str, title: str = "Dependency graph") -> None:
    """Write <out_base>.dot always; also <out_base>.svg if `dot` is available."""
    dot_path = out_base + ".dot"
    os.makedirs(os.path.dirname(os.path.abspath(dot_path)), exist_ok=True)
    with open(dot_path, "w", encoding="utf-8") as f:
        f.write(to_dot(graph, title))
    print(f"[render] wrote {dot_path}")

    dot_exe = shutil.which("dot")
    if not dot_exe:
        print("[render] Graphviz 'dot' not on PATH - skipping SVG "
              "(install Graphviz to render an image, or use the .dot file elsewhere).")
        return
    svg_path = out_base + ".svg"
    try:
        subprocess.run([dot_exe, "-Tsvg", dot_path, "-o", svg_path],
                       check=True, capture_output=True, text=True, timeout=60)
        print(f"[render] wrote {svg_path}")
    except subprocess.CalledProcessError as e:
        print(f"[render] dot failed: {e.stderr.strip()}")


def main(argv: Optional[list] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Render a collector graph.json to DOT/SVG.")
    ap.add_argument("graph_json")
    ap.add_argument("--out-base", default=None,
                    help="Output base path (default: alongside the json).")
    ap.add_argument("--title", default="Dependency graph")
    args = ap.parse_args(argv)
    with open(args.graph_json, encoding="utf-8") as f:
        graph = json.load(f)
    base = args.out_base or os.path.splitext(args.graph_json)[0]
    render(graph, base, title=args.title)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
