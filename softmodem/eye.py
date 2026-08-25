"""Measure each modem's transmitted eye from a bridge capture.

Our receiver is the same for every capture, so the *within-call* difference
between the two legs is immune to whatever varies between calls -- which is the
only comparison that can attribute a fault to a modem rather than to a call.

**Where the window falls is part of the measurement.** This used to report the
last 4000 data symbols, which lets the end of the call choose the window, and the
end of a call is where the link is worst -- that is usually why it ended. It cost
a whole experiment before it was noticed: a bridge capture of the Cirrus at
12 000 reads **2.0% in the first five seconds and 12.9% in the tail**, a factor of
six, because the far modem could not hold its receive and started asking to
retrain, after which we were decoding a held carrier state as though it were data.
Read as tails, three 12 000 captures came out *worse* than six 14 400 ones on the
same path, which is impossible and is what gave the game away.

So the capture is now measured in windows from signal E onwards and three numbers
are reported, because no single one is honest on its own:

  best    the quietest window. What this transmitter's signal looks like when
          everything else is working -- our receiver's convergence ramp is at the
          start and the link's decay at the end, and neither belongs to the
          transmitter being judged. This is the headline.
  median  across all windows. Moves with decay, so best-vs-median is the shape.
  tail    the last window, which is what this file used to print. Kept so the
          older figures quoted in v32-ans-path.md can be related to new ones.

A call that decays shows best << median; a receiver still converging shows the
first window worse than the rest. Either way it is visible instead of silently
folded into one figure.

  python3 eye.py                       the six runs of the swapped 2x2
  python3 eye.py ref/br5 /tmp/b14_1    any captures, minus the _a/_b suffix
"""
import math, sys
import g711, v32, v32fsm

SR = 8000
WIN_S = 2.0                     # window length; 2 s is 4800 symbols
MIN_SYM = 1500                  # a window with fewer than this is not scored


def _find_e(x, taps):
    """Signal E's time and the rate it called for, or None."""
    best = None
    for t0 in [8.0 + 1.0 * i for i in range(14)]:
        rx = v32fsm._Rx(taps)
        last = {}
        n = 0
        for k in range(int(t0 * SR), len(x) - 160, 160):
            for p in rx.feed(x[k:k + 160]):
                n += 1
                last[(tuple(sorted(p["rates"])), p["end"])] = k / SR
        if n and (best is None or n > best[0]):
            best = (n, dict(last), t0)
    if not best:
        return None
    n, last, _ = best
    sel = [k for k in last if k[1] and k[0]]
    if not sel:
        return ("no signal E", n, None)
    return ("ok", n, (max(last[k] for k in sel),
                      max(max(k[0]) for k in sel)))


def eye(path, taps, win_s=WIN_S):
    """Windowed transmitted eye. Returns (status, nrate, rate, stats)."""
    x = g711.decode(open(path, "rb").read(), 8)
    found = _find_e(x, taps)
    if found is None:
        return None
    if found[0] != "ok":
        return (found[0], found[1], None, None)
    n, (eT, rate) = found[1], found[2]
    ts = v32.TRELLIS_SETS.get(rate)
    if ts is None:
        return ("no set for %s" % rate, n, rate, None)

    rx = v32fsm._Rx(taps)
    for k in range(int(11.0 * SR), int((eT + 0.11) * SR) - 160, 160):
        rx.feed(x[k:k + 160])
    rx.to_data(v32.TRELLIS_MODES[rate], True, ts)

    P = rx.mode.points
    dmin = min(abs(a - b) for i, a in enumerate(P) for b in P[i + 1:])
    rms = math.sqrt(sum(abs(p) ** 2 for p in P) / len(P))
    step = int(win_s * SR)
    wins = []
    for w0 in range(int((eT + 0.11) * SR), len(x) - 160, step):
        mark = len(rx.data_syms)
        for k in range(w0, min(w0 + step, len(x) - 160), 160):
            rx.feed(x[k:k + 160])
        syms = rx.data_syms[mark:]
        if len(syms) < MIN_SYM:
            continue
        d = sorted(min(abs(z - p) for p in P) for z in syms)
        med = d[len(d) // 2]
        inside = 100.0 * sum(1 for v in d if v < dmin / 2) / len(d)
        wins.append((med, 100.0 * med / rms, inside, w0 / float(SR)))
    if not wins:
        return ("no symbols", n, rate, None)
    order = sorted(wins)
    best = order[0]
    mid = order[len(order) // 2]
    tail = wins[-1]
    return ("ok", n, rate, {"best": best, "median": mid, "tail": tail,
                            "windows": len(wins)})


def _cell(r):
    if r is None:
        return "no rate signal"
    if r[0] != "ok":
        return "%s (rate %s)" % (r[0], r[2])
    s = r[3]
    return ("%5s best %5.2f%% @%4.0fs, med %5.2f%%, tail %5.2f%% (%d win)"
            % (r[2], s["best"][1], s["best"][3], s["median"][1],
               s["tail"][1], s["windows"]))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        import os
        pairs = [(t, os.path.basename(t), "", "") for t in sys.argv[1:]]
    else:
        # run -> (legA modem, legB modem)
        pairs = [("ref/br%d" % i, str(i), a, b) for i, a, b in
                 ((5, "Cirrus", "Conexant"), (7, "Cirrus", "Conexant"),
                  (8, "Cirrus", "Conexant"), (6, "Conexant", "Cirrus"),
                  (9, "Conexant", "Cirrus"), (10, "Conexant", "Cirrus"))]
    print("%-16s %-9s %-46s %s"
          % ("run", "legA is", "leg A transmit", "leg B transmit"))
    for base, label, a, b in pairs:
        out = []
        for leg, taps in (("a", v32.Scrambler.GPC), ("b", v32.Scrambler.GPA)):
            try:
                out.append(_cell(eye("%s_%s.raw" % (base, leg), taps)))
            except Exception as e:
                out.append("error %s" % e)
        print("%-16s %-9s %-46s %s" % (label, a, out[0], out[1]))
