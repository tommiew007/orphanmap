"""
Phase 1 collector: SQL Server catalog + SQL Agent -> JSON dependency graph.

READ-ONLY. Every statement issued is a SELECT against system catalog views
(sys.*, msdb.dbo.sysjobs*). Nothing is created, altered, or dropped on the target.

It emits a single JSON graph of nodes and edges. EVERY edge carries a `source`
citation describing exactly where the relationship was observed: a catalog view
plus key, a line within an object's module definition, or an Agent job step.

Scope (Phase 1):
  - sys.databases                        -> database nodes
  - sys.objects / sys.schemas            -> table/view/procedure/function nodes + CONTAINS edges
  - sys.sql_modules                      -> module text (for line-cited scans)
  - sys.sql_expression_dependencies      -> DEPENDS_ON edges (incl. cross-server)
  - sys.servers (is_linked=1)            -> linked_server nodes + HAS_LINKED_SERVER edges
  - module-text scan for linked servers  -> REFERENCES_LINKED_SERVER edges (catches dynamic SQL)
  - msdb jobs / steps / recent history   -> agent_job / agent_job_step nodes, HAS_STEP edges,
                                            step->object INVOKES edges, step->database RUNS_ON edges

Usage:
  python phase1_sqlserver.py --server localhost --out ../output/graph.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pyodbc


# ----------------------------------------------------------------------------
# Graph model
# ----------------------------------------------------------------------------

@dataclass
class Node:
    id: str
    type: str
    name: str
    attrs: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    src: str
    dst: str
    type: str
    source: Dict[str, Any]  # citation: where this edge was observed


# ----------------------------------------------------------------------------
# Credential redaction. We never store a password in the graph. Applied centrally
# to every citation text and every sensitive node attribute, so all three phases
# are covered. A node whose text carried a secret is flagged had_credential=true
# so you still know a credential exists there without recording its value.
# ----------------------------------------------------------------------------

# Password=... / PWD=... inside a connection string (value up to ; " ' or newline)
_PWD_RE = re.compile(r'(?i)\b(pwd|password|passwd)(\s*=\s*)([^;"\'\r\n]*)')
# sqlcmd/osql password flag: -P value or -Pvalue (case-sensitive uppercase P)
_PFLAG_RE = re.compile(r'(?<![\w])-P\s*("[^"]*"|\'[^\']*\'|\S+)')
_REDACTED = "***REDACTED***"
SENSITIVE_ATTR_KEYS = {"command", "arguments", "execute", "working_directory", "definition"}


def mask_secrets(text: Optional[str]):
    """Return (masked_text, had_credential). Idempotent; leaves already-redacted text."""
    if not text:
        return text, False
    hit = False

    def _pwd(m):
        nonlocal hit
        if m.group(3).strip() and _REDACTED not in m.group(3):
            hit = True
            return f"{m.group(1)}{m.group(2)}{_REDACTED}"
        return m.group(0)

    def _pflag(m):
        nonlocal hit
        if _REDACTED in m.group(1):
            return m.group(0)
        hit = True
        return f"-P {_REDACTED}"

    out = _PWD_RE.sub(_pwd, text)
    out = _PFLAG_RE.sub(_pflag, out)
    return out, hit


class Graph:
    def __init__(self) -> None:
        self._nodes: Dict[str, Node] = {}
        self._edges: List[Edge] = []
        self._edge_keys: set = set()
        self._cred_srcs: set = set()  # node ids whose citation text carried a secret

    def add_node(self, node_id: str, ntype: str, name: str, **attrs: Any) -> str:
        # redact secrets out of sensitive string attributes before storing
        cred = False
        for k in list(attrs):
            if k in SENSITIVE_ATTR_KEYS and isinstance(attrs[k], str):
                attrs[k], h = mask_secrets(attrs[k])
                cred = cred or h
        if cred:
            attrs["had_credential"] = True
        if node_id in self._nodes:
            # merge attrs (later info wins, but don't clobber with None)
            existing = self._nodes[node_id]
            for k, v in attrs.items():
                if v is not None:
                    existing.attrs[k] = v
        else:
            self._nodes[node_id] = Node(id=node_id, type=ntype, name=name, attrs=dict(attrs))
        return node_id

    def add_edge(self, src: str, dst: str, etype: str, source: Dict[str, Any]) -> None:
        # redact secrets out of the citation text
        if isinstance(source, dict) and isinstance(source.get("text"), str):
            masked, hit = mask_secrets(source["text"])
            if hit:
                source = dict(source)
                source["text"] = masked
                self._cred_srcs.add(src)
        # de-dupe identical edges that carry the same citation
        key = (src, dst, etype, json.dumps(source, sort_keys=True, default=str))
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self._edges.append(Edge(src=src, dst=dst, type=etype, source=source))

    def to_dict(self) -> Dict[str, Any]:
        for nid in self._cred_srcs:
            if nid in self._nodes:
                self._nodes[nid].attrs["had_credential"] = True
        return {
            "nodes": [asdict(n) for n in self._nodes.values()],
            "edges": [asdict(e) for e in self._edges],
        }


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def connect(server: str, driver: str, database: str = "master") -> pyodbc.Connection:
    conn_str = (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        "Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes;"
    )
    cnx = pyodbc.connect(conn_str, timeout=10, readonly=True)
    return cnx


def rows(cnx: pyodbc.Connection, sql: str, *params: Any) -> List[pyodbc.Row]:
    cur = cnx.cursor()
    cur.execute(sql, *params)
    return cur.fetchall()


def line_of(text: str, needle: str) -> Optional[Tuple[int, str]]:
    """Return (1-based line number, stripped line) of first line containing needle
    (case-insensitive), or None."""
    low_needle = needle.lower()
    for i, ln in enumerate(text.splitlines(), start=1):
        if low_needle in ln.lower():
            return i, ln.strip()
    return None


OBJ_TYPE_MAP = {
    "U": "table",
    "V": "view",
    "P": "procedure",
    "FN": "scalar_function",
    "IF": "inline_table_function",
    "TF": "table_function",
}

MODULE_TYPES = {"V", "P", "FN", "IF", "TF"}


def obj_id(db: str, schema: str, name: str) -> str:
    return f"obj:{db}.{schema}.{name}"


# ----------------------------------------------------------------------------
# Collectors
# ----------------------------------------------------------------------------

def collect_databases(cnx: pyodbc.Connection, g: Graph, server_id: str,
                      include_system: bool = False) -> List[str]:
    user_dbs: List[str] = []
    skipped: List[str] = []
    for r in rows(cnx,
                  "SELECT database_id, name, state_desc, recovery_model_desc, create_date "
                  "FROM sys.databases ORDER BY name"):
        is_system = r.name in ("master", "tempdb", "model", "msdb")
        if is_system and not include_system:
            skipped.append(r.name)
            continue
        db_node = g.add_node(f"db:{r.name}", "database", r.name,
                             database_id=r.database_id, state=r.state_desc,
                             recovery_model=r.recovery_model_desc,
                             create_date=r.create_date, is_system=is_system)
        g.add_edge(server_id, db_node, "HOSTS_DATABASE",
                   {"type": "catalog", "object": "sys.databases",
                    "key": f"database_id={r.database_id}"})
        if not is_system:
            user_dbs.append(r.name)
    if skipped:
        print(f"[filter] skipped {len(skipped)} system database(s): {skipped} "
              f"(use --include-system to keep)")
    return user_dbs


def collect_linked_servers(cnx: pyodbc.Connection, g: Graph, server_id: str) -> Dict[str, str]:
    """Return {linked_server_name_lower: node_id} for later text matching."""
    linked: Dict[str, str] = {}
    for r in rows(cnx,
                  "SELECT name, product, provider, data_source, is_linked "
                  "FROM sys.servers WHERE is_linked = 1 ORDER BY name"):
        node = g.add_node(f"lnk:{r.name}", "linked_server", r.name,
                          product=r.product, provider=r.provider, data_source=r.data_source)
        g.add_edge(server_id, node, "HAS_LINKED_SERVER",
                   {"type": "catalog", "object": "sys.servers",
                    "key": f"name={r.name!r}"})
        linked[r.name.lower()] = node
    return linked


def collect_db_objects(cnx: pyodbc.Connection, g: Graph, db: str) -> Dict[int, Tuple[str, str, str]]:
    """Add object nodes + CONTAINS edges. Return {object_id: (schema, name, type)}."""
    db_node = f"db:{db}"
    objmap: Dict[int, Tuple[str, str, str]] = {}
    q = f"""
        SELECT o.object_id, s.name AS schema_name, o.name, o.type, o.create_date, o.modify_date
        FROM [{db}].sys.objects o
        JOIN [{db}].sys.schemas s ON s.schema_id = o.schema_id
        WHERE o.is_ms_shipped = 0 AND o.type IN ('U','V','P','FN','IF','TF')
        ORDER BY s.name, o.name
    """
    for r in rows(cnx, q):
        otype = OBJ_TYPE_MAP.get(r.type.strip(), r.type.strip())
        nid = obj_id(db, r.schema_name, r.name)
        g.add_node(nid, otype, f"{r.schema_name}.{r.name}",
                   database=db, sql_type=r.type.strip(),
                   create_date=r.create_date, modify_date=r.modify_date)
        g.add_edge(db_node, nid, "CONTAINS",
                   {"type": "catalog", "object": f"[{db}].sys.objects",
                    "key": f"object_id={r.object_id}"})
        objmap[r.object_id] = (r.schema_name, r.name, r.type.strip())
    return objmap


def collect_modules(cnx: pyodbc.Connection, db: str,
                    objmap: Dict[int, Tuple[str, str, str]]) -> Dict[int, str]:
    """Return {object_id: module_definition_text} for modular objects."""
    mods: Dict[int, str] = {}
    q = f"""
        SELECT m.object_id, m.definition
        FROM [{db}].sys.sql_modules m
        JOIN [{db}].sys.objects o ON o.object_id = m.object_id
        WHERE o.is_ms_shipped = 0
    """
    for r in rows(cnx, q):
        if r.object_id in objmap and r.definition:
            mods[r.object_id] = r.definition
    return mods


def collect_dependencies(cnx: pyodbc.Connection, g: Graph, db: str,
                         objmap: Dict[int, Tuple[str, str, str]]) -> None:
    """DEPENDS_ON edges from sys.sql_expression_dependencies (intra-db + cross-server)."""
    q = f"""
        SELECT
            d.referencing_id,
            d.referenced_server_name,
            d.referenced_database_name,
            d.referenced_schema_name,
            d.referenced_entity_name,
            d.referenced_id
        FROM [{db}].sys.sql_expression_dependencies d
        WHERE d.referencing_id IS NOT NULL
    """
    for r in rows(cnx, q):
        if r.referencing_id not in objmap:
            continue
        rs, rn, rt = objmap[r.referencing_id]
        src = obj_id(db, rs, rn)

        if r.referenced_server_name:
            # cross-server reference -> linked server node
            dst = g.add_node(f"lnk:{r.referenced_server_name}", "linked_server",
                             r.referenced_server_name)
            g.add_edge(src, dst, "REFERENCES_LINKED_SERVER",
                       {"type": "catalog", "object": f"[{db}].sys.sql_expression_dependencies",
                        "key": f"referencing_id={r.referencing_id}",
                        "detail": f"{r.referenced_server_name}."
                                  f"{r.referenced_database_name or ''}."
                                  f"{r.referenced_schema_name or ''}."
                                  f"{r.referenced_entity_name or ''}"})
            continue

        if r.referenced_entity_name:
            sch = r.referenced_schema_name or "dbo"
            tdb = r.referenced_database_name or db
            dst = obj_id(tdb, sch, r.referenced_entity_name)
            # node may not exist yet (created lazily); label it
            g.add_node(dst, "object", f"{sch}.{r.referenced_entity_name}", database=tdb)
            g.add_edge(src, dst, "DEPENDS_ON",
                       {"type": "catalog", "object": f"[{db}].sys.sql_expression_dependencies",
                        "key": f"referencing_id={r.referencing_id}",
                        "detail": f"{tdb}.{sch}.{r.referenced_entity_name}"})


def scan_modules_for_linked_servers(g: Graph, db: str,
                                    objmap: Dict[int, Tuple[str, str, str]],
                                    mods: Dict[int, str],
                                    linked: Dict[str, str]) -> None:
    """Catch linked-server references that live in module TEXT (e.g. dynamic SQL),
    which sys.sql_expression_dependencies does not record. Cite the exact line."""
    if not linked:
        return
    for object_id, text in mods.items():
        sch, name, _ = objmap[object_id]
        src = obj_id(db, sch, name)
        for ls_lower, ls_node in linked.items():
            # look for the bracketed or bare linked-server name as a 4-part prefix
            hit = line_of(text, f"[{ls_lower}]") or line_of(text, ls_lower)
            if hit:
                lineno, linetext = hit
                g.add_edge(src, ls_node, "REFERENCES_LINKED_SERVER",
                           {"type": "module_line",
                            "object": f"{db}.{sch}.{name}",
                            "line": lineno,
                            "text": linetext[:200]})


EXEC_RE = re.compile(r"\bEXEC(?:UTE)?\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?\.\[?(\w+)\]?", re.IGNORECASE)
EXEC_SIMPLE_RE = re.compile(r"\bEXEC(?:UTE)?\s+\[?(\w+)\]?\.\[?(\w+)\]?", re.IGNORECASE)
FOURPART_RE = re.compile(r"\[([A-Za-z0-9_\-]+)\]\.\[?\w+\]?\.\[?\w+\]?\.\[?\w+\]?")
# file references inside a non-TSQL job step command (CmdExec/PowerShell/SSIS)
STEP_FILE_RE = re.compile(
    r'"([A-Za-z]:\\[^"]+?\.(?:bat|cmd|ps1|vbs|exe|sql|dtsx|py))"'
    r'|([A-Za-z]:\\[^\s"]+?\.(?:bat|cmd|ps1|vbs|exe|sql|dtsx|py))', re.IGNORECASE)


# Well-known Microsoft-shipped Agent jobs (msdb has no is_ms_shipped flag for jobs).
SYSTEM_JOB_RE = re.compile(
    r"^(syspolicy_purge_history|SSIS Server Maintenance Job|sysutility_.*|mdw_.*|"
    r"collection_set_.*|sysmail.*|syspolicy_.*)$", re.IGNORECASE)


def collect_agent_jobs(cnx: pyodbc.Connection, g: Graph, server_id: str,
                       linked: Dict[str, str], history_limit: int = 20,
                       include_system: bool = False) -> None:
    """Jobs, steps, HAS_STEP edges, step->object INVOKES, step->database RUNS_ON,
    and recent job history attached to the job node."""
    jobs = rows(cnx, """
        SELECT j.job_id, j.name, j.enabled, j.description, j.date_created
        FROM msdb.dbo.sysjobs j ORDER BY j.name
    """)
    filtered: List[str] = []
    for j in jobs:
        if SYSTEM_JOB_RE.match(j.name) and not include_system:
            filtered.append(j.name)
            continue
        job_node = g.add_node(f"job:{j.name}", "agent_job", j.name,
                              enabled=bool(j.enabled), description=j.description,
                              date_created=j.date_created)
        g.add_edge(server_id, job_node, "HAS_JOB",
                   {"type": "catalog", "object": "msdb.dbo.sysjobs",
                    "key": f"job_id={j.job_id}"})

        steps = rows(cnx, """
            SELECT step_id, step_name, subsystem, database_name, command
            FROM msdb.dbo.sysjobsteps WHERE job_id = ? ORDER BY step_id
        """, j.job_id)
        for s in steps:
            step_node = g.add_node(f"step:{j.name}#{s.step_id}", "agent_job_step",
                                   f"{j.name} / {s.step_name}",
                                   subsystem=s.subsystem, database_name=s.database_name,
                                   command=s.command)
            g.add_edge(job_node, step_node, "HAS_STEP",
                       {"type": "catalog", "object": "msdb.dbo.sysjobsteps",
                        "key": f"job_id={j.job_id}, step_id={s.step_id}"})

            cmd = s.command or ""
            # step -> database it runs in
            if s.database_name:
                g.add_edge(step_node, f"db:{s.database_name}", "RUNS_ON",
                           {"type": "catalog", "object": "msdb.dbo.sysjobsteps",
                            "key": f"job_id={j.job_id}, step_id={s.step_id}",
                            "detail": f"database_name={s.database_name}"})

            subsystem = (s.subsystem or "").upper()
            if subsystem == "TSQL":
                # step -> object it EXECs (best-effort parse of the T-SQL command)
                target_db = s.database_name or "master"
                _link_exec_targets(g, step_node, cmd, target_db, j.job_id, s.step_id)
                # step -> linked server referenced in the command text
                for ls_lower, ls_node in linked.items():
                    hit = line_of(cmd, f"[{ls_lower}]") or line_of(cmd, ls_lower)
                    if hit:
                        lineno, linetext = hit
                        g.add_edge(step_node, ls_node, "REFERENCES_LINKED_SERVER",
                                   {"type": "jobstep_line",
                                    "object": f"{j.name}#{s.step_id}",
                                    "line": lineno, "text": linetext[:200]})
            else:
                # CmdExec / PowerShell / SSIS: link to the files the step runs.
                # Same file: node-id scheme as Phase 2/3, so these merge across phases.
                for m in STEP_FILE_RE.finditer(cmd):
                    token = next((x for x in m.groups() if x), None)
                    if not token:
                        continue
                    fnorm = token.strip().strip('"').replace("/", "\\")
                    fnode = g.add_node(f"file:{fnorm.lower()}", "file",
                                       os.path.basename(fnorm) or fnorm, path=fnorm)
                    g.add_edge(step_node, fnode, "INVOKES",
                               {"type": "jobstep", "object": "msdb.dbo.sysjobsteps",
                                "key": f"job_id={j.job_id}, step_id={s.step_id}",
                                "text": token})

        # recent history attached as an attribute of the job node
        hist = rows(cnx, f"""
            SELECT TOP {int(history_limit)} h.step_id, h.step_name, h.run_status,
                   h.run_date, h.run_time, h.message
            FROM msdb.dbo.sysjobhistory h
            WHERE h.job_id = ? ORDER BY h.instance_id DESC
        """, j.job_id)
        status_map = {0: "Failed", 1: "Succeeded", 2: "Retry", 3: "Canceled", 4: "InProgress"}
        recent = []
        for h in hist:
            recent.append({
                "step_id": h.step_id,
                "step_name": h.step_name,
                "outcome": status_map.get(h.run_status, str(h.run_status)),
                "run_date": h.run_date,
                "run_time": h.run_time,
                "message": (h.message or "")[:300],
            })
        if recent:
            g._nodes[job_node].attrs["recent_history"] = recent

    if filtered:
        print(f"[filter] skipped {len(filtered)} Microsoft-shipped job(s): {filtered} "
              f"(use --include-system to keep)")


def _link_exec_targets(g: Graph, step_node: str, cmd: str, target_db: str,
                       job_id: Any, step_id: int) -> None:
    seen = set()
    for m in EXEC_RE.finditer(cmd):
        parts = [p for p in m.groups() if p]
        if len(parts) == 3:
            db, sch, name = parts
        elif len(parts) == 2:
            db, sch, name = target_db, parts[0], parts[1]
        else:
            continue
        dst = obj_id(db, sch, name)
        if dst in seen:
            continue
        seen.add(dst)
        g.add_node(dst, "object", f"{sch}.{name}", database=db)
        g.add_edge(step_node, dst, "INVOKES",
                   {"type": "jobstep", "object": "msdb.dbo.sysjobsteps",
                    "key": f"job_id={job_id}, step_id={step_id}",
                    "text": m.group(0)})


# ----------------------------------------------------------------------------
# JSON serialization for datetime/decimal
# ----------------------------------------------------------------------------

def json_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, bytes):
        return o.hex()
    return str(o)


# ----------------------------------------------------------------------------
# Orchestration (reusable by the multi-phase orchestrator)
# ----------------------------------------------------------------------------

def run_phase1(g: Graph, *, server: str, driver: str,
               databases: Optional[List[str]] = None,
               history_limit: int = 20, include_system: bool = False) -> None:
    """Populate graph `g` with Phase 1 (SQL Server catalog + Agent) data."""
    print(f"[phase1] connect {server} via {driver} (read-only, Windows auth)")
    cnx = connect(server, driver, "master")
    try:
        srv_name = rows(cnx, "SELECT @@SERVERNAME AS n")[0].n
        version = rows(cnx, "SELECT SERVERPROPERTY('ProductVersion') AS v, "
                            "SERVERPROPERTY('Edition') AS e")[0]
        server_id = g.add_node(f"server:{srv_name}", "server", srv_name,
                               product_version=str(version.v), edition=str(version.e))

        print("[phase1] databases")
        user_dbs = collect_databases(cnx, g, server_id, include_system=include_system)
        if databases:
            user_dbs = [d for d in user_dbs if d in set(databases)]
        print(f"         user databases: {user_dbs}")

        print("[phase1] linked servers")
        linked = collect_linked_servers(cnx, g, server_id)
        print(f"         linked servers: {list(linked.values())}")

        for db in user_dbs:
            print(f"[phase1] objects/modules/dependencies in [{db}]")
            objmap = collect_db_objects(cnx, g, db)
            mods = collect_modules(cnx, db, objmap)
            collect_dependencies(cnx, g, db, objmap)
            scan_modules_for_linked_servers(g, db, objmap, mods, linked)

        print("[phase1] SQL Server Agent jobs / steps / history")
        collect_agent_jobs(cnx, g, server_id, linked, history_limit=history_limit,
                           include_system=include_system)
    finally:
        cnx.close()


def write_graph(g: Graph, out: str) -> None:
    import os
    graph = g.to_dict()
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2, default=json_default)

    by_type: Dict[str, int] = {}
    for n in graph["nodes"]:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    edge_by_type: Dict[str, int] = {}
    for e in graph["edges"]:
        edge_by_type[e["type"]] = edge_by_type.get(e["type"], 0) + 1
    print(f"\n[done] wrote {out}")
    print(f"       nodes: {len(graph['nodes'])}  {by_type}")
    print(f"       edges: {len(graph['edges'])}  {edge_by_type}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 1 read-only SQL Server collector.")
    ap.add_argument("--server", default="localhost")
    ap.add_argument("--driver", default="ODBC Driver 18 for SQL Server")
    ap.add_argument("--databases", nargs="*", default=None,
                    help="Limit to these user databases (default: all non-system).")
    ap.add_argument("--out", default="output/graph.json")
    ap.add_argument("--history-limit", type=int, default=20)
    ap.add_argument("--include-system", action="store_true",
                    help="Include system databases (master/msdb/...) and Microsoft-shipped Agent jobs.")
    args = ap.parse_args(argv)

    g = Graph()
    run_phase1(g, server=args.server, driver=args.driver, databases=args.databases,
               history_limit=args.history_limit, include_system=args.include_system)
    write_graph(g, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
