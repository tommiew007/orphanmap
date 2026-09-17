r"""
Phase 2 collector: Windows Task Scheduler -> graph nodes/edges.

READ-ONLY. Shells out to PowerShell `Get-ScheduledTask` (enumeration only; nothing
is created, changed, enabled, or run). Adds to the shared graph:

  - scheduled_task nodes
  - executable nodes (the action's Execute) + RUNS edges
  - file nodes for scripts/data referenced in the action Arguments + REFERENCES edges
  - referenced SQL server targets parsed from sqlcmd/osql/bcp -S args + REFERENCES_SERVER edges

Every edge carries a `source` citation naming the task (full TaskPath\TaskName) and
the exact action text or argument the relationship came from.

By default, Microsoft-shipped tasks (TaskPath under \Microsoft\) are skipped as noise;
pass include_microsoft=True to keep them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Dict, List, Optional

# Import the shared graph model from the Phase 1 module.
from phase1_sqlserver import Graph, write_graph


PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$tasks = Get-ScheduledTask
$out = foreach ($t in $tasks) {
    foreach ($a in $t.Actions) {
        [pscustomobject]@{
            TaskName         = $t.TaskName
            TaskPath         = $t.TaskPath
            State            = [string]$t.State
            Author           = $t.Author
            Execute          = $a.Execute
            Arguments        = $a.Arguments
            WorkingDirectory = $a.WorkingDirectory
        }
    }
}
if ($null -eq $out) { '[]' } else { $out | ConvertTo-Json -Depth 3 }
"""

# file extensions we treat as "referenced artifacts" worth a node/edge
FILE_EXT = r"ps1|bat|cmd|vbs|sql|py|exe|accdb|mdb|xlsx|xlsm|dtsx|rdl|jar|dll|config|xml|ini"
FILE_TOKEN_RE = re.compile(
    r'"([A-Za-z]:\\[^"]+?\.(?:' + FILE_EXT + r'))"'            # quoted absolute path
    r'|([A-Za-z]:\\[^\s"]+?\.(?:' + FILE_EXT + r'))'            # bare absolute path
    r'|(\b[\w.\-]+\.(?:' + FILE_EXT + r'))\b',                  # bare filename
    re.IGNORECASE)

# sqlcmd/osql/bcp server argument:  -S server   /S:server   -Sserver
SQL_SERVER_ARG_RE = re.compile(r'[-/]S[ :=]?\s*"?([A-Za-z0-9_.\\\-]+)"?', re.IGNORECASE)
SQL_CLIENT_EXES = ("sqlcmd", "osql", "bcp")


def _norm_path(p: str) -> str:
    return p.strip().strip('"').replace("/", "\\")


def _exe_basename(execute: str) -> str:
    base = os.path.basename(_norm_path(execute)).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _query_tasks() -> List[Dict[str, Any]]:
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", PS_SCRIPT],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Get-ScheduledTask failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "[]")
    if isinstance(data, dict):
        data = [data]
    return data


def run_phase2(g: Graph, *, include_microsoft: bool = False,
               actions: Optional[List[Dict[str, Any]]] = None) -> None:
    """Populate graph `g` with Phase 2 (Task Scheduler) data.

    `actions` may be injected (list of {TaskName,TaskPath,State,Author,Execute,
    Arguments,WorkingDirectory} dicts) for testing; otherwise queried live."""
    if actions is None:
        print("[phase2] querying Windows Task Scheduler (read-only)")
        actions = _query_tasks()

    kept = 0
    skipped_ms = 0
    for a in actions:
        task_path = a.get("TaskPath") or "\\"
        task_name = a.get("TaskName") or ""
        full = f"{task_path}{task_name}"

        if not include_microsoft and task_path.lower().startswith("\\microsoft\\"):
            skipped_ms += 1
            continue

        execute = a.get("Execute")
        if not execute:
            # non-exec action (COM handler, etc.) - record the task, no RUNS edge
            g.add_node(f"task:{full}", "scheduled_task", full,
                       task_path=task_path, state=a.get("State"), author=a.get("Author"))
            kept += 1
            continue

        args = a.get("Arguments") or ""
        workdir = a.get("WorkingDirectory") or ""
        task_node = g.add_node(f"task:{full}", "scheduled_task", full,
                               task_path=task_path, state=a.get("State"),
                               author=a.get("Author"), execute=execute,
                               arguments=args, working_directory=workdir)
        kept += 1

        # scheduled_task -> executable
        exe_norm = _norm_path(execute)
        exe_node = g.add_node(f"exe:{exe_norm.lower()}", "executable",
                              os.path.basename(exe_norm) or exe_norm, path=exe_norm)
        cite_text = execute if not args else f"{execute} {args}"
        g.add_edge(task_node, exe_node, "RUNS",
                   {"type": "task_scheduler", "object": full,
                    "text": cite_text[:300]})

        # scheduled_task -> referenced files in the arguments
        for m in FILE_TOKEN_RE.finditer(args):
            token = next((x for x in m.groups() if x), None)
            if not token:
                continue
            fnorm = _norm_path(token)
            # skip if it's just the exe again
            if fnorm.lower() == exe_norm.lower():
                continue
            file_node = g.add_node(f"file:{fnorm.lower()}", "file",
                                   os.path.basename(fnorm) or fnorm, path=fnorm)
            g.add_edge(task_node, file_node, "REFERENCES",
                       {"type": "task_action_arg", "object": full,
                        "text": token})

        # scheduled_task -> SQL server target (only for SQL client exes)
        if _exe_basename(execute) in SQL_CLIENT_EXES:
            for m in SQL_SERVER_ARG_RE.finditer(args):
                srv = m.group(1)
                if not srv:
                    continue
                srv_node = g.add_node(f"sqlserver:{srv.lower()}", "sql_server_ref", srv)
                g.add_edge(task_node, srv_node, "REFERENCES_SERVER",
                           {"type": "task_action_arg", "object": full,
                            "text": m.group(0)})

    print(f"         scheduled tasks kept: {kept}"
          + (f"  (skipped {skipped_ms} Microsoft task actions; "
             f"use --include-microsoft to keep)" if skipped_ms else ""))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Phase 2 read-only Task Scheduler collector.")
    ap.add_argument("--out", default="output/tasks_graph.json")
    ap.add_argument("--include-microsoft", action="store_true",
                    help="Include Microsoft-shipped tasks (\\Microsoft\\...).")
    args = ap.parse_args(argv)

    g = Graph()
    run_phase2(g, include_microsoft=args.include_microsoft)
    write_graph(g, args.out)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
