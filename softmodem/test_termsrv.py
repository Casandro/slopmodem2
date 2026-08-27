"""Terminal server: routing, the byte bridge, and the whole termination policy.

Offline. Imports siproute/tcpbridge/v32flow but never termsrv or sip_glue --
sip_glue calls sipcfg.load() at import time and raises SystemExit without
testrig/ata.md, which would make this suite unrunnable on any machine that is
not the rig.

The soft-to-soft cases at the end use a real 127.0.0.1 socket rather than
socketpair(): socketpair is AF_UNIX, and the partial-write and buffer-size
behaviour that the bridge exists to handle is what we want under test.
"""
import socket
import sys

import channel
import siproute
import tcpbridge
import v32flow
import v32fsm

FAIL = []


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


# ---------------- rule file ----------------
print("routing: the rule file")

GOOD = """
# regex          host            port
\\*\\*62[0-9]      127.0.0.1       23
\\*\\*7[0-9]{2,3}\tbbs.example.org\t2323
.*               10.0.0.5        2000
"""
rules = siproute.parse_rules(GOOD, "t")
check("comments and blank lines skipped, order kept", len(rules) == 3,
      "%d rules" % len(rules))
check("tabs are a delimiter too", rules[1].host == "bbs.example.org")
check("a regex containing a comma survives -- {2,3}",
      rules[1].rx.pattern == "\\*\\*7[0-9]{2,3}")
check("ports parsed", [r.port for r in rules] == [23, 2323, 2000])


def raises(text):
    try:
        siproute.parse_rules(text, "t")
    except siproute.RuleError as e:
        return str(e)
    return None


e = raises("^620$  host\n")
check("a two-column line fails the load", e and "3 whitespace" in e, e or "")
check("... and names the line number", e and "t:1:" in e, e or "")
e = raises("# c\n\n^6(20$  h  23\n")
check("an uncompilable regex fails the load", e and "bad regex" in e, e or "")
check("... at the right line number", e and "t:3:" in e, e or "")
check("a non-numeric port fails", raises("x h zz\n") is not None)
check("port 0 is rejected", raises("x h 0\n") is not None)
check("port 65536 is rejected", raises("x h 65536\n") is not None)
check("port 65535 is accepted", raises("x h 65535\n") is None)

# ---------------- URI user part ----------------
print("routing: the user part of a SIP URI")
U = siproute.uri_user
check("bare request URI with a port", U("sip:**620@127.0.0.1:5060") == "**620")
check("angle brackets", U("<sip:**620@127.0.0.1>") == "**620")
check("display name and a header tag do not leak",
      U('"Telefon" <sip:**1@fritz.box>;tag=CAFEBABE') == "**1",
      repr(U('"Telefon" <sip:**1@fritz.box>;tag=CAFEBABE')))
check("uri parameters are stripped", U("sip:620@h;user=phone") == "620")
check("sips: is a scheme too", U("sips:620@h") == "620")
check("a password is not part of the number", U("sip:620:secret@h") == "620")
check("percent-encoding is decoded", U("sip:%2A%2A620@h") == "**620")
check("tel: has no host and is all number", U("tel:+4915112345") == "+4915112345")
check("a URI with no user is not a number", U("sip:host.example") is None)
check("empty input", U("") is None and U(None) is None)

# ---------------- numbers off an INVITE ----------------
print("routing: both numbers off an INVITE")
import test_modem                                  # reuse the committed fixture
nums = siproute.numbers_of(test_modem.INVITE)
check("request-URI and To agree, so one number", nums == ["**620"], repr(nums))

SPLIT = ("INVITE sip:2323@box SIP/2.0\r\n"
         "To: <sip:**620@box>\r\n"
         "From: <sip:**1@box>;tag=X\r\n\r\n")
check("when they differ, both are offered, request-URI first",
      siproute.numbers_of(SPLIT) == ["2323", "**620"],
      repr(siproute.numbers_of(SPLIT)))

