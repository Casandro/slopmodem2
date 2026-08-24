"""The received eye through a live call, window by window, off the capture.

The figure printed at the end of a call is the last 4000 data symbols, and
data_syms is cleared on every retrain. At 12 000 that is steady state out of an
83 s data phase; at 14 400 the data phase lasts about three seconds, so the same
statistic is always the 1.7 s immediately before a collapse. Comparing the two
compares windows, not links.

This measures the same capture the call already saved, but as a series of short
windows from the moment the data phase opens, so the shape over time is visible:
whether the eye is bad from the start, or opens and then closes.

It is passive in the sense that matters -- it re-runs our receiver over recorded
audio, so nothing about the measurement disturbs the call it is measuring, and
the window is chosen after the fact rather than by where the call happened to end.

  python3 eyewin.py <capture.raw> [window_seconds]
"""
import math, sys
import g711, v32, v32fsm

SR = 8000
# We answer, so the far end is the caller and scrambles with GPC.
FAR_TAPS = v32.Scrambler.GPC


def find_e(x, taps):
    """Signal E's time and the rate it called for, the way eye.py finds them."""
    best = None
    for t0 in [8.0 + 1.0 * i for i in range(14)]:
        rx = v32fsm._Rx(taps)
        last, n = {}, 0
        for k in range(int(t0 * SR), len(x) - 160, 160):
            for p in rx.feed(x[k:k + 160]):
                n += 1
                last[(tuple(sorted(p["rates"])), p["end"])] = k / SR
        if n and (best is None or n > best[0]):
            best = (n, dict(last), t0)
    if not best:
        return None
    n, last, t0 = best
    sel = [k for k in last if k[1] and k[0]]
    if not sel:
        return None
    return max(last[k] for k in sel), max(max(k[0]) for k in sel), t0


def main(path, win=0.5):
    x = g711.decode(open(path, "rb").read(), 8)
    print("  %.1f s of capture" % (len(x) / float(SR)))
    found = find_e(x, FAR_TAPS)
    if not found:
        print("  no signal E found -- the call never reached a data phase")
        return 1
    eT, rate, t0 = found
    ts = v32.TRELLIS_SETS.get(rate)
    print("  signal E at %.2f s, calling for %s bit/s" % (eT, rate))
    if ts is None:
        print("  no trellis set for %s" % rate)
        return 1
    mode = v32.TRELLIS_MODES[rate]
    P = mode.points
    rms = math.sqrt(sum(abs(p) ** 2 for p in P) / len(P))
    dmin = min(abs(a - b) for i, a in enumerate(P) for b in P[i + 1:])
    print("  %d points, rms radius %.3f, decision radius %.3f, margin %.1f%%"
          % (len(P), rms, dmin / 2, 100.0 * (dmin / 2) / rms))

    rx = v32fsm._Rx(FAR_TAPS)
    for k in range(int(11.0 * SR), int((eT + 0.11) * SR) - 160, 160):
        rx.feed(x[k:k + 160])
    rx.to_data(mode, True, ts)

    print()
    print("  %-9s %-9s %-11s %-11s %s"
          % ("t (s)", "symbols", "median", "residual", "inside d_min/2"))
    start = int((eT + 0.11) * SR)
    step = int(win * SR)
    for w0 in range(start, len(x) - 160, step):
        before = len(rx.data_syms)
        for k in range(w0, min(w0 + step, len(x) - 160), 160):
            rx.feed(x[k:k + 160])
        syms = rx.data_syms[before:]
        if len(syms) < 200:
            continue
        d = sorted(min(abs(z - p) for p in P) for z in syms)
        med = d[len(d) // 2]
        inside = 100.0 * sum(1 for v in d if v < dmin / 2) / len(d)
        print("  %-9.1f %-9d %-11.3f %-11s %.1f%%"
              % (w0 / float(SR), len(syms), med,
                 "%.1f%%" % (100.0 * med / rms), inside))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 0.5))
