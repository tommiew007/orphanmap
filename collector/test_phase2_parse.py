"""Offline test of Phase 2 argument parsing (no system writes, no live query).
Feeds synthetic scheduled-task actions like an orphaned server would have and
asserts the RUNS / REFERENCES / REFERENCES_SERVER edges + citations come out right."""
from phase1_sqlserver import Graph
from phase2_taskscheduler import run_phase2

SYNTHETIC = [
    # sqlcmd nightly load against a server, running a .sql script
    {"TaskName": "NightlyETL", "TaskPath": "\\Legacy\\", "State": "Ready", "Author": "OLDADMIN",
     "Execute": "sqlcmd.exe",
     "Arguments": "-S ARCHIVE-SQL -d LegacyDB -i \"D:\\Jobs\\load_customers.sql\" -o D:\\Jobs\\log.txt"},
    # powershell running a maintenance script
    {"TaskName": "PurgeTemp", "TaskPath": "\\Legacy\\", "State": "Ready", "Author": "OLDADMIN",
     "Execute": "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
     "Arguments": "-ExecutionPolicy Bypass -File C:\\Scripts\\purge_temp.ps1"},
    # a batch file that shells an Access db
    {"TaskName": "AccessImport", "TaskPath": "\\Legacy\\", "State": "Disabled", "Author": "OLDADMIN",
     "Execute": "cmd.exe", "Arguments": "/c C:\\imports\\run_import.bat customers.accdb"},
    # a Microsoft task that must be filtered by default
    {"TaskName": "SomeMsTask", "TaskPath": "\\Microsoft\\Windows\\Foo\\", "State": "Ready",
     "Author": "Microsoft", "Execute": "C:\\Windows\\system32\\foo.exe", "Arguments": ""},
]

g = Graph()
run_phase2(g, actions=SYNTHETIC)
d = g.to_dict()

edges = d["edges"]
nodes = {n["id"]: n for n in d["nodes"]}


def has_edge(etype, src_sub, dst_sub):
    for e in edges:
        if e["type"] == etype and src_sub in e["src"] and dst_sub in e["dst"]:
            return e
    return None


# Microsoft task filtered out
assert "task:\\Microsoft\\Windows\\Foo\\SomeMsTask" not in nodes, "Microsoft task should be filtered"

# RUNS edges
assert has_edge("RUNS", "NightlyETL", "exe:sqlcmd.exe"), "sqlcmd RUNS edge missing"
e_ps = has_edge("RUNS", "PurgeTemp", "powershell.exe")
assert e_ps, "powershell RUNS edge missing"

# sqlcmd -> .sql file reference, with the arg cited
e_sql = has_edge("REFERENCES", "NightlyETL", "load_customers.sql")
assert e_sql and e_sql["source"]["type"] == "task_action_arg", "sql file REFERENCES missing"

# sqlcmd -> server target ARCHIVE-SQL (ties to the SQL fixture's linked server name)
e_srv = has_edge("REFERENCES_SERVER", "NightlyETL", "sqlserver:archive-sql")
assert e_srv and "ARCHIVE-SQL" in e_srv["source"]["text"], "server REFERENCES_SERVER missing"

# powershell -> .ps1 file reference
assert has_edge("REFERENCES", "PurgeTemp", "purge_temp.ps1"), "ps1 REFERENCES missing"

# batch task -> .bat and .accdb references
assert has_edge("REFERENCES", "AccessImport", "run_import.bat"), "bat REFERENCES missing"
assert has_edge("REFERENCES", "AccessImport", "customers.accdb"), "accdb REFERENCES missing"

# every edge carries a source citation
assert all(e.get("source") for e in edges), "an edge is missing its source citation"

print("ALL PHASE 2 PARSE ASSERTIONS PASSED")
print(f"nodes={len(d['nodes'])} edges={len(edges)}")
for e in edges:
    print(f"  {e['type']:20} {e['src']}  ->  {e['dst']}")
    print(f"      cite: {e['source']}")
