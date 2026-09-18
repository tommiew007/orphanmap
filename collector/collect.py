"""
Orchestrator: run all available collection phases into ONE JSON dependency graph.

READ-ONLY end to end. Phase 1 = SQL Server catalog + Agent. Phase 2 = Windows Task
Scheduler. (Phase 3 = file-tree scan, to come.) Every edge in the output carries a
source citation.

Usage:
  python collect.py --server localhost --out ../output/graph.json
  python collect.py --server localhost --skip-tasks         # phase 1 only
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from phase1_sqlserver import Graph, run_phase1, write_graph
from phase2_taskscheduler import run_phase2
from phase3_files import run_phase3


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only multi-phase environment collector.")
    ap.add_argument("--server", default="localhost")
    ap.add_argument("--driver", default="ODBC Driver 18 for SQL Server")
    ap.add_argument("--databases", nargs="*", default=None)
    ap.add_argument("--out", default="output/graph.json")
    ap.add_argument("--history-limit", type=int, default=20)
    ap.add_argument("--include-system", action="store_true",
                    help="Include system databases and Microsoft-shipped Agent jobs.")
    ap.add_argument("--include-microsoft", action="store_true",
                    help="Include Microsoft-shipped scheduled tasks.")
    ap.add_argument("--scan-dir", default=None,
                    help="Directory tree to scan for Phase 3 (files). Omit to skip Phase 3.")
    ap.add_argument("--max-files", type=int, default=5000)
    ap.add_argument("--skip-sql", action="store_true", help="Skip Phase 1 (SQL Server).")
    ap.add_argument("--skip-tasks", action="store_true", help="Skip Phase 2 (Task Scheduler).")
    ap.add_argument("--render", action="store_true",
                    help="Also render the graph to DOT (and SVG if Graphviz is installed).")
    args = ap.parse_args(argv)

    g = Graph()

    if not args.skip_sql:
        try:
            run_phase1(g, server=args.server, driver=args.driver, databases=args.databases,
                       history_limit=args.history_limit, include_system=args.include_system)
        except Exception as e:
            sqlstate = e.args[0] if getattr(e, "args", None) else ""
            if sqlstate in ("08001", "08S01", "HYT00", "HYT01"):
                print(f"[phase1] No SQL Server reachable at '{args.server}'. Skipping the SQL phase; "
                      f"the other phases still run. Point at a live instance with --server, "
                      f"or pass --skip-sql to skip it on purpose.")
            elif sqlstate == "IM002":
                print(f"[phase1] ODBC driver '{args.driver}' is not installed. Install the Microsoft "
                      f"ODBC Driver 18 for SQL Server (see the README), or pass --driver with a driver "
                      f"you have. Skipping the SQL phase.")
            else:
                print(f"[phase1] ERROR (continuing): {e}")

    if not args.skip_tasks:
        try:
            run_phase2(g, include_microsoft=args.include_microsoft)
        except Exception as e:
            print(f"[phase2] ERROR (continuing): {e}")

    if args.scan_dir:
        try:
            run_phase3(g, scan_dir=args.scan_dir, max_files=args.max_files)
        except Exception as e:
            print(f"[phase3] ERROR (continuing): {e}")

    write_graph(g, args.out)

    if args.render:
        from render import render
        base = args.out[:-5] if args.out.lower().endswith(".json") else args.out
        render(g.to_dict(), base, title="Orphaned-system dependency graph")
    return 0


if __name__ == "__main__":
    sys.exit(main())
