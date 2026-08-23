"""Replay a captured V.32 call through the receiver alone.

The state machines cannot be replayed -- what we transmit changes what the far
end does -- but the *receiver* can, and that is where the remaining problems are.
Open a StreamRx on the four-point mode at the far end's TRN, let it train, switch
to the negotiated data constellation where signal E was seen, and watch what
happens. Fully reproducible, no hardware, one second per second of call.

  python3 v32replay.py ref/v32if_rx.raw --trn 4.34 --data 8.34
"""
import argparse, math, sys
import g711, tracking, v32, v32fsm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--trn", type=float, default=4.34, help="seconds: open here")
    ap.add_argument("--data", type=float, default=8.34, help="seconds: switch here")
    ap.add_argument("--mode", default="9600T", choices=("9600T", "9600", "4800"))
    ap.add_argument("--lose", type=float, default=None,
                    help="override lose_thresh")
    ap.add_argument("--every", type=float, default=2.0, help="report interval, s")
    ap.add_argument("--taps", type=int, default=21)
    ap.add_argument("--mu-dd", type=float, default=None)
    ap.add_argument("--quiet", action="store_true", help="one summary line only")
    ap.add_argument("--end", type=float, default=None,
                    help="stop here: the far end may stop sending data before "
                         "the call ends, and a retrain tone flatters the eye")
    a = ap.parse_args()

    x = g711.decode(open(a.path, "rb").read(), 8)
    print("%s: %.2f s" % (a.path, len(x) / 8000.0))
    dm = {"9600T": v32.TRELLIS9600, "9600": v32.QAM9600, "4800": v32.QPSK4800}[a.mode]
    kw = {}
    if a.mu_dd is not None:
        kw["mu_dd"] = a.mu_dd
    rx = tracking.StreamRx(carrier=v32.CARRIER, mode=v32.QPSK4800,
                           sps=v32.SPS, baud=v32.BAUD, beta=v32.ROLLOFF,
                           span=10, acq_min=400, acq_win=400, settle=200,
                           taps=a.taps, **kw)
    if a.lose is not None:
        rx.lose_thresh = a.lose
    i0 = int(a.trn * 8000) // 160 * 160
    i1 = int(a.data * 8000) // 160 * 160
    switched = False
    syms = []
    nxt = a.trn + a.every
    print("  t(s)  mode   dd  retr clamp acq_med  median  p90   in0.35  n")
    iend = len(x) - 160 if a.end is None else int(a.end * 8000) // 160 * 160
    for i in range(i0, iend, 160):
        if not switched and i >= i1:
            rx.rescale_to(dm)
            switched = True
            print("  --- switched to %s at %.2f s ---" % (dm.name, i / 8000.0))
        got = rx.feed(x[i:i + 160])
        syms.extend(got)
        t = i / 8000.0
        if t >= nxt and not a.quiet:
            nxt += a.every
            w = syms[-2000:]
            P = rx.mode.points
            d = sorted(min(abs(z - p) for p in P) for z in w) or [0.0]
            print("%6.2f  %-6s %-4s %-4d %-5d %7.3f  %6.3f %6.3f %5.1f%%  %d"
                  % (t, rx.mode.name, rx.dd, rx.retrains, rx.clamps,
                     getattr(rx, "acq_med", -1.0), d[len(d) // 2],
                     d[int(0.9 * len(d))],
                     100.0 * sum(1 for v in d if v < 0.35) / len(d), len(syms)))
    # when did it settle? first 2000-symbol window with 99% inside 0.35
    P = dm.points
    settle = None
    for k in range(2000, len(syms), 500):
        w = syms[k - 2000:k]
        good = sum(1 for z in w if min(abs(z - p) for p in P) < 0.35)
        if good >= 0.99 * len(w):
            settle = a.trn + k / v32.BAUD
            break
    w = syms[-4000:]
    d = sorted(min(abs(z - p) for p in P) for z in w) or [0.0]
    if a.quiet:
        print("taps %-3d mu_dd %-7s | settled %-7s | final median %.3f p90 "
              "%.3f in0.35 %.1f%% | dd %s retr %d"
              % (a.taps, a.mu_dd if a.mu_dd is not None else "default",
                 ("%.1f s" % settle) if settle else "never",
                 d[len(d) // 2], d[int(0.9 * len(d))],
                 100.0 * sum(1 for v in d if v < 0.35) / len(d),
                 rx.dd, rx.retrains))
    else:
        print()
        print("final: dd %s, retrains %d, clamps %d, %d symbols; settled %s"
              % (rx.dd, rx.retrains, rx.clamps, len(syms),
                 ("at %.1f s" % settle) if settle else "never"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
