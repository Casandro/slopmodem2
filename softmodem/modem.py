"""One modem. It answers calls and it places them, whichever the DTE asks for.

Until now the two roles were two programs, `run_answer.py` and `run_call.py`,
with the V.22bis data call written out twice. A modem is not two programs: it is
one device sitting on a line, and which end starts the call is the DTE's
business, not the implementation's. So this registers once, opens one
pseudo-terminal, and then waits for whichever comes first --

    ATD from the DTE   -> originate (fsm.OriginateV22bis, 6.3.1.1.1)
    INVITE from the PBX -> RING, then answer (fsm.AnswerV22bis, 6.3.1.1.2)

-- runs the call, hangs up, and goes back to waiting. Several calls in a session,
in either direction, which is the thing neither of the old programs could do.

The role difference is down to two class attributes on the state machines,
`rx_carrier` and `rx_open`, so `_data_call` below never asks which role it is
driving. That is the whole of the unification: one call runner, one on_frame, one
report.

  python3 modem.py --dte                         # a modem; the DTE drives it
  python3 modem.py --calls 1 --payload 'A '      # answer one call, fixed data
  python3 modem.py --dial '**1' --payload 'A '   # place one call, fixed data
"""
import argparse, random, re, socket, sys, time
import dte, fsm, g711, rtp, tracking, v22
from sip_glue import sipmin, raw_recv, resp_for, HOST, USER, PW

REGISTER_EVERY = 240.0          # we ask for 300 s; renew before it lapses
REGISTER_RETRY = 30.0           # ...and back off rather than hammer on failure


def sdp_for(lip, rport, pt):
    name = "PCMA" if pt == 8 else ("PCMU" if pt == 0 else "G726-32")
    return ("v=0\r\no=- %d 1 IN IP4 %s\r\ns=-\r\nc=IN IP4 %s\r\nt=0 0\r\n"
            % (random.getrandbits(30), lip, lip) +
            "m=audio %d RTP/AVP %d\r\na=rtpmap:%d %s/8000\r\na=sendrecv\r\n"
            % (rport, pt, pt, name))


def parse_sdp(msg):
    body = msg.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in msg else ""
    ip = re.search(r"c=IN IP4 ([\d.]+)", body)
    m = re.search(r"m=audio (\d+) RTP/AVP ([\d ]+)", body)
    return (ip.group(1) if ip else None,
            int(m.group(1)) if m else None,
            m.group(2).split() if m else [])