# What a FRITZ!Box actually sends: the request URI and To are the registered
# account, and the dialled number is only in P-Called-Party-ID. Captured off
# the rig; without this header every call routes as "wurstuser".
FRITZ = ("INVITE sip:acct@192.168.5.117:46894 SIP/2.0\r\n"
         "From: \"Telefon\" <sip:**1@fritz.box>;tag=B2B\r\n"
         "To: <sip:acct@192.168.5.117:46894>\r\n"
         "P-Called-Party-ID: <sip:**9@fritz.box>\r\n\r\n")
check("the dialled number is found in P-Called-Party-ID",
      siproute.numbers_of(FRITZ) == ["acct", "**9"],
      repr(siproute.numbers_of(FRITZ)))
check("... and a rule on it routes the call",
      siproute.route(siproute.parse_rules("\\*\\*9  h  1\n", "t"),
                     siproute.numbers_of(FRITZ))[1] == "**9")

# ---------------- route order ----------------
print("routing: one pass over the rules, both numbers")
R = siproute.parse_rules("\\*\\*620  a  1\n2323  b  2\n", "t")
r, num = siproute.route(R, ["2323", "**620"])
check("an earlier rule matching the To number beats a later rule matching "
      "the request URI", r is not None and r.host == "a" and num == "**620",
      repr((r, num)))
R2 = siproute.parse_rules("2323  b  2\n\\*\\*620  a  1\n", "t")
r2, _ = siproute.route(R2, ["2323", "**620"])
check("... and the mirror image", r2 is not None and r2.host == "b")
check("no rule matches gives nothing",
      siproute.route(R, ["9999"]) == (None, None))
check("fullmatch, so 620 does not match **6201",
      siproute.route(siproute.parse_rules("620  a  1\n", "t"), ["**6201"])[0] is None)
check("a prefix rule is spelled 620.*",
      siproute.route(siproute.parse_rules("620.*  a  1\n", "t"), ["6201"])[0] is not None)


# ---------------- feed budget ----------------
print("flow: feed_budget matches the expression it was extracted from")


class _Enc:
    def __init__(self, n):
        self.n = n

    def pending(self):
        return self.n


class _Lapm:
    def __init__(self, q):
        self.outq = bytearray(q)


class _Link:
    def __init__(self, q):
        self.lapm = _Lapm(q)


class _Ec:
    def __init__(self, up, q, phase="lapm"):
        self.up = up
        self.link = _Link(q)
        self.phase = phase


class _M:
    def __init__(self, ec=None, ecq=b"", enc=0, want_ec=False,
                 fell=False, rate=9600):
        self.ec = ec
        self.ecq = bytearray(ecq)
        self.enc = _Enc(enc)
        self.want_ec = want_ec
        self.ec_fell_back = fell
        self.rate = rate


def inline(m, feed, ahead=64, margin=0.95, lapm_hi=4096):
    """The expression as it stands inlined in v32answer.py and v32call.py."""
    if m.ec is not None:
        return feed if (m.ec.up and len(m.ec.link.lapm.outq) < lapm_hi) else 0
    if m.want_ec and not m.ec_fell_back:
        return min(feed, ahead - len(m.ecq))
    per_frame = int((m.rate or 4800) / 500.0 * margin)
    return min(feed, max(per_frame, 1), ahead - m.enc.pending())


cases = []
for up in (True, False):
    for q in (0, 4095, 4096, 8192):
        cases.append(_M(ec=_Ec(up, q)))
for n in (0, 32, 64, 96):
    cases.append(_M(ecq=b"x" * n, want_ec=True))
for rate in (None, 4800, 7200, 9600, 12000, 14400):
    for n in (0, 32, 64, 80):
        cases.append(_M(enc=n, rate=rate))
bad = [(i, inline(m, 64), v32flow.feed_budget(m, 64))
       for i, m in enumerate(cases)
       if inline(m, 64) != v32flow.feed_budget(m, 64)]
