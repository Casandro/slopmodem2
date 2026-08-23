"""Measure each modem's transmitted eye from a bridge capture.

Our receiver is the same for every capture, so the *within-call* difference
between the two legs is immune to whatever varies between calls -- which is the
only comparison that can attribute a fault to a modem rather than to a call.
"""
import sys
import g711, v32, v32fsm

SR = 8000


def eye(path, taps):
    x = g711.decode(open(path, "rb").read(), 8)
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
    n, last, t0 = best
    sel = [k for k in last if k[1] and k[0]]
    if not sel:
        return ("no signal E", n, None, None)
    eT = max(last[k] for k in sel)
    rate = max(max(k[0]) for k in sel)
    ts = v32.TRELLIS_SETS.get(rate)
    if ts is None:
        return ("no set for %s" % rate, n, rate, None)
    rx2 = v32fsm._Rx(taps)
    for k in range(int(11.0 * SR), int((eT + 0.11) * SR) - 160, 160):
        rx2.feed(x[k:k + 160])
    rx2.to_data(v32.TRELLIS_MODES[rate], True, ts)
    for k in range(int((eT + 0.11) * SR), len(x) - 160, 160):
        rx2.feed(x[k:k + 160])
    P = rx2.mode.points
    if not rx2.data_syms:
        return ("no symbols", n, rate, None)
    d = sorted(min(abs(z - p) for p in P) for z in rx2.data_syms[-4000:])
    dmin = min(abs(a - b) for i, a in enumerate(P) for b in P[i + 1:])
    inside = 100.0 * sum(1 for v in d if v < dmin / 2) / len(d)
    return ("ok", n, rate, (d[len(d) // 2], inside))


if __name__ == "__main__":
    # run -> (legA modem, legB modem)
    runs = [(5, "Cirrus", "Conexant"), (7, "Cirrus", "Conexant"),
            (8, "Cirrus", "Conexant"), (6, "Conexant", "Cirrus"),
            (9, "Conexant", "Cirrus"), (10, "Conexant", "Cirrus")]
    print("%-5s %-9s %-26s %-26s" % ("run", "legA is", "leg A transmit", "leg B transmit"))
    for i, a, b in runs:
        out = []
        for leg, taps in (("a", v32.Scrambler.GPC), ("b", v32.Scrambler.GPA)):
            try:
                r = eye("ref/br%d_%s.raw" % (i, leg), taps)
            except Exception as e:
                r = ("error %s" % e, 0, None, None)
            if r is None:
                out.append("no rate signal")
            elif r[0] != "ok":
                out.append("%s (rate %s)" % (r[0], r[3] and r[2] or r[2]))
            else:
                out.append("%5s: med %.3f, %5.1f%%" % (r[2], r[3][0], r[3][1]))
        print("%-5d %-9s %-26s %-26s" % (i, a, out[0], out[1]))
