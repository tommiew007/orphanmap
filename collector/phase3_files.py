r"""
Phase 3 collector: walk a directory tree and extract SQL references from the
kinds of files a departed developer leaves behind.

READ-ONLY. Files are opened for reading only; nothing is written to the tree.

Handles:
  .bat/.cmd   - sqlcmd/osql/bcp invocations (-S server, -d db, -i script) + file refs
  .ps1        - connection strings, Invoke-Sqlcmd -ServerInstance/-Database, file refs
  .sql        - USE <db>, EXEC/FROM/JOIN table refs, 4-part linked-server refs
  .dtsx       - SSIS ConnectionString values (Data Source / Initial Catalog)
  .rdl        - SSRS DataSource ConnectString values
  .xlsx/.xlsm - xl/connections.xml external-data connection strings + command
  .accdb/.mdb - binary scan for embedded ODBC connect strings + linked source tables

Edges use the SAME node-id scheme as Phase 1 (db:<name>, obj:<db>.<schema>.<table>)
so a file that connects to OrphanTestDB / reads dbo.Customers wires straight into the
live SQL objects. Every edge carries a `source` citation: file path + line, XML/zip
part, or byte offset.
"""

from __future__ import annotations

import os
import re
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from phase1_sqlserver import Graph, write_graph

# ---- connection-string / command parsing ---------------------------------

# Stop at ; " ' < > and whitespace-newline so XML element values like
# <ConnectString>Data Source=x;Initial Catalog=y</ConnectString> don't swallow the tag.
SERVER_RE = re.compile(r"(?:\bSERVER\b|\bDATA\s+SOURCE\b)\s*=\s*([^;\"'<>\r\n]+)", re.I)
DB_RE = re.compile(r"(?:\bDATABASE\b|\bINITIAL\s+CATALOG\b)\s*=\s*([^;\"'<>\r\n]+)", re.I)

SQLCMD_EXE_RE = re.compile(r"\b(sqlcmd|osql|bcp)\b", re.I)
SQLCMD_S_RE = re.compile(r"[-/]S[ :=]?\s*\"?([A-Za-z0-9_.\\\-]+)\"?")
SQLCMD_D_RE = re.compile(r"[-/]d[ :=]?\s*\"?([A-Za-z0-9_.\\\-]+)\"?")
SQLCMD_I_RE = re.compile(r"[-/]i[ :=]?\s*\"([^\"]+)\"|[-/]i[ :=]?\s*(\S+)", re.I)

INVOKE_SRV_RE = re.compile(r"-ServerInstance\s+\"?([A-Za-z0-9_.\\\-]+)\"?", re.I)
INVOKE_DB_RE = re.compile(r"-Database\s+\"?([A-Za-z0-9_.\\\-]+)\"?", re.I)

USE_RE = re.compile(r"\bUSE\s+\[?(\w+)\]?", re.I)
TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|EXEC(?:UTE)?)\s+\[?(\w+)\]?\.\[?(\w+)\]?", re.I)

FILE_EXT = r"ps1|bat|cmd|vbs|sql|py|exe|accdb|mdb|xlsx|xlsm|dtsx|rdl|jar|dll"
FILE_TOKEN_RE = re.compile(
    r'"([A-Za-z]:\\[^"]+?\.(?:' + FILE_EXT + r'))"'
    r'|([A-Za-z]:\\[^\s"]+?\.(?:' + FILE_EXT + r'))'
    r'|(\b[\w.\-]+\.(?:' + FILE_EXT + r'))\b', re.I)

SCANNED_EXTS = {".bat", ".cmd", ".ps1", ".sql", ".dtsx", ".rdl", ".xlsx", ".xlsm",
                ".accdb", ".mdb"}

SERVER_ALIASES = {".": "localhost", "(local)": "localhost"}


def _norm(p: str) -> str:
    return p.strip().strip('"').replace("/", "\\")


def _server_id(server: str) -> str:
    s = server.strip()
    return SERVER_ALIASES.get(s.lower(), s).lower()


def _file_node(g: Graph, path: str) -> str:
    n = _norm(path)
    return g.add_node(f"file:{n.lower()}", "file", os.path.basename(n) or n, path=n)


def _emit_server(g: Graph, file_node: str, server: str, cite: Dict[str, Any]) -> None:
    sid = _server_id(server)
    node = g.add_node(f"sqlserver:{sid}", "sql_server_ref", server.strip())
    g.add_edge(file_node, node, "REFERENCES_SERVER", cite)


def _emit_db(g: Graph, file_node: str, database: str, cite: Dict[str, Any]) -> None:
    db = database.strip()
    node = g.add_node(f"db:{db}", "database", db)  # merges with Phase 1 db node
    g.add_edge(file_node, node, "CONNECTS_TO", cite)


