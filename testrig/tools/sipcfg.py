"""Load SIP credentials from testrig/ata.md (or env) so they live in one place only."""
import os, re

def load(path=None):
    host = os.environ.get("SIP_HOST")
    user = os.environ.get("SIP_USER")
    pw   = os.environ.get("SIP_PW")
    if host and user and pw:
        return host, user, pw
    if path is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, os.pardir, "ata.md")
    txt = open(path, encoding="utf-8", errors="replace").read()
    h = re.search(r"(\d+\.\d+\.\d+\.\d+)", txt)
    u = re.search(r"(?:Username|User)\s+(\S+)", txt, re.I)
    p = re.search(r"(?:Passwort|Password|Passwd)\s+(\S+)", txt, re.I)
    if not (h and u and p):
        raise SystemExit("could not parse SIP credentials from %s" % path)
    return host or h.group(1), user or u.group(1), pw or p.group(1)
