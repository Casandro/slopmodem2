"""Offline tests for the calling side of V.22bis. No hardware, no SIP.

The centrepiece is a back-to-back run: our originator against our answerer,
frame by frame, each hearing only what the other transmitted. That exercises
6.3.1.1.1 and 6.3.1.1.2 against each other, so a timing error on one side cannot
hide behind a matching error on the other -- and it is checked against the
Recommendation's own tolerances, not against whatever the code happens to do.
"""
import math, random, sys
import ansam, dsp, dte, fsm, tracking, v22, v22bis

FAIL = []


def check(name, cond, detail=""):
    print("  %-56s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def energies(x):
    ms = dsp.mean_square(x)
    if ms <= 0:
        return [0.0] * 4
    return [dsp.goertzel(x, hz) / ms for hz in (2250.0, 2400.0, 2100.0, 2700.0)]


def mean_energies(sig, a=3, b=8):
    rows = [energies(sig[i * 160:(i + 1) * 160]) for i in range(a, b)]
    rows = [r for r in rows if any(r)]
    return [sum(r[k] for r in rows) / max(len(rows), 1) for k in range(4)]


def score(data, expect):
    if not data:
        return 0.0, 0
    n = len(expect)
    ph = max(range(n), key=lambda p: sum(
        1 for i, c in enumerate(data) if c == expect[(i + p) % n]))
    ok = sum(1 for i, c in enumerate(data) if c == expect[(i + ph) % n])
    return ok / float(len(data)), ok


def loopback(frames=1500, echo=0.0, ans_s=0.4, seed=5, v22_only=False):
    """Run originator and answerer against each other. Returns everything."""
    pat_o, pat_a = b"ORIG2ANS  ", b"ANS2ORIG  "
    eo, ea = dte.AsyncEncoder(idle=2), dte.AsyncEncoder(idle=2)
    eo.put(pat_o * 900)
    ea.put(pat_a * 900)
    orig = fsm.OriginateV22bis(level_dbfs=-18.0, tx_source=eo.take,
                               v22_only=v22_only)
    ans = fsm.AnswerV22bis(level_dbfs=-18.0, lead=0.04, ans_s=ans_s,
                           tx_source=ea.take)
    # The receivers are made on first use, not up front: which constellation
    # they need is not known until the handshake has chosen a rate.
    rx = {"o": None, "a": None}
    to_o = to_a = [0] * fsm.FRAME
    silent_before_usb1 = True
    prev_o = prev_a = [0] * fsm.FRAME
    for _ in range(frames):
        out_o = orig.step(to_o)
        out_a = ans.step(to_a)
        if orig.state == fsm.WAITUSB1 and max(abs(v) for v in out_o) > 0:
            silent_before_usb1 = False
        # Each side hears the far end, plus a scaled copy of what it sent last
        # frame if an echo is being simulated. A real two-wire hybrid returns
        # some of your own transmission; the two channels are adjacent in
        # frequency (675-1725 and 1875-2925 Hz at 75% roll-off), so this is a
        # genuine test of the channel separation and not just of the logic.
        if echo:
            to_a = [out_o[i] + int(echo * prev_a[i]) for i in range(fsm.FRAME)]
            to_o = [out_a[i] + int(echo * prev_o[i]) for i in range(fsm.FRAME)]
        else:
            to_a, to_o = out_o, out_a
        prev_o, prev_a = out_o, out_a
        for key, mach, frame in (("o", orig, to_o), ("a", ans, to_a)):
            if not mach.rx_open:
                continue
            if rx[key] is None:
                rx[key] = tracking.LiveRx(carrier=mach.rx_carrier,
                                          mode=mach.rx_mode)
            rx[key].feed(frame)
    return {"orig": orig, "ans": ans, "rx_o": rx["o"], "rx_a": rx["a"],
            "pat_o": pat_o, "pat_a": pat_a, "silent": silent_before_usb1}


def at(events, needle):
    for t, st, m in events:
        if needle in m:
            return t
    return None


if __name__ == "__main__":
    print("high-channel detectors, calibrated not assumed")
    sigs = {}
    m = v22bis.Mod("high", level_dbfs=-18.0)
    sigs["USB1"] = m.modulate([1] * 240, scramble=False, bps=2)
    m = v22bis.Mod("high", level_dbfs=-18.0)
    sigs["S1"] = m.modulate(v22.s1_bits(240), scramble=False, bps=2)
    m = v22bis.Mod("high", level_dbfs=-18.0)
    sigs["SB1"] = m.modulate([1] * 240, scramble=True, bps=2)
    random.seed(3)
    m = v22bis.Mod("high", level_dbfs=-18.0)
    sigs["data"] = m.modulate([random.randint(0, 1) for _ in range(480)],
                              scramble=True)
    sigs["ANS"] = list(ansam.ans_samples(0.2, level_dbfs=-18.0))
    e = {k: mean_energies(v) for k, v in sigs.items()}
    print("    %-6s %7s %7s %7s %7s" % ("", "e2250", "e2400", "e2100", "e2700"))
    for k in ("USB1", "S1", "SB1", "data", "ANS"):
        print("    %-6s %7.3f %7.3f %7.3f %7.3f" % (k, e[k][0], e[k][1],
                                                    e[k][2], e[k][3]))
    check("USB1 is a tone at 2400-150 = 2250 Hz, not 1950",
          e["USB1"][0] > 0.8, "e2250 = %.3f" % e["USB1"][0])
    check("nothing else puts energy at 2250",
          max(e[k][0] for k in ("S1", "SB1", "data", "ANS")) < 0.2,
          "worst %.3f" % max(e[k][0] for k in ("S1", "SB1", "data", "ANS")))
    check("S1 is carrier plus 2400 +/- 300 Hz sidebands",
          e["S1"][1] > 0.4 and (e["S1"][2] + e["S1"][3]) > 0.4,
          "e2400 %.3f, sidebands %.3f" % (e["S1"][1], e["S1"][2] + e["S1"][3]))
    check("the S1 test does not fire on data or on the answer tone",
          not (e["data"][1] > 0.20 and e["data"][2] + e["data"][3] > 0.12)
          and not (e["ANS"][1] > 0.20),
          "data %.3f/%.3f, ANS e2400 %.3f"
          % (e["data"][1], e["data"][2] + e["data"][3], e["ANS"][1]))

    print()
    print("back to back: our originator against our answerer")
    r = loopback()
    o, a = r["orig"], r["ans"]
    for t, st, msg in o.events:
        print("    orig [%6.3f] %-9s %s" % (t, st, msg))
    for t, st, msg in a.events:
        print("    ans  [%6.3f] %-9s %s" % (t, st, msg))

    check("6.3.1.1.1 a): the caller is silent until it hears the answerer",
          r["silent"])
    t_usb1 = at(o.events, "unscrambled binary 1 detected")
    t_gap = at(o.events, "-> S1TX")
    t_s1 = at(o.events, "-> SB1TX")
    t_112 = at(o.events, "circuit 112 on")
    t_train = at(o.events, "-> TRAIN")
    t_106 = at(o.events, "circuit 106 on")
    t_data = at(o.events, "-> DATA")
    check("b) USB1 detected after 155 +/- 10 ms of it",
          abs((t_usb1 - 0.44) * 1000 - 155) <= 10,
          "%.0f ms of USB1 (it began at 0.440)" % ((t_usb1 - 0.44) * 1000))
    check("b) then silent for a further 456 +/- 10 ms",
          abs((t_gap - t_usb1) * 1000 - 456) <= 10,
          "%.0f ms" % ((t_gap - t_usb1) * 1000))
    check("b) then S1 for 100 +/- 3 ms",
          abs((t_s1 - t_gap) * 1000 - 100) <= 3,
          "%.0f ms" % ((t_s1 - t_gap) * 1000))
    check("d) 2400 bit/s begins 600 +/- 10 ms after circuit 112",
          abs((t_train - t_112) * 1000 - 600) <= 10,
          "%.0f ms" % ((t_train - t_112) * 1000))
    check("e) circuit 106 on after 200 +/- 10 ms of it",
          abs((t_106 - t_train) * 1000 - 200) <= 10,
          "%.0f ms" % ((t_106 - t_train) * 1000))
    check("...and training continues to 400 ms before data, as the answerer does",
          abs((t_data - t_train) * 1000 - 400) <= 10,
          "%.0f ms" % ((t_data - t_train) * 1000))
    check("both sides reach their data phase",
          o.state == fsm.DATA and a.state == fsm.DATA,
          "orig %s at %.3f, ans %s" % (o.state, t_data, a.state))

    print()
    print("...and data flows both ways")
    for lbl, rx, exp in (("answerer -> originator", r["rx_o"], r["pat_a"]),
                         ("originator -> answerer", r["rx_a"], r["pat_o"])):
        d = bytes(rx.data)
        frac, ok = score(d, exp)
        q = rx.summary()
        check("%s: every character correct" % lbl, d and ok == len(d),
              "%d/%d, acq@%d, %d retrain(s), %d framing errors"
              % (ok, len(d), q["acquired_at"], q["retrains"], q["framing_bad"]))

    print()
    print("with a two-wire hybrid echoing each side's own transmission back")
    for db in (-20.0, -12.0):
        g = 10 ** (db / 20.0)
        rr = loopback(frames=1400, echo=g)
        good = True
        detail = []
        for rx, exp in ((rr["rx_o"], rr["pat_a"]), (rr["rx_a"], rr["pat_o"])):
            d = bytes(rx.data)
            frac, ok = score(d, exp)
            good = good and bool(d) and ok == len(d)
            detail.append("%d/%d" % (ok, len(d)))
        check("echo at %.0f dB: both directions still error-free" % db, good,
              " and ".join(detail) + ("; orig %s, ans %s"
                                      % (rr["orig"].state, rr["ans"].state)))

    print()
    print("giving up cleanly")
    lonely = fsm.OriginateV22bis(level_dbfs=-18.0, usb1_timeout=1.0)
    for _ in range(120):
        lonely.step([0] * fsm.FRAME)
    check("no answer: WAITUSB1 times out into FAILED",
          lonely.state == fsm.FAILED,
          "%s; %s" % (lonely.state, at(lonely.events, "no USB1") is not None))
    deaf = fsm.OriginateV22bis(level_dbfs=-18.0, s1_timeout=0.6)
    mm = v22bis.Mod("high", level_dbfs=-18.0)
    usb1 = mm.modulate([1] * 4800, scramble=False, bps=2)
    for k in range(200):
        deaf.step(usb1[k * fsm.FRAME:(k + 1) * fsm.FRAME])
    check("answerer stuck on USB1 for ever: SB1TX times out into FAILED",
          deaf.state == fsm.FAILED,
          "%s -- USB1 is neither S1 nor scrambled data, so neither branch "
          "fires and it gives up rather than waiting" % deaf.state)

    print()
    print("the 1200 bit/s fallback, 6.3.1.2, back to back")
    # A caller that only does 1200 bit/s goes straight from the 456 ms gap to
    # scrambled binary 1 (6.3.1.2.1 b) and never sends S1. That is enough to send
    # both sides down the fallback: the answerer sees no S1, and the caller then
    # sees the answerer's SB1 rather than an S1.
    r12 = loopback(frames=2200, v22_only=True)
    o12, a12 = r12["orig"], r12["ans"]
    for t, st, msg in o12.events:
        print("    orig [%6.3f] %-9s %s" % (t, st, msg))
    for t, st, msg in a12.events:
        print("    ans  [%6.3f] %-9s %s" % (t, st, msg))

    t_u = at(o12.events, "unscrambled binary 1 detected")
    t_sb = at(o12.events, "-> SB1TX")
    t_109 = at(o12.events, "circuit 109 on")
    t_d = at(o12.events, "-> DATA1200")
    a_fb = at(a12.events, "circuit 112 off")
    a_rdy = at(a12.events, "ready both ways")
    check("6.3.1.2.1 b): no S1 -- straight from the gap to scrambled binary 1",
          fsm.S1TX not in [st for _, st, _ in o12.events],
          "states: %s" % sorted({st for _, st, _ in o12.events}))
    check("b) the 456 +/- 10 ms gap is still observed",
          abs((t_sb - t_u) * 1000 - 456) <= 10, "%.0f ms" % ((t_sb - t_u) * 1000))
    check("c) caller falls back on 270 +/- 40 ms of scrambled binary 1",
          "280 ms of scrambled binary 1" in "".join(m for _, _, m in o12.events),
          "detected for 280 ms")
    check("d) caller ready to transmit 765 +/- 10 ms after circuit 109",
          abs((t_d - t_109) * 1000 - 765) <= 10,
          "%.0f ms" % ((t_d - t_109) * 1000))
    check("6.3.1.2.2 b) answerer drops circuit 112 on the same evidence",
          a_fb is not None, "at %.3f s" % (a_fb or -1))
    check("c) answerer ready after 765 +/- 10 ms of its own SB1",
          abs((a_rdy - a_fb) * 1000 - 765) <= 10,
          "%.0f ms" % ((a_rdy - a_fb) * 1000))
    check("both sides settle on 1200 bit/s",
          o12.line_rate == 1200 and a12.line_rate == 1200,
          "orig %d, ans %d" % (o12.line_rate, a12.line_rate))
    check("both receivers switched to the four-point constellation",
          o12.rx_mode.name == "1200" and a12.rx_mode.name == "1200"
          and o12.rx_mode.bps == 2)

    for lbl, rx, exp in (("answerer -> originator", r12["rx_o"], r12["pat_a"]),
                         ("originator -> answerer", r12["rx_a"], r12["pat_o"])):
        d = bytes(rx.data) if rx else b""
        frac, ok = score(d, exp)
        check("%s at 1200 bit/s: every character correct" % lbl,
              d and ok == len(d), "%d/%d" % (ok, len(d)))

    print()
    print("...and the 2400 bit/s path is not taken by accident")
    check("with a V.22bis caller both sides stay at 2400",
          r["orig"].line_rate == 2400 and r["ans"].line_rate == 2400,
          "orig %d, ans %d" % (r["orig"].line_rate, r["ans"].line_rate))
    check("the answerer never dropped circuit 112",
          at(r["ans"].events, "circuit 112 off") is None)
    check("the caller never turned circuit 109 on",
          at(r["orig"].events, "circuit 109 on") is None)

    print()
    if FAIL:
        print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
        sys.exit(1)
    print("all originate tests passed")
