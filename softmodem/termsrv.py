"""A modem terminal server: inbound calls, routed by number, bridged to TCP.

Registers over SIP, waits for INVITEs, looks the dialled number up in a rule
file, opens a TCP connection to whatever it finds there, and runs a V.32bis data
phase as a raw byte pipe between the caller and that socket. Call after call,
until stopped.

    python3 termsrv.py --routes ../testrig/routes.example

A raw byte pipe means exactly that: no AT commands, no DTR/DSR or RTS/CTS, no
CONNECT or NO CARRIER text. What the caller types reaches the host and what the
host writes reaches the caller, and nothing else passes in either direction.

Three things about the sequence are deliberate and were chosen with the user.

**Both numbers are matched.** The user part of the Request-URI and of the To
header are both offered to the rule list, and the list is walked once with each
rule tried against both -- so an earlier rule matching the To number beats a
later rule matching the Request-URI. Nesting it the other way gives first-number
wins, which quietly reorders the routing table.

**The TCP connection is opened before the 200 OK.** A refused or unreachable
backend then declines the call with a SIP error instead of answering into a dead
pipe and dropping the caller ten seconds later in the middle of a handshake. The
100 Trying goes out first, because the connect can take five seconds and a PBX
starts retransmitting the INVITE at 500 ms.

**A number with no rule is rejected with 404**, not answered. This is a terminal
server; if it is not in the table, it is not ours.

The daemon survives its calls. Every call runs inside try/except, because a
modem does not go away because one call went wrong -- the comment at modem.py's
serve() records what happened the one time that was not true.
"""
import argparse
import re
import socket
import sys
import time
import traceback

import g711
import modem
import rtp
import siproute
import tcpbridge
import v32fsm
from sip_glue import sipmin, raw_recv, resp_for, HOST, USER, PW

REGISTER_EVERY = 240.0          # the FRITZ!Box grants 300; renew well inside it
CONNECT_TIMEOUT = 5.0


def _bye(ua, inv, totag):
    """In-dialog BYE from the answering side, per the pattern in v32answer.py."""
    ct = re.search(r";tag=([^\s;]+)", sipmin.hget(inv, "From") or "")
    cm = re.search(r"<([^>]+)>", sipmin.hget(inv, "Contact") or "")
    b, _, _ = ua.authed("BYE", cm.group(1) if cm else "sip:%s" % HOST,
                        callid=(sipmin.hget(inv, "Call-ID") or "").strip(),
                        fromtag=totag,
                        totag=ct.group(1) if ct else None)
    return sipmin.status(b)[0] if b else None


