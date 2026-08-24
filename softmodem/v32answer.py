"""Answer a call from a hardware modem and run our V.32 §5.4 start-up at it.

Everything in v32fsm.py so far has been checked soft-to-soft, which proves the
two state machines agree with each other and with the Recommendation as we read
it. It does not prove either agrees with a modem. This is the first attempt at
that: register as **620, answer, and drive AnswerStartup frame by frame in the
RTP callback.

Both modems have to be pushed off automode first, or they will pick V.34 or V.90
and never get to V.32 at all -- see the orchestrator.

  python3 v32answer.py --seconds 45 --trellis
"""
import argparse, math, socket, sys, time
import dsp, g711, rtp, v32, v32fsm, v42
from sip_glue import sipmin, raw_recv, resp_for, HOST, USER, PW
import modem

# Characters we are willing to have queued ahead of the line when there is no
# error-correcting entity to push back for us. One 20 ms frame is 24 characters
# at 12000 bit/s, so this is a few frames of margin -- enough that the line never
# idles, and well under dte.AsyncEncoder's hiwater of 128, past which V.14 starts
# deleting every stop bit it sees.
V14_AHEAD = 64

# ... and how much of the line's own character budget to use. A bound on the
# queue alone is not enough: it still lets us offer exactly the line rate, and
# V.14 at exactly the line rate has no margin for the two clocks to differ, which
# is what AsyncEncoder's docstring means by the slips coming back from the
# Conexant. Feeding at 95% leaves the slack V.14 needs and costs 5% of a
# direction that has no error correction to protect it anyway.
V14_MARGIN = 0.95


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--wait", type=float, default=90.0)
    ap.add_argument("--level", type=float, default=-24.0)
    ap.add_argument("--ans", type=float, default=3.3,
                    help="V.25 answer sequence, seconds (4.3: 3.3 +/- 0.7)")
    ap.add_argument("--trn", type=int, default=1280,
                    help="TRN symbols; 5.2.3 makes 1280 the minimum and permits "
                         "extending it")
    ap.add_argument("--trellis", action="store_true",
                    help="advertise trellis coding in B8")
    ap.add_argument("--rates", default="4800,9600",
                    help="the rates our R1 offers. 5.4.2 selects "
                         "max(offered & ours), so the default caps the link at "
                         "9600 however much the far end offers; --bis without "
                         "this is still a 9600 link")
    ap.add_argument("--regain", type=int, default=None,
                    help="frames of shut eye before re-measuring the gain; "
                         "0 disables it")
    ap.add_argument("--bis", action="store_true",
                    help="offer V.32bis: B4 and B8 both set, and 7200, 12000 "
                         "and 14400 become available")
    ap.add_argument("--send", default="",
                    help="characters to send once the data phase opens, "
                         "repeated (7.1.2 via the V.14 converter)")
    ap.add_argument("--ec", action="store_true",
                    help="V.42: run the 7.2.1 detection phase when the data "
                         "phase opens, then LAPM, instead of V.14")
    ap.add_argument("--feed", type=int, default=4,
                    help="bytes handed to the DTE side per 20 ms frame; 4 is "
                         "200 byte/s, well under any of the line rates, so "
                         "raise it to measure throughput rather than latency")
    ap.add_argument("--echo-budget", type=int, default=None,
                    help="lags scanned per frame; 0 keeps the canceller in the "
                         "path but does no searching, which isolates its cost")
    ap.add_argument("--echo", action="store_true",
                    help="cancel our own echo out of the receive path")
    ap.add_argument("--xid-no-opt", action="store_true",
                    help="omit PI 3 from the XID response")
    ap.add_argument("--xid-probe", action="store_true",
                    help="diagnostic: answer each retransmitted XID command "
                         "with a different variant, to find which one a far "
                         "end will accept")
    ap.add_argument("--xid-reps", type=int, default=1,
                    help="diagnostic: repeat the XID response this many times")
    ap.add_argument("--bits-out", default="ref/v32if_bits.txt")
    ap.add_argument("--out", default="ref/v32if_rx.raw")
    ap.add_argument("--tx-out", default="ref/v32if_tx.raw")
    a = ap.parse_args()
    rates = tuple(int(v) for v in a.rates.split(","))

    if a.regain is not None:
        v32fsm.REGAIN_EVERY = a.regain
    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s" % (sipmin.status(r)[0] if r else None), flush=True)
    rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rs.bind(("0.0.0.0", 0))
    rs.settimeout(0.02)
    totag = sipmin.rid(32)

    print("waiting up to %.0fs for an INVITE ..." % a.wait, flush=True)
    inv = None
    end = time.time() + a.wait
    while time.time() < end:
        msg, _ = raw_recv(ua, max(0.5, end - time.time()))
        if msg and msg.startswith("INVITE "):
            inv = msg
            break
        if msg and msg.startswith("OPTIONS "):
            ua.send(resp_for(msg, 200, "OK", ua, totag))
    if inv is None:
        print("no INVITE")
        return 1
    rip, rpt, pts = modem.parse_sdp(inv)
    pt = 8 if "8" in pts else 0
    ua.send(resp_for(inv, 100, "Trying", ua, totag))
    ua.send(resp_for(inv, 200, "OK", ua, totag,
                     modem.sdp_for(ua.lip, rs.getsockname()[1], pt)))
    print("answered; caller RTP %s:%s PT %d" % (rip, rpt, pt), flush=True)

    m = v32fsm.AnswerStartup(level_dbfs=a.level, ans_s=a.ans, rates=rates,
                             trellis=a.trellis, trn=a.trn, bis=a.bis,
                             ec=a.ec, cancel_echo=a.echo,
                             **({} if a.echo_budget is None
                                else {"echo_budget": a.echo_budget}))
    m.xid_reps = a.xid_reps
    m.xid_opt_pi = not a.xid_no_opt
    if a.xid_probe:
        # One call, several hypotheses. The modem retransmits its XID about once
        # a second, so each retry can be answered differently and whichever
        # variant is in flight when SABME appears is the one it accepts.
        def echo_len(info):
            """Reply with PI 3 the length *they* sent, not Note 1's 4 octets."""
            out, _ = v42.xid_response(info)
            sub = v42.parse_xid(info)[v42.GI_PARAM]
            n = len(sub.get(v42.PI_HDLC_OPT, b"\0" * 4))
            mine = v42.parse_xid(out)[v42.GI_PARAM]
            mine[v42.PI_HDLC_OPT] = v42.opt_mask((), response=True)[:n]
            return v42.xid_info(mine)

        def no_opt(info):
            """Omit PI 3 entirely: 12.2.2 says unrecognised fields are ignored
            and 9.2.3 makes absence mean the default."""
            mine = v42.parse_xid(v42.xid_response(info)[0])[v42.GI_PARAM]
            del mine[v42.PI_HDLC_OPT]
            return v42.xid_info(mine)

        def bare(info):
            """An empty parameter-negotiation subfield: everything default."""
            return v42.xid_info({})

        def spec(info):
            return v42.xid_response(info)[0]

        m.xid_variants = [
            ("spec, C/R=1 (Table 6)", None, spec),
            ("C/R=0", 0, spec),
            ("PI 3 echoed at their length", None, echo_len),
            ("PI 3 omitted", None, no_opt),
            ("empty subfield", None, bare),
            ("C/R=0 and their PI 3 length", 0, echo_len),
        ]
    seen = 0
    t0 = time.time()
    got = bytearray()
    pat = a.send.encode("ascii", "replace") if a.send else b""
    sent = [0]
    first = [0.0]
    last = [0.0]

    def on_frame(inbound):
        nonlocal seen
        x = g711.decode(inbound, pt) if inbound else []
        out = m.step(x)
        if m.state == v32fsm.DATA:
            # 7.1.2 out, 7.2 back: characters, not raw bits
            if pat:
                # roll through the pattern properly: slicing pat[i:][:4] drops
                # the wrap-around and put "OPMODEM" gaps in the output, which
                # looked like the modem losing characters and was this
                # Backpressure, so a feed rate above the line rate measures
                # what the line carries instead of how much RAM we have.
                #
                # Every path needs its own, because the thing that provides it
                # differs. With LAPM up the transmit queue does it. Without
                # error correction there is nothing at all downstream: V.14 has
                # no window and no retransmission, so an unbounded feed fills
                # RAM, and once the converter's queue passes its hiwater every
                # stop bit starts being deleted -- a stream the far framer
                # cannot acquire on. That read as 27.9% printable and looked
                # like a line fault. The bound is V14_AHEAD, comfortably under
                # dte.AsyncEncoder's hiwater so deletion stays the exception
                # V.14 intends it to be, and still several frames' worth so the
                # line never goes idle for want of data.
                if m.ec is not None:
                    want = (a.feed if (m.ec.up
                                       and len(m.ec.link.lapm.outq) < 4096)
                            else 0)
                elif m.want_ec and not m.ec_fell_back:
                    # Detection has not finished. put() buffers into ecq, which
                    # is handed to the V.14 converter in one go if detection
                    # fails -- so bound it here too, or the fallback begins with
                    # a burst it will spend the next second deleting.
                    want = min(a.feed, V14_AHEAD - len(m.ecq))
                else:
                    # V.14: 10 bits on the line per 8-bit character, so the
                    # line's budget is rate/10 characters a second and rate/500
                    # per 20 ms frame. Pace to that, keep some back, and still
                    # bound the queue.
                    per_frame = int((m.rate or 4800) / 500.0 * V14_MARGIN)
                    want = min(a.feed, max(per_frame, 1),
                               V14_AHEAD - m.enc.pending())
                if want > 0:
                    # Repeat the pattern enough times to actually fill the feed.
                    # `pat + pat` only ever yields len(pat) octets or fewer, so
                    # with the 9-octet SLOPMODEM this capped every frame at 18
                    # regardless of --feed: 900 byte/s, 7200 bit/s, and the same
                    # number at 9600 and at 12000 because it was never the line.
                    i = sent[0] % len(pat)
                    reps = want // len(pat) + 2
                    chunk = (pat * reps)[i:i + want]
                    m.put(chunk)
                    sent[0] += len(chunk)
            new = m.received()
            if new and not got:
                first[0] = time.time()
            got.extend(new)
            if new:
                last[0] = time.time()
        n = len(m.events)
        if n > seen:
            for t, st, msg in m.events[seen:]:
                print("  [%6.3f] %-7s %s" % (time.time() - t0, st, msg),
                      flush=True)
            seen = n
        return g711.encode(out[:160], pt)

    def on_sip():
        ua.sock.settimeout(0.001)
        try:
            t = ua.sock.recvfrom(65535)[0].decode("utf-8", "replace")
            if t.startswith("BYE "):
                ua.send(resp_for(t, 200, "OK", ua, totag))
                print("caller sent BYE", flush=True)
                return True
        except Exception:
            pass
        return False

    st = rtp.pump(rs, (rip, rpt), pt, a.seconds, on_frame, on_sip=on_sip,
                  frame_bytes=160, watchdog=1e9)
    rtp.report(st)
    open(a.out, "wb").write(bytes(st["in_audio"]))
    open(a.tx_out, "wb").write(bytes(st["out_audio"]))

    print()
    print("  final state %s, rate %s, trellis %s, 107 %s, 109 %s"
          % (m.state, m.rate, m.trellis, m.c107, m.c109), flush=True)
    if m.rx is not None:
        r = m.rx
        print("  receiver: dd %s, retrains %d, clamps %d, %d symbols, mode %s"
              % (r.rx.dd, r.rx.retrains, r.rx.clamps, r.rx.nsym, r.mode.name))
        print("            eye gate opened %d times, gain re-measured %d, "
              "%d data bits per symbol" % (r.gates, r.regains, m._data_bps()))
        print("            acq_med %.3f (hands over to dd at %.2f), carrier "
              "%.2f Hz" % (getattr(r.rx, "acq_med", -1.0), r.rx.acq_thresh,
                           r.rx.c_freq * v32.BAUD / (2 * math.pi)))
        if r.data_syms:
            P = r.mode.points
            d = sorted(min(abs(z - p) for p in P) for z in r.data_syms[-4000:])
            print("  data phase: %d symbols, median distance %.3f, 90th pct "
                  "%.3f, within 0.35 of a point %.1f%%"
                  % (len(r.data_syms), d[len(d) // 2], d[int(0.9 * len(d))],
                     100.0 * sum(1 for v in d if v < 0.35) / len(d)))
        if r.data_bits:
            b = r.data_bits
            tail = b[-20000:]
            print("  data bits: %d, ones %.2f%% over the last %d"
                  % (len(b), 100.0 * sum(tail) / len(tail), len(tail)))
            print("  (the far end sends scrambled binary ones during 5.4's B1;"
                  " after that it sends whatever its DTE gives it)")
            with open(a.bits_out, "w") as f:
                f.write("".join(str(v) for v in b))
            print("  bits -> %s" % a.bits_out)
        if m.ec is not None or m.ec_fell_back:
            print()
            if m.ec is None:
                print("  V.42: the far end did not answer the detection phase; "
                      "the call ran on V.14")
            else:
                L = m.ec.link.lapm
                d = m.ec.link.deframer
                print("  V.42: detection %s, LAPM %s, %d octets in / %d out"
                      % (m.ec.det.result, L.state, len(got), sent[0]))
                print("        HDLC: %d frames, discarded %d short / %d FCS / "
                      "%d aborted / %d oversize"
                      % (d.frames, d.short, d.badfcs, d.aborted, d.oversize))
                print("        V(S) %d V(A) %d V(R) %d, %d outstanding, "
                      "%d resends, %d T401 expiries"
                      % (L.vs, L.va, L.vr, len(L.sent), L.stats["resend"],
                         L.stats["t401"]))
                print("        frames received: %s"
                      % " ".join("%s=%d" % (k[3:], v)
                                 for k, v in sorted(L.stats.items())
                                 if k.startswith("rx ")))
                print("        frames sent (by control octet): %s"
                      % " ".join("%s=%d" % (k, v)
                                 for k, v in sorted(L.stats.items())
                                 if not k.startswith("rx ")
                                 and k not in ("resend", "t401", "undefined")))
                tl = m.ec.link.txlog
                print("        %d frames handed to the framer; first few: %s"
                      % (len(tl), ", ".join("%.2fs addr %02X ctl %s +%dB"
                                            % r for r in tl[:6])))
                print("        XID negotiated: %s" % (L.xid,))
                rl = m.ec.link.rxlog
                print("        received: %s"
                      % ", ".join("%.2fs %s" % r for r in rl[:14]))
                tl2 = [r for r in m.ec.link.txlog if r[2] == "af"]
                print("        our XID responses: %s"
                      % ", ".join("%.2fs" % r[0] for r in tl2[:14]))
                if L.xid_tried:
                    print("        XID variants tried: %s"
                          % ", ".join("%.2fs %s" % r for r in L.xid_tried))
                if last[0] > first[0]:
                    dt = last[0] - first[0]
                    print("        throughput: %d octets in %.1f s = "
                          "%.0f byte/s = %.0f bit/s of a %s bit/s channel "
                          "(%.0f%%)"
                          % (len(got), dt, len(got) / dt, 8 * len(got) / dt,
                             m.rate, 100.0 * 8 * len(got) / dt / (m.rate or 1)))
        if m.echo is not None:
            print()
            print("  %s" % m.echo.state())
            # Every scan's peak and lag, in the canceller's own index space --
            # which, unlike the capture files, is not skewed by the pump's
            # priming frames. This is the measurement of the echo itself.
            print("  echo scans (rho@lag): %s"
                  % ", ".join("%.3f@%s" % r for r in m.echo.scan_log))
        print()
        print("  7.2 V.14: %d characters recovered from the far end's DTE"
              % len(got))
        if got:
            txt = "".join(chr(c) if 32 <= c < 127 else "." for c in got)
            print("     %.1f%% printable, framer: %d locks, %d good, %d bad, "
                  "%d stop bits restored"
                  % (100.0 * sum(1 for c in got if 32 <= c < 127) / len(got),
                     r.framer.locks, r.framer.good, r.framer.bad,
                     r.framer.restored))
            for k in range(0, min(len(txt), 240), 80):
                print("     %s" % txt[k:k + 80])
        if pat and m.ec is not None:
            print("  7.3: %d octets handed to the error-correcting entity "
                  "(%d still queued)" % (sent[0], len(m.ec.link.lapm.outq)))
        elif pat and r.data_bits:
            print("  7.1.2: %d characters handed to the V.14 converter "
                  "(%d framed, %d stop bits deleted)"
                  % (sent[0], m.enc.chars, m.enc.deleted))
            # V.14 framing on the descrambled stream, and what it spells
            i, ch, rej = 0, [], 0
            while i < len(b) - 10:
                if b[i] == 1:
                    i += 1
                    continue
                if b[i + 9] != 1:
                    rej += 1
                    i += 1
                    continue
                ch.append(sum(b[i + 1 + k] << k for k in range(8)))
                i += 10
            txt = "".join(chr(c) if 32 <= c < 127 else "." for c in ch)
            print("  V.14 framing: %d characters, %d rejects, %.1f%% printable"
                  % (len(ch), rej,
                     100.0 * sum(1 for c in ch if 32 <= c < 127)
                     / max(len(ch), 1)))
            for k in range(0, min(len(txt), 300), 100):
                print("     %s" % txt[k:k + 100])
    lin = g711.decode(st["in_audio"], pt)
    if lin:
        rms = dsp.mean_square(lin) ** 0.5
        print("  received %.1f s at %.1f dBFS"
              % (len(lin) / 8000.0, 20 * math.log10(max(rms, 1) / 32768.0)))
    if st["stopped"] != "sip":
        # in-dialog BYE from the answering side, per the hard-won pattern in
        # bridge.py: the caller's Contact is the target, and the tags are ours
        # and theirs the way the dialog was set up
        import re
        ct = re.search(r";tag=([^\s;]+)", sipmin.hget(inv, "From") or "")
        cm = re.search(r"<([^>]+)>", sipmin.hget(inv, "Contact") or "")
        b, _, _ = ua.authed("BYE", cm.group(1) if cm else "sip:%s" % HOST,
                            callid=(sipmin.hget(inv, "Call-ID") or "").strip(),
                            fromtag=totag,
                            totag=ct.group(1) if ct else None)
        print("  BYE -> %s" % (sipmin.status(b)[0] if b else "no reply"))
    rs.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
