"""Offline test of the .accdb binary connect-string scanner. Builds a blob with an
ODBC connect string embedded as UTF-16LE (how Access stores linked-table connects)
inside binary padding, and asserts parse_accdb pulls out the server + database.
This proves the scanner logic; it does not claim to validate a real Jet file layout."""
import os
from phase1_sqlserver import Graph
from phase3_files import parse_accdb

TMP = os.path.join(os.environ.get("TEMP", "."), "synthetic_test.accdb")

connect = "ODBC;DRIVER=SQL Server;SERVER=ARCHIVE-SQL;DATABASE=LegacyDB;Trusted_Connection=Yes;"
blob = (b"\x00\x01\x02JETBINARYHEADER\x00\x00"
        + connect.encode("utf-16-le")          # how Access stores the connect string
        + b"\x00\x00somepadding\x00\x00"
        + b"ODBC;DRIVER=SQL Server;SERVER=localhost;DATABASE=OrphanTestDB;"  # ASCII variant
        + b"\x00\xff")

with open(TMP, "wb") as f:
    f.write(blob)

g = Graph()
parse_accdb(g, TMP)
d = g.to_dict()
edges = d["edges"]
nodes = {n["id"]: n for n in d["nodes"]}

def has(etype, dst):
    return any(e["type"] == etype and e["dst"] == dst for e in edges)

assert has("REFERENCES_SERVER", "sqlserver:archive-sql"), "missed UTF-16LE server"
assert has("CONNECTS_TO", "db:LegacyDB"), "missed UTF-16LE database"
assert has("REFERENCES_SERVER", "sqlserver:localhost"), "missed ASCII server"
assert has("CONNECTS_TO", "db:OrphanTestDB"), "missed ASCII database"
assert all(e.get("source") for e in edges), "edge missing citation"
# citation should carry offset + encoding
assert all("offset" in e["source"] and "encoding" in e["source"]
           for e in edges), "binary citation missing offset/encoding"

os.remove(TMP)
print("ALL .accdb SCANNER ASSERTIONS PASSED")
for e in edges:
    print(f"  {e['type']:18} -> {e['dst']:28} cite enc={e['source']['encoding']} off={e['source']['offset']}")
