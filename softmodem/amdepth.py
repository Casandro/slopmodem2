"""Calibrated AM-depth estimator.

Motivation: naive estimates of ANSam's 15 Hz AM disagree wildly. A long
coherent Goertzel on the sidebands washes out any signal whose AM is not
perfectly coherent (the same over-integration trap that made a real 1800 Hz
tone read as absent); short windows inflate the estimate with noise; and
envelope percentiles are biased by noise in the opposite direction.

So: estimate the coherent AM component of the envelope, and *calibrate the
estimator against synthetic signals of known depth* before trusting it on
measured data.
"""
import math
import dsp

def am_depth(x, sr=8000, carrier=None, am_rate=15.0, win=24, step=4,
             search=True):
    """Return (depth, rate_hz, coherence).

    depth      : peak deviation as a fraction of mean envelope
    coherence  : fraction of envelope variance explained by that component;
                 low coherence means the "AM" is really broadband noise
    """
    if carrier is None:
        carrier, _ = dsp.dominant(x, 1800, 2400, coarse=10, fine=1, sr=sr)
    ts, env = dsp.sliding_mag(x, carrier, sr, win=win, step=step)
    if len(env) < 100:
        return 0.0, 0.0, 0.0
    m = sum(env) / len(env)
    if m <= 0:
        return 0.0, 0.0, 0.0
    d = [v - m for v in env]
    esr = sr / float(step)
    var = dsp.mean_square(d)
    if search:
        best, bf = -1.0, am_rate
        f = max(2.0, am_rate - 6.0)
        while f <= am_rate + 6.0:
            p = dsp.goertzel(d, f, esr)
            if p > best:
                best, bf = p, f
            f += 0.05
    else:
        bf = am_rate
        best = dsp.goertzel(d, bf, esr)
    # goertzel returns mean power A^2/2 for a sinusoid of amplitude A
    amp = math.sqrt(max(best, 0.0) * 2.0)
    return amp / m, bf, (best / var if var > 0 else 0.0)

def calibrate(verbose=True):
    """Recover known depths from synthetic ANSam. Returns max abs error."""
    import ansam
    worst = 0.0
    if verbose:
        print("  calibration: synthetic ANSam, known depth -> estimate")
        print("    %-8s %-8s %-8s %-9s %s" % ("level", "true", "est", "rate", "coherence"))
    for lvl in (-12.0, -24.0, -30.0):
        for true_d in (0.20, 0.10, 0.04, 0.01):
            x = ansam.ansam_samples(5.0, reversal_ms=None, am_depth=true_d,
                                    level_dbfs=lvl)
            d, r, c = am_depth(x)
            worst = max(worst, abs(d - true_d))
            if verbose:
                print("    %-8.0f %-8.3f %-8.3f %-9.2f %.3f" % (lvl, true_d, d, r, c))
    if verbose:
        print("    worst absolute error: %.4f" % worst)
    return worst

if __name__ == "__main__":
    import sys, g711
    calibrate()
    print()
    for path in sys.argv[1:]:
        pt = 0 if path.endswith("_ulaw.raw") else 8
        x = g711.decode(open(path, "rb").read(), pt)
        seg = dsp.find_tone_segment(x, 2100.0)
        if not seg:
            print("  %-34s no 2100 Hz segment" % path)
            continue
        y = x[seg[0]:seg[1]]
        d, r, c = am_depth(y)
        print("  %-34s %.2f s  %6.1f dBFS  depth=%5.2f%%  rate=%.2f Hz  coh=%.3f"
              % (path.split("/")[-1], len(y)/8000.0, dsp.dbfs(dsp.rms(y)),
                 100*d, r, c))