check("identical across %d cases (LAPM, detection, V.14 at every rate)"
      % len(cases), not bad, repr(bad[:3]))
m = _M(ec=_Ec(True, 2000))
check("a lower lapm_hi does bite", v32flow.feed_budget(m, 64, lapm_hi=1024) == 0
      and v32flow.feed_budget(m, 64, lapm_hi=4096) == 64)


# ---------------- partial writes ----------------
print("bridge: partial writes and the recv(0) trap")


class _Sock:
    """Accepts 7 bytes a call and would-blocks every third."""

    def __init__(self, feed=b""):
        self.got = bytearray()
        self.feed = bytearray(feed)
        self.calls = 0
        self.recv_sizes = []

    def send(self, view):
        self.calls += 1
        if self.calls % 3 == 0:
            raise BlockingIOError()
        b = bytes(view)[:7]
        self.got.extend(b)
        return len(b)

    def recv(self, n, flags=0):
        self.recv_sizes.append(n)
        if not self.feed:
            raise BlockingIOError()
        b = bytes(self.feed[:n])
        if not flags:
            del self.feed[:n]
        return b

    def shutdown(self, how):
        pass

    def close(self):
        pass


class _FakeM:
    """Just enough modem for the bridge: DATA, LAPM up, a byte source."""

    def __init__(self, src=b"", rate=9600):
        self.state = v32fsm.DATA
        self.ec = _Ec(True, 0)
        self.ecq = bytearray()
        self.enc = _Enc(0)
        self.want_ec = True
        self.ec_fell_back = False
        self.rate = rate
        self.rx = None
        self.retrains = 0
        self.src = bytearray(src)
        self.put_got = bytearray()

    def received(self):
        out = bytes(self.src[:200])
        del self.src[:200]
        return out

    def put(self, d):
        self.put_got.extend(d)


payload = bytes(range(256)) * 256                   # 64 KiB
fm = _FakeM(src=payload)
sk = _Sock()
br = tcpbridge.Bridge(fm, sk, t0=0.0)
for i in range(20000):
    br.last_rtp = i * 0.02
    br.frame(i * 0.02)
    if not fm.src and br.stats()["pending"] == 0:
        break
check("64 KiB crosses byte for byte through 7-byte partial writes",
      bytes(sk.got) == payload, "%d of %d" % (len(sk.got), len(payload)))
check("recv() is never called with 0", all(n > 0 for n in sk.recv_sizes),
      repr([n for n in sk.recv_sizes if n <= 0][:3]))

# a full modem queue must not make us read, and must not miss a FIN
fm2 = _FakeM()
fm2.ec = _Ec(True, 99999)                            # queue over any threshold
sk2 = _Sock()
br2 = tcpbridge.Bridge(fm2, sk2, t0=0.0)
br2.frame(0.0)
check("a full transmit queue means no read at all", sk2.recv_sizes == [])
br2.frame(2.0)
check("... but a FIN is still noticed, by MSG_PEEK",
      len(sk2.recv_sizes) == 1 and sk2.recv_sizes[0] == 1)


# ---------------- termination policy ----------------
print("bridge: every way a call ends")


def fresh(**kw):
    m = _FakeM(**kw)
    b = tcpbridge.Bridge(m, _Sock(), t0=0.0)
    b.last_rtp = 0.0
    return m, b


def ask(b, now):
    """stop_reason on a call whose RTP is still flowing.

    A live call refreshes last_rtp every frame, so a test that does not is
    really testing rtp-dead -- which it will get, since that branch outranks
    almost everything by design.
    """
    b.last_rtp = now
    return b.stop_reason(now)


m, b = fresh()
check("nothing wrong, nothing returned", ask(b, 1.0) is None)
check("no handshake inside the limit", ask(b, 59.0) is None)
check("no handshake past 60 s", ask(b, 61.0) == "no-handshake")

