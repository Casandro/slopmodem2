"""Decode a whole V.22bis capture in one pass with the tracking receiver.

The point of contrast with `v22bis_rx.py`: that script decodes a window at a
time, so it re-acquires from cold for every window and pays a blind-equaliser
transient each time, and its single tap set has to be a compromise across the
window. This one acquires once and tracks, so a capture of any length is one
continuous symbol stream, one framing search and one score.

usage: v22bis_track.py CAPTURE EXPECTED [START_S] [carrier]
"""
import sys
import g711, v22, v22bis_rx, tracking


def score(text, expect):
    """Fraction of characters matching the expected repeating pattern.

    One global phase for the whole stream. Fine when nothing slips, misleading
    when something does -- see score_slip.
    """
    if not text:
        return 0.0, 0
    best = (0, 0)
    for ph in range(len(expect)):
        ok = sum(1 for i, c in enumerate(text)
                 if c == expect[(i + ph) % len(expect)])
        if ok > best[0]:
            best = (ok, ph)
    return best[0] / float(len(text)), best[1]


def score_slip(text, expect, look=40):
    """Slip-tolerant score: (fraction correct, slips, positions).

    A single global phase is the wrong model for this stream. V.14 -- which
    V.22bis 4.1.2 and 4.2 require for the start-stop modes -- rate-adapts by
    *deleting and inserting stop bits*, so the far modem legitimately emits a
    character more or fewer than the nominal rate now and then. One such event
    shifts the phase of every character after it, and a fixed-phase scorer then
    counts the entire remainder as wrong: measured on the Conexant capture, a
    single slip near character 13 000 dragged the reported match from 100% down
    to 92.9% while the decoded text either side of it was visibly perfect.

    So: walk the stream, and on a mismatch try re-aligning. Every phase of the
    pattern is tried, not a small window of them -- both slips measured on the
    Conexant capture turned out to be +6 characters, which a +/-4 search missed
    entirely and then charged as 990 wrong characters. Re-alignment demands that
    the next `look` characters match *exactly*, so a spurious resync would need
    40 consecutive characters to agree by chance.

    Slips are reported, never hidden -- they are a real property of the link,
    just not a decode error.
    """
    n = len(expect)
    if not text:
        return 0.0, 0, []
    head = min(len(text), look * 8)
    ph = max(range(n), key=lambda p: sum(
        1 for i in range(head) if text[i] == expect[(i + p) % n]))
    ok, slips, at, i = 0, 0, [], 0
    while i < len(text):
        if text[i] == expect[(i + ph) % n]:
            ok += 1
            i += 1
            continue
        found = None
        m = min(look, len(text) - i)
        if m >= look // 2:
            for s in range(1, n):
                if all(text[i + k] == expect[(i + k + ph + s) % n]
                       for k in range(m)):
                    found = s
                    break
        if found is not None:
            ph = (ph + found) % n
            slips += 1
            at.append(i)
        i += 1
    return ok / float(len(text)), slips, at


def run(path, expect, start_s=6.0, carrier=v22.LOW, verbose=True,
        settle=1500, lag=100, **kw):
    """Decode `path` in one pass and score it against a repeating `expect`.

    Both edge guards are derived, not tuned:

      settle  the DD equaliser's tap time constant is ntaps/mu_dd = 21/0.02 ~=
              1050 symbols, and measurement bears that out -- median lattice
              distance after the handover reads 0.098, 0.041, 0.069, 0.090,
              0.099, 0.067, 0.037 over successive 200-symbol groups before it
              settles to 0.013. So the first ~1400 symbols of decision-directed
              operation are still converging.
      lag     the loss-of-lock detector averages the decision error with
              alpha = 0.01, i.e. a 100-symbol window, so it reports a drop about
              that late. Measured: quality is 0.014 at symbol 38900, 1.397 at
              39000, and the detector reports at 39045.
    """
    x = g711.decode(open(path, "rb").read(), 8)[int(start_s * 8000):]
    rx = tracking.TrackingRx(**kw)
    syms, info = rx.run(x, carrier)

    # everything from here is a single pass over the whole symbol stream
    bits = tracking.decode(syms)
    fscore, off, chars, good, bad = v22bis_rx.deframe(bits)
    text = bytes(c & 0xFF for c in chars).decode("latin-1", "replace")
    frac, ph = score(text, expect)

    # The same single pass, restricted to the stretch the receiver itself says
    # it was locked over: from `settle` symbols after the last acquisition up to
    # the next loss of lock, if any. On these captures that upper bound is the
    # far modem ceasing transmission at the end of the call -- the receiver
    # reports the drop within a quarter of a second of when the sender stopped --
    # so it is a signal boundary, not a decode failure.
    lo = info["dd_at"] + settle
    hi = len(syms)
    for at, mode in info["events"]:
        if mode == "acq" and at > lo:
            hi = max(lo, at - lag)
            break
    lock_syms = syms[lo:hi]
    lbits = tracking.decode(lock_syms)
    ls, loff, lchars, lgood, lbad = v22bis_rx.deframe(lbits)
    ltext = bytes(c & 0xFF for c in lchars).decode("latin-1", "replace")
    lfrac, lph = score(ltext, expect)
    sfrac, slips, slip_at = score_slip(ltext, expect)

    if verbose:
        print("  %s from %.0f s: %d symbols in one pass (%.1f s of line time)"
              % (path.split("/")[-1], start_s, info["nsym"], info["nsym"] / 600.0))
        print("     acquired at symbol %d (median %.3f), carrier %+.3f Hz, "
              "timing correction %+.2f samples"
              % (info["dd_at"], info["acq_median"] or -1,
                 info["carrier_hz"], info["timing_drift_samples"]))
        print("     mode changes: %s"
              % (", ".join("%s@%d" % (m, a) for a, m in info["events"]) or "none"))
        print("     locked stretch %d-%d (%.1f s); median lattice distance "
              "%.3f overall, %.3f while locked"
              % (lo, hi, (hi - lo) / 600.0,
                 tracking.quality(syms)[0], tracking.quality(lock_syms)[0]))
        print("     framing: %d chars accepted, %d rejected (%.1f%% clean)"
              % (good, bad, 100 * fscore))
        print("     MATCH %.3f%% of %d chars overall; %.3f%% of %d while "
              "locked (single phase)"
              % (100 * frac, len(text), 100 * lfrac, len(ltext)))
        print("     **slip-tolerant: %.4f%% of %d chars correct, %d character "
              "slip%s%s**"
              % (100 * sfrac, len(ltext), slips, "" if slips == 1 else "s",
                 (" at " + ", ".join(str(v) for v in slip_at[:6])) if slips else ""))
        pr = "".join(ch if 32 <= ord(ch) < 127 else "." for ch in ltext)
        print("     decoded: %r" % pr[:88])
    return {"nsym": info["nsym"], "retrains": info["retrains"],
            "overall": (frac, len(text)), "locked": (lfrac, len(ltext)),
            "slip": (sfrac, slips, slip_at),
            "span": (lo, hi), "syms": syms, "info": info}


def stability(syms, expect, chunk=3000):
    """Median lattice distance per chunk, to show it does not drift."""
    out = []
    for i in range(0, len(syms) - chunk + 1, chunk):
        out.append(tracking.quality(syms[i:i + chunk])[0])
    return out


if __name__ == "__main__":
    path = sys.argv[1]
    expect = sys.argv[2]
    start = float(sys.argv[3]) if len(sys.argv) > 3 else 6.0
    carrier = float(sys.argv[4]) if len(sys.argv) > 4 else v22.LOW
    run(path, expect, start, carrier)