def _emit_table(g: Graph, file_node: str, db: str, schema: str, table: str,
                cite: Dict[str, Any]) -> None:
    node = g.add_node(f"obj:{db}.{schema}.{table}", "object", f"{schema}.{table}",
                      database=db)  # merges with Phase 1 object node
    g.add_edge(file_node, node, "REFERENCES_TABLE", cite)


def _cite(path: str, **extra: Any) -> Dict[str, Any]:
    c = {"object": path}
    c.update(extra)
    return c


# ---- per-type parsers -----------------------------------------------------

def _parse_connstr_line(g: Graph, file_node: str, path: str, line: str, lineno: int,
                        ctype: str, default_db: Optional[str] = None) -> Optional[str]:
    """Extract server+db from any connection-string-ish text on a line. Returns db if found."""
    srv = SERVER_RE.search(line)
    db = DB_RE.search(line)
    found_db = None
    if srv:
        _emit_server(g, file_node, srv.group(1),
                     _cite(path, type=ctype, line=lineno, text=line.strip()[:200]))
    if db:
        found_db = db.group(1).strip()
        _emit_db(g, file_node, found_db,
                 _cite(path, type=ctype, line=lineno, text=line.strip()[:200]))
    return found_db


def parse_bat(g: Graph, path: str, text: str) -> None:
    fn = _file_node(g, path)
    for i, line in enumerate(text.splitlines(), 1):
        if SQLCMD_EXE_RE.search(line):
            db_here = None
            ms = SQLCMD_S_RE.search(line)
            if ms:
                _emit_server(g, fn, ms.group(1),
                             _cite(path, type="file_line", line=i, text=line.strip()[:200]))
            md = SQLCMD_D_RE.search(line)
            if md:
                db_here = md.group(1)
                _emit_db(g, fn, db_here,
                         _cite(path, type="file_line", line=i, text=line.strip()[:200]))
            mi = SQLCMD_I_RE.search(line)
            if mi:
                script = mi.group(1) or mi.group(2)
                if script:
                    sfn = _file_node(g, script)
                    g.add_edge(fn, sfn, "REFERENCES",
                               _cite(path, type="file_line", line=i, text=script))
        # generic connection strings (e.g. a set VAR=Server=...;Database=...)
        _parse_connstr_line(g, fn, path, line, i, "file_line")


def parse_ps1(g: Graph, path: str, text: str) -> None:
    fn = _file_node(g, path)
    for i, line in enumerate(text.splitlines(), 1):
        _parse_connstr_line(g, fn, path, line, i, "file_line")
        ms = INVOKE_SRV_RE.search(line)
        if ms:
            _emit_server(g, fn, ms.group(1),
                         _cite(path, type="file_line", line=i, text=line.strip()[:200]))
        md = INVOKE_DB_RE.search(line)
        if md:
            _emit_db(g, fn, md.group(1),
                     _cite(path, type="file_line", line=i, text=line.strip()[:200]))
        for m in FILE_TOKEN_RE.finditer(line):
            tok = next((x for x in m.groups() if x), None)
            if tok and os.path.splitext(tok)[1].lower() in (".ps1", ".bat", ".sql", ".cmd"):
                if _norm(tok).lower() != _norm(path).lower():
                    g.add_edge(fn, _file_node(g, tok), "REFERENCES",
                               _cite(path, type="file_line", line=i, text=tok))


def parse_sql(g: Graph, path: str, text: str) -> None:
    fn = _file_node(g, path)
    cur_db = None
    for i, line in enumerate(text.splitlines(), 1):
        mu = USE_RE.search(line)
        if mu:
            cur_db = mu.group(1)
            _emit_db(g, fn, cur_db, _cite(path, type="file_line", line=i, text=line.strip()[:200]))
        if cur_db:
            for m in TABLE_REF_RE.finditer(line):
                schema, name = m.group(1), m.group(2)
                _emit_table(g, fn, cur_db, schema, name,
                            _cite(path, type="file_line", line=i, text=line.strip()[:200]))


def _parse_xml_connstrings(g: Graph, path: str, text: str, ctype: str) -> None:
    fn = _file_node(g, path)
    # find connection-string-bearing lines; SSIS uses ConnectionString=, RDL uses <ConnectString>
    for i, line in enumerate(text.splitlines(), 1):
        if "connectionstring" in line.lower() or "connectstring" in line.lower() \
           or SERVER_RE.search(line) or DB_RE.search(line):
            _parse_connstr_line(g, fn, path, line, i, ctype)


def parse_dtsx(g: Graph, path: str, text: str) -> None:
    _parse_xml_connstrings(g, path, text, "file_xml")


