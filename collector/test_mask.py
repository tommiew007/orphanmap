"""Verify credential redaction: passwords never survive into the graph, and the
node that carried one is flagged had_credential=true."""
from phase1_sqlserver import Graph, mask_secrets

# unit: the masker itself
for raw, must_gone in [
    ("Provider=SQLOLEDB;Data Source=X;Initial Catalog=Y;User ID=sa;Password=Hunter2;", "Hunter2"),
    ("ODBC;DRIVER=SQL Server;SERVER=X;DATABASE=Y;PWD=s3cr3t!;UID=sa;", "s3cr3t!"),
    ("sqlcmd -S X -d Y -U sa -P MyP@ss -i job.sql", "MyP@ss"),
    ("sqlcmd -S X -d Y -U sa -PMyP@ss -i job.sql", "MyP@ss"),
]:
    masked, hit = mask_secrets(raw)
    assert hit, f"should have flagged a secret in: {raw}"
    assert must_gone not in masked, f"secret {must_gone!r} survived: {masked}"
    assert "***REDACTED***" in masked

# no false positive on trusted connections
for safe in ["Server=localhost;Database=Y;Integrated Security=True;",
             "Server=localhost;Database=Y;Trusted_Connection=yes;",
             "sqlcmd -S localhost -d Y -E -i job.sql"]:
    masked, hit = mask_secrets(safe)
    assert not hit, f"false positive on: {safe}"
    assert masked == safe

# integration: edge citation + node attr both get scrubbed and flagged
g = Graph()
g.add_node("file:x.udl", "file", "x.udl",
           command="sqlcmd -U sa -P TopSecret1 -S H -d D")
g.add_edge("file:x.udl", "db:D", "CONNECTS_TO",
           {"type": "file_line", "object": "x.udl", "line": 1,
            "text": "Provider=SQLOLEDB;Data Source=H;Initial Catalog=D;Password=TopSecret1;"})
d = g.to_dict()
edge = d["edges"][0]
node = {n["id"]: n for n in d["nodes"]}["file:x.udl"]
assert "TopSecret1" not in edge["source"]["text"], "secret leaked in edge citation"
assert "TopSecret1" not in node["attrs"]["command"], "secret leaked in node attr"
assert node["attrs"].get("had_credential") is True, "had_credential flag not set"
# full-graph belt-and-suspenders
import json
assert "TopSecret1" not in json.dumps(d), "secret present somewhere in graph"

print("ALL REDACTION ASSERTIONS PASSED")
print("  edge text:", edge["source"]["text"])
print("  node.command:", node["attrs"]["command"])
print("  node.had_credential:", node["attrs"]["had_credential"])