m, b = fresh()
b.t_data = 0.0
check("rtp-dead at 10 s of no inbound frame", b.stop_reason(11.0) == "rtp-dead")
check("... and it outranks the rest, which is why ask() exists",
      b.stop_reason(9.0) is None)

m, b = fresh()
b.t_data = 0.0
b.fatal = "tcp-error-104"
check("a fatal socket error outranks everything",
      ask(b, 100.0) == "tcp-error-104")

m, b = fresh()
b.t_data = 0.0
b.last_move = 60.0
b.over_since = 60.0
check("tcp-stalled only after 30 s over the mark",
      ask(b, 89.0) is None and ask(b, 91.0) == "tcp-stalled")

# carrier-lost is armed only in DATA, and reset by leaving it
m, b = fresh()
b.t_data = 0.0
b.last_move = 100.0
check("a dead receiver in DATA is carrier-lost after 5 s",
      ask(b, 100.0) is None and ask(b, 106.0) == "carrier-lost")
m, b = fresh()
b.t_data = 0.0
b.last_move = 100.0
m.state = "RC1"
check("a legitimate retrain is not carrier-lost",
      ask(b, 100.0) is None and ask(b, 106.0) is None)
check("... but a retrain that never ends is retrain-stuck",
      ask(b, 121.0) == "retrain-stuck")

# lapm-down must not fire before the link was ever up
m, b = fresh()
b.t_data = 0.0
b.last_move = 100.0
m.rx = type("R", (), {"rx": type("E", (), {"dd": True})()})()
m.ec.link.lapm.state = "disconnected"
check("lapm-down does not fire at t=0, before it was ever up",
      ask(b, 100.0) is None)
b.ec_was_up = True
check("... and does once it has been", ask(b, 100.0) == "lapm-down")

# idle
m, b = fresh()
b.t_data = 0.0
b.last_move = 0.0
m.rx = type("R", (), {"rx": type("E", (), {"dd": True})()})()
m.ec.link.lapm.state = "connected"
check("idle at 900 s of no movement",
      ask(b, 899.0) is None and ask(b, 901.0) == "idle")

# tcp-eof drain conditions
m, b = fresh()
b.t_data = 0.0
b.last_move = 1000.0
m.rx = type("R", (), {"rx": type("E", (), {"dd": True})()})()
m.ec.link.lapm.state = "connected"
m.ec.link.lapm.sent = {1: b"x"}
b._saw_fin(1000.0)
check("FIN with an unacknowledged I frame is not drained",
      ask(b, 1000.5) is None)
m.ec.link.lapm.sent = {}
check("... nor immediately once it is acknowledged, until it settles",
      ask(b, 1000.6) is None)
check("... and then tcp-eof", ask(b, 1001.0) == "tcp-eof")
check("shutdown(SHUT_WR) was sent", b.wr_shut)

m, b = fresh()
b.t_data = 0.0
b.last_move = 1000.0
m.rx = type("R", (), {"rx": type("E", (), {"dd": True})()})()
m.ec.link.lapm.state = "connected"
m.ec.link.lapm.sent = {1: b"x"}
b._saw_fin(1000.0)
check("a link that never drains gives up at POST_FIN_MAX",
      ask(b, 1016.0) == "post-fin-timeout")


# ---------------- soft to soft, over a real socket ----------------
print("bridge: soft to soft over 127.0.0.1")