class Server:
    def __init__(self, a):
        self.a = a
        self.rules = siproute.load_rules(a.routes)
        self.ua = sipmin.UA(HOST, USER, PW)
        self.totag = sipmin.rid(32)
        self.rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rs.bind(("0.0.0.0", 0))
        self.rs.settimeout(0.02)
        self.registered = 0.0
        self.calls = 0

    # -- registration ----------------------------------------------------

    def register(self):
        r, _, _ = self.ua.authed("REGISTER", "sip:%s" % HOST,
                                 extra=("Expires: 300",))
        code = sipmin.status(r)[0] if r else None
        self.registered = time.time()
        print("REGISTER -> %s  (contact %s:%d)"
              % (code, self.ua.lip, self.ua.lport), flush=True)
        return code == 200

    # -- the idle loop ---------------------------------------------------

    def serve(self):
        if not self.register():
            return 1
        print("%d rule(s) from %s; waiting for calls"
              % (len(self.rules), self.a.routes), flush=True)
        while True:
            if time.time() - self.registered > REGISTER_EVERY:
                self.register()
            if self.a.calls and self.calls >= self.a.calls:
                print("handled %d call(s), stopping" % self.calls, flush=True)
                return 0
            msg, _ = raw_recv(self.ua, 0.25)
            if not msg:
                continue
            if msg.startswith("OPTIONS "):
                self.ua.send(resp_for(msg, 200, "OK", self.ua, self.totag))
                continue
            if not msg.startswith("INVITE "):
                continue
            try:
                self.call(msg)
            except Exception:
                print("  *** call failed ***", flush=True)
                traceback.print_exc()
            self.calls += 1

    # -- one call --------------------------------------------------------

    def call(self, inv):
        a = self.a
        ua = self.ua
        if a.dump_invite:
            print("--- INVITE ---\n%s\n--- end ---"
                  % inv.split("\r\n\r\n")[0], flush=True)
        nums = siproute.numbers_of(inv)
        rule, num = siproute.route(self.rules, nums)
        frm = (sipmin.hget(inv, "From") or "").strip()
        print("\nINVITE for %s from %s" % ("/".join(nums) or "?", frm), flush=True)
        if rule is None:
            ua.send(resp_for(inv, 404, "Not Found", ua, self.totag))
            print("  no rule matches -> 404", flush=True)
            return

        print("  %s matches %s -> %s:%d"
              % (num, rule.rx.pattern, rule.host, rule.port), flush=True)
        # Trying first: the connect below may take CONNECT_TIMEOUT, and a PBX
        # retransmits the INVITE at 500 ms if it has heard nothing at all.
        ua.send(resp_for(inv, 100, "Trying", ua, self.totag))
        try:
            sock = socket.create_connection((rule.host, rule.port),
                                            timeout=CONNECT_TIMEOUT)
        except OSError as e:
            ua.send(resp_for(inv, 503, "Service Unavailable", ua, self.totag))
            print("  connect failed (%s) -> 503" % e, flush=True)
            return
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass          # a terminal server must not let Nagle clump keystrokes
        print("  connected", flush=True)

        rip, rpt, pts = modem.parse_sdp(inv)
        pt = 8 if "8" in pts else 0
        rport = self.rs.getsockname()[1]
        ua.send(resp_for(inv, 200, "OK", ua, self.totag,
                         modem.sdp_for(ua.lip, rport, pt)))

        m = v32fsm.AnswerStartup(
            level_dbfs=a.level, ans_s=a.ans, rates=tuple(
                int(v) for v in a.rates.split(",")),
            bis=True, trellis=a.trellis, ec=True,
            cancel_echo=not a.no_echo, allow_14400=a.allow_14400)
        br = tcpbridge.Bridge(m, sock, feed=a.feed, lapm_hi=a.lapm_hi,
                              idle_max=a.idle_max, t0=time.time(),
                              log=lambda s: print("  %s" % s, flush=True))
        seen = [0]
        callid = (sipmin.hget(inv, "Call-ID") or "").strip()
        far_bye = [False]

        def on_frame(inbound):
            now = time.time()
            if inbound is not None:
                br.last_rtp = now
            x = g711.decode(inbound, pt) if inbound else []
            out = m.step(x)
            br.frame(now)
            if len(m.events) > seen[0]:
                for t, st, msg in m.events[seen[0]:]:
                    print("  [%7.3f] %-7s %s" % (t, st, msg), flush=True)
                seen[0] = len(m.events)
            return g711.encode(out[:160], pt)

        def on_sip():
            ua.sock.settimeout(0.001)
            try:
                t = ua.sock.recvfrom(65535)[0].decode("utf-8", "replace")
            except Exception:
                return False
            if t.startswith("BYE "):
                ua.send(resp_for(t, 200, "OK", ua, self.totag))
                far_bye[0] = True
                print("  caller sent BYE", flush=True)
                return True
            if t.startswith("OPTIONS "):
                ua.send(resp_for(t, 200, "OK", ua, self.totag))
            elif t.startswith("INVITE "):
                # A re-INVITE on this dialog is a hold or a codec change and must
                # be answered, or the PBX tears the call down. A new dialog is
                # someone else calling while we are busy; saying so is one line
                # and saves the switch seven retransmissions and a failure.
                same = (sipmin.hget(t, "Call-ID") or "").strip() == callid
                if same:
                    ua.send(resp_for(t, 200, "OK", ua, self.totag,
                                     modem.sdp_for(ua.lip, rport, pt)))
                else:
                    ua.send(resp_for(t, 486, "Busy Here", ua, self.totag))
            return False

        def on_stop():
            return br.stop_reason(time.time())

        # capture=False: the default keeps every inbound and outbound payload in
        # memory for the life of the call -- 16 kB/s, and these calls are meant
        # to last. watchdog off: an unrequested outbound frame is twelve symbols
        # the far receiver never asked for.
        st = rtp.pump(self.rs, (rip, rpt), pt, a.max_call, on_frame,
                      on_sip=on_sip, on_stop=on_stop, frame_bytes=160,
                      watchdog=1e9, capture=False)
        s = br.stats()
        print("  ended: %s after %.1f s | rate %s %s | caller->host %d, "
              "host->caller %d octets%s"
              % (st.get("stopped") or "time limit", st["dur"], m.rate,
                 # ec_was_up, not ec.up: the link is normally already
                 # down by the time we print this, and reporting the last
                 # instant rather than the call is how a V.42 session that
                 # carried 100.0000% gets logged as V.14.
                 "V.42/LAPM" if br.ec_was_up else "V.14",
                 s["tcp_in"], s["tcp_out"],
                 ", %d discarded" % s["discarded"] if s["discarded"] else ""),
              flush=True)
        br.close()
        if not far_bye[0]:
            print("  BYE -> %s" % _bye(ua, inv, self.totag), flush=True)