def parse_rdl(g: Graph, path: str, text: str) -> None:
    _parse_xml_connstrings(g, path, text, "file_xml")
    # also capture dataset table refs when a db is known from the connstring
    fn = f"file:{_norm(path).lower()}"
    dbs = [n for n in [DB_RE.search(l) for l in text.splitlines()] if n]
    db = dbs[0].group(1).strip() if dbs else None
    if db:
        for m in TABLE_REF_RE.finditer(text):
            _emit_table(g, fn, db, m.group(1), m.group(2),
                        _cite(path, type="file_xml", text=m.group(0)))


def parse_xlsx(g: Graph, path: str) -> None:
    fn = _file_node(g, path)
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith("connections.xml")]
            for part in names:
                data = z.read(part).decode("utf-8", "ignore")
                for m in re.finditer(r'connection\s*=\s*"([^"]+)"', data, re.I):
                    cs = m.group(1)
                    srv = SERVER_RE.search(cs)
                    db = DB_RE.search(cs)
                    if srv:
                        _emit_server(g, fn, srv.group(1),
                                     _cite(path, type="file_zip", part=part, text=cs[:200]))
                    if db:
                        _emit_db(g, fn, db.group(1),
                                 _cite(path, type="file_zip", part=part, text=cs[:200]))
                # command="SELECT ... FROM dbo.X"
                dbname = None
                mdb = DB_RE.search(data)
                if mdb:
                    dbname = mdb.group(1).strip()
                if dbname:
                    for cm in re.finditer(r'command\s*=\s*"([^"]+)"', data, re.I):
                        for tm in TABLE_REF_RE.finditer(cm.group(1)):
                            _emit_table(g, fn, dbname, tm.group(1), tm.group(2),
                                        _cite(path, type="file_zip", part=part, text=cm.group(1)[:200]))
    except zipfile.BadZipFile:
        pass


def parse_accdb(g: Graph, path: str) -> None:
    """Binary scan for embedded ODBC connect strings + linked source tables.
    Access stores these as ASCII and/or UTF-16LE text; scan both."""
    fn = _file_node(g, path)
    with open(path, "rb") as f:
        raw = f.read()
    texts = [("ascii", raw.decode("latin-1", "ignore")),
             ("utf16le", raw.decode("utf-16-le", "ignore"))]
    seen = set()
    for enc, t in texts:
        # capture the whole connect string run after ODBC; (greedy to a null/newline
        # boundary) so SERVER= / DATABASE= aren't truncated at the first semicolon
        for m in re.finditer(r"ODBC;[^\x00\r\n]{0,600}", t):
            cs = m.group(0)
            srv = SERVER_RE.search(cs)
            db = DB_RE.search(cs)
            key = (enc, srv.group(1) if srv else "", db.group(1) if db else "")
            if key in seen:
                continue
            seen.add(key)
            cite = _cite(path, type="file_binary", offset=m.start(), encoding=enc,
                         text=cs[:200])
            if srv:
                _emit_server(g, fn, srv.group(1), cite)
            if db:
                _emit_db(g, fn, db.group(1), cite)


DISPATCH_TEXT = {
    ".bat": parse_bat, ".cmd": parse_bat, ".ps1": parse_ps1, ".sql": parse_sql,
    ".dtsx": parse_dtsx, ".rdl": parse_rdl,
}


# ---- walker ---------------------------------------------------------------

def run_phase3(g: Graph, *, scan_dir: str, max_files: int = 5000) -> None:
    """Populate graph `g` with Phase 3 (file-tree) data."""
    print(f"[phase3] scanning {scan_dir}")
    scanned = 0
    capped = False
    for root, _dirs, files in os.walk(scan_dir):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in SCANNED_EXTS:
                continue
            if scanned >= max_files:
                capped = True
                break
            full = os.path.join(root, name)
            scanned += 1
            try:
                if ext in (".xlsx", ".xlsm"):
                    parse_xlsx(g, full)
                elif ext in (".accdb", ".mdb"):
                    parse_accdb(g, full)
                else:
                    with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                        text = fh.read()
                    DISPATCH_TEXT[ext](g, full, text)
            except Exception as e:
                print(f"[phase3] WARN could not parse {full}: {e}")
        if capped:
            break
    print(f"         files scanned: {scanned}"
          + (f"  (CAPPED at {max_files}; more files not scanned)" if capped else ""))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Phase 3 read-only file-tree collector.")
    ap.add_argument("--scan-dir", required=True)
    ap.add_argument("--out", default="output/files_graph.json")
    ap.add_argument("--max-files", type=int, default=5000)
    args = ap.parse_args(argv)
    g = Graph()
    run_phase3(g, scan_dir=args.scan_dir, max_files=args.max_files)
    write_graph(g, args.out)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