def soft_call(rate=9600, org_ec=True, frames=1500, org_chats=False,
              hit=None, feed=64):
    """Answerer bridged to a TCP socket; originator stands in for the caller."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.create_connection(srv.getsockname())
    host, _ = srv.accept()
    srv.close()
    for s in (cli, host):
        s.setblocking(False)
    ca = channel.make("perfect")
    co = channel.make("perfect", seed=2)
    ans = v32fsm.AnswerStartup(level_dbfs=-24.0, ans_s=0.6, rates=(rate,),
                               bis=True, trellis=True, ec=True)
    org = v32fsm.OriginateStartup(level_dbfs=-24.0, rates=(rate,), bis=True,
                                  trellis=True, ec=org_ec)
    br = tcpbridge.Bridge(ans, host, feed=feed, t0=0.0)
    import random
    rng = random.Random(99)
    to_a = to_o = [0] * 160
    pat = bytes(range(33, 127))
    to_line = bytearray()          # what the caller sent, in order
    at_caller = bytearray()        # what the caller received
    at_host = bytearray()          # what the host received
    i = 0
    for k in range(frames):
        now = k * 0.02
        br.last_rtp = now
        oa = ans.step(to_a)
        oo = org.step(to_o)
        to_o = co.step(oa)
        to_a = ca.step(oo)
        if hit and hit[0] <= now < hit[0] + hit[1]:
            amp = hit[2] * (sum(abs(v) for v in to_a) / max(1, len(to_a)) + 1.0)
            to_a = [int(v + rng.gauss(0, amp)) for v in to_a]
            to_o = [int(v + rng.gauss(0, amp)) for v in to_o]
        br.frame(now)
        if org.state == v32fsm.DATA and (org_chats or org_ec):
            w = v32flow.feed_budget(org, feed)
            if w > 0:
                chunk = (pat * 3)[i % 94:i % 94 + w]
                org.put(chunk)
                to_line.extend(chunk)
                i += w
        at_caller.extend(org.received())
        # the host: consume, and offer a stream of its own
        try:
            d = cli.recv(65536)
            if d:
                at_host.extend(d)
        except BlockingIOError:
            pass
        try:
            cli.send(bytes((pat * 2)[k % 94:k % 94 + 32]))
        except (BlockingIOError, OSError):
            pass
    return dict(ans=ans, org=org, br=br, cli=cli, host=host,
                to_line=bytes(to_line), at_caller=bytes(at_caller),
                at_host=bytes(at_host))


r = soft_call(org_ec=True)
check("(a) V.42: both ends reach the data phase",
      r["ans"].state == v32fsm.DATA and r["org"].state == v32fsm.DATA)
check("(a) V.42: LAPM up on both ends",
      r["ans"].ec is not None and r["ans"].ec.up and r["org"].ec.up)
check("(a) caller -> host, byte exact",
      r["at_host"] == r["to_line"][:len(r["at_host"])] and len(r["at_host"]) > 4000,
      "%d octets" % len(r["at_host"]))
check("(a) host -> caller carried too", len(r["at_caller"]) > 4000,
      "%d octets" % len(r["at_caller"]))
check("(a) nothing was discarded", r["br"].stats()["discarded"] == 0)
for s in (r["cli"], r["host"]):
    try:
        s.close()
    except OSError:
        pass

r = soft_call(org_ec=False, org_chats=True, frames=1800)
check("(b) V.14 far end that chatters: the wedge is broken by the watchdog",
      r["ans"].ec is None and r["ans"].ec_fell_back,
      repr(r["br"].notes))
check("(b) and then bytes actually cross to the host",
      len(r["at_host"]) > 4000, "%d octets" % len(r["at_host"]))
for s in (r["cli"], r["host"]):
    try:
        s.close()
    except OSError:
        pass

r = soft_call(org_ec=True, frames=1800, hit=(8.0, 1.5, 0.9))
check("(c) a retrain happened", (r["ans"].retrains + r["org"].retrains) >= 1,
      "%d + %d" % (r["ans"].retrains, r["org"].retrains))
check("(c) the byte stream is still exactly intact across it",
      r["at_host"] == r["to_line"][:len(r["at_host"])] and len(r["at_host"]) > 2000,
      "%d octets" % len(r["at_host"]))
check("(c) and nothing was injected or dropped by the bridge",
      r["br"].stats()["discarded"] == 0)
for s in (r["cli"], r["host"]):
    try:
        s.close()
    except OSError:
        pass

print()
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
print("all terminal server tests passed")
