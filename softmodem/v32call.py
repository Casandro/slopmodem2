"""Originate a V.32 call to a hardware modem and run our 5.4 start-up at it.

Everything in this project has been measured the other way round: a modem dials
`**620` and we answer. That fixes the modem in the *calling* role, and the bridge
captures say the calling modem transmits a good deal worse than the answering one
at 14 400 -- 6 to 9% of amplitude against 1 to 2.5%, same modem, same evening,
against a decision margin of 11.0%. If that is real rather than an artefact of
three sessions read together, then originating from our side should put the modem
in its better role and 14 400 should hold.

Same body as v32answer.py: the FSM is driven frame by frame from the RTP
callback. What changes is the SIP leg, which is placed rather than received, and
the state machine, which is OriginateStartup -- so we send the 5.4 caller's side,
watch for the answer tone, and scramble with GPC while listening for GPA.

  python3 v32call.py --number '**1' --bis --rates 4800,7200,9600,12000,14400 \
                     --ec --feed 64 --echo --level -18 --seconds 95
"""
import argparse, math, re, socket, sys, time
import dsp, g711, rtp, v32, v32fsm, v42
from sip_glue import sipmin, raw_recv, resp_for, HOST, USER, PW
import modem


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--number", default="**1",
                    help="extension to call; **1 is the Cirrus, **2 the Conexant")
    ap.add_argument("--seconds", type=float, default=95.0)
    ap.add_argument("--level", type=float, default=-18.0)
    ap.add_argument("--trn", type=int, default=1280)
    ap.add_argument("--ans-hold", type=float, default=1.0)
    ap.add_argument("--trellis", action="store_true")
    ap.add_argument("--rates", default="4800,9600")
    ap.add_argument("--bis", action="store_true")
    ap.add_argument("--send", default="")
    ap.add_argument("--ec", action="store_true")
    ap.add_argument("--feed", type=int, default=4)
    ap.add_argument("--echo", action="store_true")
    ap.add_argument("--echo-budget", type=int, default=None)
    ap.add_argument("--out", default="ref/v32call_rx.raw")
    ap.add_argument("--tx-out", default="ref/v32call_tx.raw")
    a = ap.parse_args()
    rates = tuple(int(v) for v in a.rates.split(","))

    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s" % (sipmin.status(r)[0] if r else None), flush=True)
    rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rs.bind(("0.0.0.0", 0))
    rs.settimeout(0.02)

    ruri = "sip:%s@%s" % (a.number, HOST)
    print("INVITE %s" % ruri, flush=True)
    rsp, cid, ftag = ua.authed("INVITE", ruri, timeout=45.0,
                               body=modem.sdp_for(ua.lip,
                                                  rs.getsockname()[1], 8))
    code = sipmin.status(rsp)[0] if rsp else None
    print("  -> %s" % code, flush=True)
    if code != 200:
        print("call not answered")
        return 1
    m = re.search(r";tag=([^\s;]+)", sipmin.hget(rsp, "To") or "")
    totag = m.group(1) if m else None
    cm = re.search(r"<([^>]+)>", sipmin.hget(rsp, "Contact") or "")
    target = cm.group(1) if cm else ruri
    rip, rpt, pts = modem.parse_sdp(rsp)
    pt = 8 if "8" in pts else 0
    ua.req("ACK", target, callid=cid, fromtag=ftag, totag=totag)
    print("answered; far RTP %s:%s PT %d" % (rip, rpt, pt), flush=True)

    m = v32fsm.OriginateStartup(level_dbfs=a.level, rates=rates,
                                trellis=a.trellis, trn=a.trn, bis=a.bis,
                                ec=a.ec, cancel_echo=a.echo,
                                ans_hold=a.ans_hold,
                                **({} if a.echo_budget is None
                                   else {"echo_budget": a.echo_budget}))
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
            if pat:
                # Same backpressure as v32answer.py: LAPM's queue when there is
                # an error-correcting entity, and the V.14 converter's own queue
                # paced to the line when there is not, because V.14 has neither
                # a window nor retransmission to push back with.
                if m.ec is not None:
                    want = (a.feed if (m.ec.up
                                       and len(m.ec.link.lapm.outq) < 4096)
                            else 0)
                elif m.want_ec and not m.ec_fell_back:
                    want = min(a.feed, 64 - len(m.ecq))
                else:
                    per_frame = int((m.rate or 4800) / 500.0 * 0.95)
                    want = min(a.feed, max(per_frame, 1),
                               64 - m.enc.pending())
                if want > 0:
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
                print("far end sent BYE", flush=True)
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
        print("            acq_med %.3f (hands over at %.2f)"
              % (getattr(r.rx, "acq_med", -1.0), r.rx.acq_thresh))
        if r.data_syms:
            P = r.mode.points
            d = sorted(min(abs(z - p) for p in P) for z in r.data_syms[-4000:])
            rms = math.sqrt(sum(abs(p) ** 2 for p in P) / len(P))
            print("  data phase: %d symbols, median %.3f = %.1f%% residual, "
                  "within 0.35 of a point %.1f%%"
                  % (len(r.data_syms), d[len(d) // 2],
                     100.0 * d[len(d) // 2] / rms,
                     100.0 * sum(1 for v in d if v < 0.35) / len(d)))
        if m.ec is not None:
            L = m.ec.link.lapm
            dfr = m.ec.link.deframer
            print("  V.42: detection %s, LAPM %s, %d octets in / %d out"
                  % (m.ec.det.result, L.state, len(got), sent[0]))
            print("        HDLC: %d frames, discarded %d short / %d FCS / "
                  "%d aborted / %d oversize"
                  % (dfr.frames, dfr.short, dfr.badfcs, dfr.aborted,
                     dfr.oversize))
            if last[0] > first[0]:
                dt = last[0] - first[0]
                print("        throughput: %d octets in %.1f s = %.0f byte/s "
                      "= %.0f bit/s of %s (%.0f%%)"
                      % (len(got), dt, len(got) / dt, 8 * len(got) / dt,
                         m.rate, 100.0 * 8 * len(got) / dt / (m.rate or 1)))
        elif m.ec_fell_back:
            print("  V.42: the far end did not answer detection; ran on V.14")
        print("  7.2 V.14: %d characters recovered from the far end" % len(got))
        if m.echo is not None:
            print("  %s" % m.echo.state())
    lin = g711.decode(st["in_audio"], pt)
    if lin:
        rms = dsp.mean_square(lin) ** 0.5
        print("  received %.1f s at %.1f dBFS"
              % (len(lin) / 8000.0, 20 * math.log10(max(rms, 1) / 32768.0)))
    if st["stopped"] != "sip":
        b, _, _ = ua.authed("BYE", target, callid=cid, fromtag=ftag,
                            totag=totag)
        print("  BYE -> %s" % (sipmin.status(b)[0] if b else "no reply"))
    rs.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