class Modem:
    def __init__(self, a):
        self.a = a
        self.ua = sipmin.UA(HOST, USER, PW)
        self.totag = sipmin.rid(32)
        self.next_register = 0.0
        self.rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rs.bind(("0.0.0.0", 0))
        self.rs.settimeout(0.25)
        self.rport = self.rs.getsockname()[1]
        self.dte = None
        if a.dte:
            self.dte = dte.Dte(idle=a.idle, s0=a.s0, can_dial=True)
            print("  DTE interface on %s  (screen %s 115200, or minicom -D %s)"
                  % (self.dte.name, self.dte.name, self.dte.name), flush=True)
        self.calls = 0
        self.results = []

    # -- registration ----------------------------------------------------

    def register(self):
        r, _, _ = self.ua.authed("REGISTER", "sip:%s" % HOST,
                                 extra=("Expires: 300",))
        code = sipmin.status(r)[0] if r else None
        print("REGISTER -> %s  (contact %s:%d)" % (code, self.ua.lip,
                                                   self.ua.lport), flush=True)
        # Schedule the next attempt whether or not this one worked. Keying the
        # timer off the last *success* means a failing registrar gets a REGISTER
        # every idle tick, which is 20 a second.
        self.next_register = time.time() + (REGISTER_EVERY if code == 200
                                            else REGISTER_RETRY)
        return code == 200

    # -- the idle loop ---------------------------------------------------

    def serve(self, seconds, max_calls):
        """Wait for work from either side until the time or the call count runs
        out. This is the part that makes it a modem rather than a script."""
        end = time.time() + seconds
        print("idle: waiting up to %.0fs for ATD or an INVITE (%d call%s)"
              % (seconds, max_calls, "" if max_calls == 1 else "s"), flush=True)
        while time.time() < end and self.calls < max_calls:
            if time.time() >= self.next_register:
                self.register()
            what, payload = self._idle_poll()
            if what is None:
                continue
            # A modem does not go away because one call went wrong. This is not
            # hypothetical: a module-shadowing bug (see sip_glue) took out the
            # second call of a two-call session, and because the exception
            # propagated straight out of serve() the log just said "handled 1
            # call(s)" with the traceback lost in a filtered pipe.
            try:
                if what == "invite":
                    self._answer(payload)
                else:
                    self._originate(payload)
            except Exception:
                import traceback
                print("  *** call failed ***", flush=True)
                traceback.print_exc()
                self.results.append((what, "exception", None, None))
                if self.dte is not None:
                    self.dte.no_carrier()
        return self.results

    def _idle_poll(self, slice_s=0.05):
        """One turn of the idle loop: give the DTE a tick, then look for SIP."""
        if self.dte is not None:
            self.dte.poll()
            if self.dte.want_dial:
                num = self.dte.want_dial
                self.dte.want_dial = None
                return "dial", num
            if self.dte.want_hangup:
                # ATH with no call up is simply OK, which _run_line already sent
                self.dte.want_hangup = False
        msg, _ = raw_recv(self.ua, slice_s)
        if not msg:
            return None, None
        if msg.startswith("INVITE "):
            return "invite", msg
        if msg.startswith("OPTIONS "):
            self.ua.send(resp_for(msg, 200, "OK", self.ua, self.totag))
        return None, None

    # -- answering -------------------------------------------------------

    def _answer(self, inv):
        a = self.a
        print("INVITE from %s" % (sipmin.hget(inv, "From") or "").strip(),
              flush=True)
        rip, rpt, pts = parse_sdp(inv)
        pt = a.pt if (a.pt is not None and str(a.pt) in pts) else (
            8 if "8" in pts else 0)
        self.ua.send(resp_for(inv, 100, "Trying", self.ua, self.totag))

        # V.250: report RING and let S0 or ATA decide. Without a DTE there is
        # nobody to ask, so answer at once.
        d = self.dte
        if d is not None:
            d.ring()
            if a.s0 == 0:
                print("  S0=0: waiting up to %.0f s for ATA" % a.dte_wait,
                      flush=True)
                deadline = time.time() + a.dte_wait
                tick = 0
                while time.time() < deadline and not d.want_answer:
                    d.poll()
                    time.sleep(0.02)
                    tick += 1
                    if tick % 100 == 0:
                        d.ring()
                if not d.want_answer:
                    print("  no ATA - rejecting", flush=True)
                    self.ua.send(resp_for(inv, 486, "Busy Here", self.ua,
                                          self.totag))
                    return
                print("  ATA from the DTE", flush=True)
            else:
                print("  S0=%d: auto-answering" % a.s0, flush=True)
            d.want_answer = False

        self.ua.send(resp_for(inv, 200, "OK", self.ua, self.totag,
                              sdp_for(self.ua.lip, self.rport, pt)))
        print("  200 OK sent; caller RTP %s:%s PT %d" % (rip, rpt, pt),
              flush=True)
        # Enough of the dialog to hang up from this end. sipmin.req always builds
        # From: as our own identity, which is what an in-dialog request from the
        # answering side needs: our tag as From, the caller's as To, and their
        # Contact as the request URI. Without this a local ATH would leave the
        # dialog for the PBX to time out.
        cid = (sipmin.hget(inv, "Call-ID") or "").strip()
        ct = re.search(r";tag=([^\s;]+)", sipmin.hget(inv, "From") or "")
        cm = re.search(r"<([^>]+)>", sipmin.hget(inv, "Contact") or "")
        machine = fsm.AnswerV22bis(level_dbfs=a.level, lead=a.lead,
                                   payload=a.payload.encode(), idle=a.idle,
                                   tx_source=(d.tx.take if d else None),
                                   data_s=max(5.0, a.max_call - 6.0))
        self._data_call(machine, pt, rip, rpt,
                        bye=(cm.group(1) if cm else "sip:%s" % HOST, cid,
                             self.totag, ct.group(1) if ct else None))

    # -- originating -----------------------------------------------------

    def _originate(self, number):
        a = self.a
        ruri = "sip:%s@%s" % (number, HOST)
        print("ATD %s -> INVITE %s" % (number, ruri), flush=True)
        rsp, cid, ftag = self.ua.authed("INVITE", ruri, timeout=40.0,
                                        body=sdp_for(self.ua.lip, self.rport, 8))
        code = sipmin.status(rsp)[0] if rsp else None
        print("  -> %s" % code, flush=True)
        if code != 200:
            if self.dte is not None:
                self.dte.result(dte.NO_CARRIER if code == 486 else dte.NO_ANSWER)
            self.results.append(("originate", number, "sip-%s" % code, None))
            return
        totag = re.search(r";tag=([^\s;]+)", sipmin.hget(rsp, "To") or "")
        totag = totag.group(1) if totag else None
        cm = re.search(r"<([^>]+)>", sipmin.hget(rsp, "Contact") or "")
        target = cm.group(1) if cm else ruri
        rip, rpt, pts = parse_sdp(rsp)
        pt = 8 if "8" in pts else (0 if "0" in pts else int(pts[0]))
        print("  answered; remote RTP %s:%s PT %d" % (rip, rpt, pt), flush=True)
        self.ua.req("ACK", target, callid=cid, fromtag=ftag, totag=totag,
                    cseq=self.ua.cseq)
        machine = fsm.OriginateV22bis(level_dbfs=a.level,
                                      tx_source=(self.dte.tx.take
                                                 if self.dte else None),
                                      payload=a.payload.encode(), idle=a.idle,
                                      usb1_timeout=a.usb1_timeout)
        self._data_call(machine, pt, rip, rpt, bye=(target, cid, ftag, totag))

    # -- the call, for either role ---------------------------------------

    def _data_call(self, machine, pt, rip, rpt, bye=None):
        """Run a V.22bis data call. Role-agnostic: the state machine says which
        channel to listen on and when it is worth listening."""
        a = self.a
        d = self.dte
        # The receiver is built on first use, not up front: which constellation
        # it needs is not known until the handshake has chosen a rate. 6.3.1.2
        # can send either side to 1200 bit/s and a four-point constellation, and
        # rx_open is timed to leave the streaming receiver its 600-half-symbol
        # prologue before data starts either way.
        live = [None]
        seen = [0]
        report = [0]
        far_bye = [False]

        def on_frame(inbound):
            samples = g711.decode(inbound, pt) if inbound else []
            out = machine.step(samples)
            while len(machine.events) > seen[0]:
                t, st, msg = machine.events[seen[0]]
                print("  [%6.3f] %-9s %s" % (t, st, msg), flush=True)
                seen[0] += 1
            if d is not None:
                d.poll()
                if machine.state in (fsm.DATA, fsm.D1200) and not d.connected:
                    d.connect(machine.line_rate)
            if samples and machine.rx_open:
                if live[0] is None:
                    live[0] = tracking.LiveRx(carrier=machine.rx_carrier,
                                              mode=machine.rx_mode)
                    print("  receiver open: %s bit/s, %d bits/symbol"
                          % (machine.rx_mode.name, machine.rx_mode.bps),
                          flush=True)
                got = live[0].feed(samples)
                if d is not None and got:
                    d.deliver(got)
                if live[0].frames - report[0] >= 250:
                    report[0] = live[0].frames
                    q = live[0].summary()
                    print("  rx   %6d chars | %s bit/s, %d sym, acq@%d, "
                          "%d retrain(s), carrier %+.3f Hz | framing %d ok / "
                          "%d err | %.2f ms"
                          % (q["chars"], q["mode"], q["symbols"],
                             q["acquired_at"], q["retrains"], q["carrier_hz"],
                             q["framing_good"], q["framing_bad"],
                             q["mean_ms"]), flush=True)
            return g711.encode(out, pt)

        def on_sip():
            self.ua.sock.settimeout(0.001)
            try:
                t = self.ua.sock.recvfrom(65535)[0].decode("utf-8", "replace")
                if t.startswith("BYE "):
                    self.ua.send(resp_for(t, 200, "OK", self.ua, self.totag))
                    print("  far end sent BYE", flush=True)
                    far_bye[0] = True
                    return True
            except Exception:
                pass
            return False

        def on_stop():
            if d is not None and d.want_hangup:
                return "dte-ath"
            if machine.state == fsm.FAILED:
                return "handshake-failed"
            return None

        # The watchdog must stay off for a modem stream: an extra outbound frame
        # pushes 160 unrequested samples -- 12 symbols -- at the far end.
        st = rtp.pump(self.rs, (rip, rpt), pt, a.max_call, on_frame,
                      on_sip=on_sip, on_stop=on_stop, frame_bytes=160,
                      watchdog=1e9)
        rtp.report(st)
        if d is not None:
            d.want_hangup = False
        if bye is not None and not far_bye[0]:
            target, cid, ftag, totag = bye
            b, _, _ = self.ua.authed("BYE", target, callid=cid, fromtag=ftag,
                                     totag=totag)
            print("  BYE -> %s" % (sipmin.status(b)[0] if b else "no reply"),
                  flush=True)
        self.calls += 1
        self.results.append((machine.role, st.get("stopped"), machine.state,
                             self._report(machine, live[0], st)))
        if d is not None:
            d.no_carrier()
            for _ in range(5):
                d.poll()
                time.sleep(0.02)

    # -- reporting -------------------------------------------------------

    def _report(self, machine, live, st):
        a = self.a
        print("  *** CALL %d (%s) ***" % (self.calls, machine.role))
        print("      final state    : %s ; stopped by %s"
              % (machine.state, st.get("stopped")))
        if live is None:
            print("      the receiver never opened - the handshake did not get "
                  "far enough to choose a rate")
            return None
        q = live.summary()
        print("      line rate      : %s bit/s (%d bits/symbol)"
              % (q["mode"], machine.rx_mode.bps))
        print("      symbols        : %d (%.1f s of line time)"
              % (q["symbols"], q["symbols"] / 600.0))
        print("      acquired at    : symbol %d ; %d retrain(s)"
              % (q["acquired_at"], q["retrains"]))
        print("      carrier offset : %+.3f Hz" % q["carrier_hz"])
        print("      framing        : %d characters, %d framing errors, %d lock(s)"
              % (q["framing_good"], q["framing_bad"], live.dec.framer.locks))
        print("      callback cost  : %.2f ms mean, %.1f ms worst over %d frames"
              % (q["mean_ms"], q["worst_ms"], q["frames"]))
        match = None
        if a.expect:
            import v22bis_track
            txt = bytes(live.data).decode("latin-1", "replace")
            frac, slips, _ = v22bis_track.score_slip(txt, a.expect)
            match = (frac, len(txt), slips)
            print("      MATCH          : %.4f%% of %d characters, %d slip%s"
                  % (100 * frac, len(txt), slips, "" if slips == 1 else "s"))
            pr = "".join(c if 32 <= ord(c) < 127 else "." for c in txt[:72])
            print("      decoded        : %r" % pr)
        for opt, blob, what in ((a.rx_out, bytes(live.data), "characters"),
                                (a.out, bytes(st["in_audio"]), "inbound audio")):
            if not opt:
                continue
            path = opt if self.calls == 1 else "%s.%d" % (opt, self.calls)
            open(path, "wb").write(blob)
            print("      %s -> %s" % (what, path))
        if self.dte is not None:
            qq = self.dte.summary()
            print("      DTE            : %d bytes in / %d out, %d idle mark bits"
                  % (qq["from_dte"], qq["to_dte"], qq["idle_bits"]))
        return match

    def close(self):
        if self.dte is not None:
            self.dte.close()
        self.rs.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dte", action="store_true",
                    help="open a pseudo-terminal and let the DTE drive")
    ap.add_argument("--dial", default=None,
                    help="place one call to this number and exit")
    ap.add_argument("--calls", type=int, default=1,
                    help="how many calls to handle before exiting")
    ap.add_argument("--idle-seconds", type=float, default=120.0,
                    help="how long to wait for work")
    ap.add_argument("--max-call", type=float, default=80.0,
                    help="longest a single call may run")
    ap.add_argument("--level", type=float, default=-18.0)
    ap.add_argument("--lead", type=float, default=0.3)
    ap.add_argument("--idle", type=int, default=2,
                    help="extra mark bits between transmitted characters")
    ap.add_argument("--s0", type=int, default=1,
                    help="S0: rings before auto-answer; 0 waits for ATA")
    ap.add_argument("--dte-wait", type=float, default=25.0)
    ap.add_argument("--payload", default="SOFT2MODEM ",
                    help="sent repeatedly when there is no DTE")
    ap.add_argument("--expect", default=None)
    ap.add_argument("--rx-out", default=None)
    ap.add_argument("--out", default=None, help="save inbound audio here")
    ap.add_argument("--pt", type=int, default=None)
    ap.add_argument("--usb1-timeout", type=float, default=25.0)
    a = ap.parse_args(argv)

    m = Modem(a)
    if not m.register():
        m.close()
        return 1
    try:
        if a.dial:
            # A number on the command line is the same thing as an ATD, so it
            # goes through exactly the same path.
            m._originate(a.dial)
        if m.calls < a.calls:
            m.serve(a.idle_seconds, a.calls)
    finally:
        print("handled %d call(s)" % m.calls, flush=True)
        for row in m.results:
            print("  %s" % (row,))
        m.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
