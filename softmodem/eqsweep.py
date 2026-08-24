"""Can more equaliser fix 14400, and is the residue even the kind it could fix?

The write-up already swept taps and step size once, on a Conexant capture at
12 000, and concluded the equaliser was not the limit -- but that was a different
rate, a different far end, and the conclusion rested on a second transmitter
rather than on the sweep. 14 400 against the Cirrus has never been swept, and the
capture for it is on disk, so it costs nothing but time.

Two questions, and the second one decides whether the first matters:

  1. does the settled residual move with tap count or step size?
  2. is the leftover error white?

Intersymbol interference is correlated sample to sample; additive noise is not.
An equaliser removes the first and cannot touch the second, so a lag-1
autocorrelation near zero says the residue is not something a longer filter will
reach, whatever the sweep shows.
"""
import cmath, math, sys
import g711, tracking, v32, v32fsm

SR = 8000
FAR_TAPS = v32.Scrambler.GPC
_ORIG = tracking.StreamRx


def find_e(x, taps):
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
    n, last, _ = best
    sel = [k for k in last if k[1] and k[0]]
    if not sel:
        return None
    return max(last[k] for k in sel), max(max(k[0]) for k in sel)


def replay(x, eT, rate, t_from, t_to, **cfg):
    """Residual and error whiteness over [t_from, t_to] with a given config."""
    class Wrapped(_ORIG):
        def __init__(self, *a, **kw):
            kw.update(cfg)
            _ORIG.__init__(self, *a, **kw)
    tracking.StreamRx = Wrapped
    try:
        ts = v32.TRELLIS_SETS[rate]
        mode = v32.TRELLIS_MODES[rate]
        rx = v32fsm._Rx(FAR_TAPS)
        for k in range(int(11.0 * SR), int((eT + 0.11) * SR) - 160, 160):
            rx.feed(x[k:k + 160])
        rx.to_data(mode, True, ts)
        a = int(max(t_from, eT + 0.11) * SR)
        for k in range(int((eT + 0.11) * SR), a - 160, 160):
            rx.feed(x[k:k + 160])
        mark = len(rx.data_syms)
        for k in range(a, min(int(t_to * SR), len(x) - 160), 160):
            rx.feed(x[k:k + 160])
        syms = rx.data_syms[mark:]
    finally:
        tracking.StreamRx = _ORIG
    if len(syms) < 400:
        return None
    P = mode.points
    rms = math.sqrt(sum(abs(p) ** 2 for p in P) / len(P))
    err = [z - min(P, key=lambda p: abs(z - p)) for z in syms]
    d = sorted(abs(e) for e in err)
    med = d[len(d) // 2]
    # lag-1 autocorrelation of the error, normalised
    num = sum((err[i + 1] * err[i].conjugate()).real for i in range(len(err) - 1))
    den = sum(abs(e) ** 2 for e in err)
    return 100.0 * med / rms, (num / den if den else 0.0), len(syms)


def main(path, rate_hint, t_from, t_to):
    x = g711.decode(open(path, "rb").read(), 8)
    got = find_e(x, FAR_TAPS)
    if not got:
        print("  no signal E")
        return 1
    eT, rate = got
    print("  signal E at %.2f s, rate %s, window %.1f-%.1f s"
          % (eT, rate, t_from, t_to))
    print()
    print("  %-28s %-11s %s" % ("configuration", "residual", "error lag-1 autocorr"))
    cfgs = [("21 taps (default)", {}),
            ("41 taps", {"taps": 41}),
            ("61 taps", {"taps": 61}),
            ("81 taps", {"taps": 81}),
            ("21 taps, mu_dd /4", {"mu_dd": 0.005}),
            ("41 taps, mu_dd /4", {"taps": 41, "mu_dd": 0.005}),
            ("21 taps, mu_dd x4", {"mu_dd": 0.08}),
            ("41 taps, carrier loop /4", {"taps": 41, "kp_c": 0.0025,
                                          "ki_c": 6.25e-5}),
            ]
    for name, cfg in cfgs:
        r = replay(x, eT, rate, t_from, t_to, **cfg)
        if r is None:
            print("  %-28s %s" % (name, "too few symbols"))
            continue
        res, ac, n = r
        print("  %-28s %-11s %+.4f   (%d symbols)" % (name, "%.2f%%" % res, ac, n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]),
                  float(sys.argv[3]), float(sys.argv[4])))
