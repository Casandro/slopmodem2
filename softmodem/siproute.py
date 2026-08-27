"""Route an inbound call by the number that was dialled.

Nothing in this project has ever looked at *which* number an INVITE was addressed
to. Every program tests `msg.startswith("INVITE ")` and answers; the Request-URI
is never parsed at all, and `To:` is read only to echo it back into a response
with a tag appended. A terminal server needs both, so both are parsed here.

Deliberately free of SIP plumbing: `sip_glue` calls `sipcfg.load()` at *import*
time, which reads testrig/ata.md and raises SystemExit when it is not there. Any
module the offline tests import must therefore not import sip_glue, and that one
constraint is why routing lives in its own file rather than inside termsrv.py.

The rule file is whitespace-delimited on purpose. Regexes routinely contain
commas -- `[0-9]{2,3}` -- and colons, so both of the other obvious delimiters
would need escaping in the one column most likely to want them. They almost
never contain spaces.
"""
import re
import urllib.parse

SCHEMES = ("sip:", "sips:", "tel:")


def uri_user(s):
    """The user part of a SIP URI, or None.

    Tolerates the shapes that actually arrive from a FRITZ!Box: a bare URI in a
    request line, one in angle brackets, one behind a display name, and any of
    them carrying `;tag=` or `;user=phone`. Percent-decoded, because `**620`
    is legal to send as `%2A%2A620` and the two must route alike.
    """
    if not s:
        return None
    s = s.strip()
    lt = s.find("<")
    if lt >= 0:                      # display name and/or header params outside
        gt = s.find(">", lt)
        s = s[lt + 1:gt if gt > lt else len(s)]
    else:
        s = s.split(";", 1)[0].split(",", 1)[0].strip()
    low = s.lower()
    for sch in SCHEMES:
        if low.startswith(sch):
            s = s[len(sch):]
            if sch == "tel:":
                # tel: has no host part at all; the whole thing is the number
                return urllib.parse.unquote(s.split(";", 1)[0]) or None
            break
    s = s.split(";", 1)[0]           # uri params, e.g. ;user=phone
    at = s.rfind("@")
    if at < 0:
        return None                  # sip:host with no user is not a number
    user = s[:at].split(":", 1)[0]   # strip a :password if one is present
    user = urllib.parse.unquote(user)
    return user or None


NUMBER_HEADERS = ("To", "P-Called-Party-ID", "Diversion", "X-Dialed-Number")


def numbers_of(msg):
    """Every number this INVITE could be addressed to, best first, deduplicated.

    The Request-URI and To first, as chosen. Then P-Called-Party-ID, and this is
    not theoretical tidiness -- on the rig it is the only header that carries the
    dialled number at all. A FRITZ!Box rewrites the Request-URI to the registered
    account and copies it into To, so a real inbound call arrives as

        INVITE sip:wurstuser@192.168.5.117:46894 SIP/2.0
        To: <sip:wurstuser@192.168.5.117:46894>
        P-Called-Party-ID: <sip:**9@fritz.box>

    and matching only the first two gives every call the same number -- the
    account name -- no matter what was dialled. RFC 3455 defines the header for
    exactly this, and Diversion covers the forwarded case.

    Returns a list rather than a tuple because the caller wants "everything this
    call could be for"; on a switch that does not rewrite, they are all equal.
    """
    out = []
    first = msg.split("\r\n", 1)[0].split("\n", 1)[0]
    parts = first.split()
    if len(parts) >= 2:
        u = uri_user(parts[1])
        if u:
            out.append(u)
    for name in NUMBER_HEADERS:
        m = re.search(r"^%s:\s*(.+?)\s*$" % re.escape(name), msg, re.I | re.M)
        if not m:
            continue
        u = uri_user(m.group(1))
        if u and u not in out:
            out.append(u)
    return out


class Rule:
    __slots__ = ("rx", "host", "port", "lineno", "raw")

    def __init__(self, rx, host, port, lineno, raw):
        self.rx, self.host, self.port = rx, host, port
        self.lineno, self.raw = lineno, raw

    def __repr__(self):
        return "Rule(%r -> %s:%d)" % (self.rx.pattern, self.host, self.port)


class RuleError(Exception):
    pass


def parse_rules(text, path="<rules>"):
    """Parse the whole file, or raise. Three whitespace-separated columns.

    A bad line fails the load rather than being skipped. A terminal server that
    silently ignores half its routing table is worse than one that refuses to
    start: the calls simply go to the wrong place, or nowhere, and nothing says so.
    """
    rules = []
    for n, line in enumerate(text.splitlines(), 1):
        body = line.split("#", 1)[0].strip()
        if not body:
            continue
        f = body.split()
        if len(f) != 3:
            raise RuleError("%s:%d: expected 3 whitespace-separated columns "
                            "(regex host port), found %d: %r"
                            % (path, n, len(f), body))
        pat, host, port = f
        try:
            rx = re.compile(pat)
        except re.error as e:
            raise RuleError("%s:%d: bad regex %r: %s" % (path, n, pat, e))
        try:
            p = int(port)
        except ValueError:
            raise RuleError("%s:%d: port %r is not a number" % (path, n, port))
        if not 1 <= p <= 65535:
            raise RuleError("%s:%d: port %d out of range 1..65535" % (path, n, p))
        rules.append(Rule(rx, host, p, n, body))
    return rules


def load_rules(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_rules(fh.read(), path)


def route(rules, numbers):
    """First rule matching any of the numbers. One pass over the rules.

    The nesting matters and is the semantics chosen deliberately: rules are the
    outer loop, so an earlier rule matching the To-number beats a later rule
    matching the Request-URI. Nesting it the other way round gives first-*number*
    wins, which is a different answer and quietly reorders the routing table.

    fullmatch, not search: `620` must not match `**6201`. A prefix rule is
    spelled `620.*`, which is what the example file does.
    """
    for r in rules:
        for num in numbers:
            if r.rx.fullmatch(num):
                return r, num
    return None, None