def main():
    ap = argparse.ArgumentParser(
        description="Answer modem calls and bridge them to TCP.")
    ap.add_argument("--routes", required=True,
                    help="rule file: whitespace-separated regex, host, port")
    ap.add_argument("--level", type=float, default=-18.0)
    ap.add_argument("--ans", type=float, default=3.3,
                    help="seconds of ANS before the conditioning signal")
    ap.add_argument("--rates", default="4800,7200,9600,12000",
                    help="the rates our R1 offers")
    ap.add_argument("--allow-14400", action="store_true",
                    help="offer 14400 as well. It negotiates and then fails, "
                         "and the 12000 it demotes to carries 1.98%% where a "
                         "12000 dialled directly carries 100.0000%%")
    ap.add_argument("--no-echo", action="store_true",
                    help="turn the echo canceller off. It is on by default "
                         "because that is the configuration every rig result "
                         "in testrig/ was measured with")
    ap.add_argument("--trellis", action="store_true",
                    help="also offer V.32 trellis coding in R1. Off by default, "
                         "matching orch_throughput.py: --bis already carries "
                         "the trellis-coded bis rates, and offering both made "
                         "the Cirrus answer R2 sixteen seconds late and then "
                         "never send E at all")
    ap.add_argument("--feed", type=int, default=tcpbridge.FEED_MAX,
                    help="octets offered to the line per 20 ms frame; the "
                         "queue depth is what really governs")
    ap.add_argument("--lapm-hi", type=int, default=tcpbridge.LAPM_OUTQ_HI,
                    help="LAPM transmit queue depth to stop reading TCP at. "
                         "Measured free to lower: throughput is identical from "
                         "256 to 4096 because the window binds first, and a "
                         "lower value means less committed output a caller has "
                         "to sit through after the host goes quiet")
    ap.add_argument("--idle-max", type=float, default=tcpbridge.IDLE_MAX,
                    help="hang up after this long with no byte moving either "
                         "way; 0 disables")
    ap.add_argument("--max-call", type=float, default=tcpbridge.MAX_CALL)
    ap.add_argument("--dump-invite", action="store_true",
                    help="print the whole INVITE before routing it. The number "
                         "a PBX puts in the Request-URI is not always the one "
                         "that was dialled -- a FRITZ!Box addresses it to the "
                         "registered account name -- so this is how you find "
                         "out what your switch actually offers to match on")
    ap.add_argument("--calls", type=int, default=0,
                    help="stop after this many calls; 0 serves forever")
    a = ap.parse_args()
    try:
        return Server(a).serve()
    except siproute.RuleError as e:
        raise SystemExit("%s" % e)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
