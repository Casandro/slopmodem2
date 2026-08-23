"""Measure the V.22bis 16-point constellation from a hardware modem capture.

Figure 2/V.22bis is a figure, so the constellation is not recoverable from the
Recommendation's text. It is recoverable from the hardware: drive a modem through
the V.22bis handshake until it transmits scrambled binary 1 at 2400 bit/s, then
look at where its symbols actually land.

Timing is found by brute force rather than a tracking loop. The symbol rate is
600 baud +/- 0.01% and our sample clock is locked to the PBX by the 1:1 RTP
pacing, so a fixed sampling phase holds for well over a second; scanning it is
more robust than asking a Gardner loop to acquire on a signal that has been
through the analogue path.

The score is the fourth-order moment |E[z^4]| / E[|z|^4]. A QAM constellation
has four-fold symmetry, so z^4 collects into a single lobe when the sampling
phase is right, and arg(E[z^4])/4 gives the residual carrier rotation for free.
"""
import cmath, math, sys
import g711, v22


def baseband(x, carrier):
    w = 2 * math.pi * carrier / v22.SR
    z = [x[i] * cmath.exp(-1j * w * i) for i in range(len(x))]
    h = v22.srrc()
    L = len(h)
    out = [0j] * len(z)
    for i in range(len(z)):
        acc = 0j
        for j in range(max(0, i - L + 1), i + 1):
            acc += z[j] * h[i - j]
        out[i] = acc
    return out


def m4_score(syms):
    if not syms:
        return 0.0, 0.0
    n = len(syms)
    s4 = sum(s ** 4 for s in syms) / n
    p4 = sum(abs(s) ** 4 for s in syms) / n
    if p4 <= 0:
        return 0.0, 0.0
    return abs(s4) / p4, cmath.phase(s4) / 4.0


def extract(x, carrier=v22.LOW, steps=96):
    """Return (symbols, timing_phase, rotation, score)."""
    y = baseband(x, carrier)
    best = (None, 0.0, 0.0, -1.0)
    for k in range(steps):
        ph = k * v22.SPS / steps
        syms = []
        pos = ph + 4 * v22.SPS
        while pos + 1 < len(y):
            i = int(pos)
            f = pos - i
            syms.append(y[i] * (1 - f) + y[i + 1] * f)
            pos += v22.SPS
        sc, rot = m4_score(syms[8:-8])
        if sc > best[3]:
            best = (syms[8:-8], ph, rot, sc)
    syms, ph, rot, sc = best
    syms = [s * cmath.exp(-1j * rot) for s in syms]
    return syms, ph, rot, sc


def rings(syms, k=3, iters=40):
    """1-D k-means on |z| to find the constellation radii."""
    mags = sorted(abs(s) for s in syms)
    if len(mags) < 20:
        return []
    lo, hi = mags[int(0.02 * len(mags))], mags[int(0.98 * len(mags))]
    cent = [lo + (hi - lo) * (i + 0.5) / k for i in range(k)]
    for _ in range(iters):
        groups = [[] for _ in range(k)]
        for v in mags:
            groups[min(range(k), key=lambda j: abs(v - cent[j]))].append(v)
        for j in range(k):
            if groups[j]:
                cent[j] = sum(groups[j]) / len(groups[j])
    return sorted((c, len(g)) for c, g in zip(cent, groups))


def report(path, a, b, carrier=v22.LOW):
    raw = open(path, "rb").read()
    x = g711.decode(raw, 8)[int(a * 8000):int(b * 8000)]
    syms, ph, rot, sc = extract(x, carrier)
    print("  segment %.1f-%.1f s: %d symbols, timing phase %.2f/%.2f samples, "
          "rotation %.1f deg, m4 score %.3f"
          % (a, b, len(syms), ph, v22.SPS, math.degrees(rot), sc))
    rg = rings(syms, 3)
    if not rg:
        print("    too few symbols")
        return
    base = rg[0][0]
    print("    radius clusters (k=3): " + ", ".join(
        "%.0f (n=%d, x%.2f)" % (c, n, c / base) for c, n in rg))
    print("    16-QAM with points at (+-1,+-1),(+-1,+-3),(+-3,+-1),(+-3,+-3)")
    print("    would give radii sqrt2 : sqrt10 : sqrt18 = 1.00 : 2.24 : 3.00")
    # quadrant occupancy and angular clustering
    ang = [(math.degrees(cmath.phase(s)) + 360) % 360 for s in syms]
    bins = [0] * 24
    for t in ang:
        bins[int(t / 15) % 24] += 1
    mx = max(bins) or 1
    print("    angle histogram (15 deg bins, scaled to peak): " +
          "".join("%d" % int(9.0 * c / mx) for c in bins))
    eq, hist = equalise(syms)
    print("    decision-directed LMS against that lattice: MSE %.3f -> %.3f"
          % (hist[0], hist[-1]))
    rg2 = rings(eq, 3)
    if rg2:
        b2 = rg2[0][0]
        print("    radii after equalising: " + ", ".join(
            "%.2f (n=%d, x%.2f)" % (c, n, c / b2) for c, n in rg2))
        errs = [abs(slice_to(z) - z) for z in eq]
        errs.sort()
        print("    per-symbol distance to nearest lattice point: median %.3f, "
              "90th pct %.3f (half the minimum point spacing is 1.0)"
              % (errs[len(errs)//2], errs[int(0.9*len(errs))]))


LATTICE = [complex(i, q) for i in (-3, -1, 1, 3) for q in (-3, -1, 1, 3)]


def slice_to(z):
    return min(LATTICE, key=lambda p: abs(z - p))


def equalise(syms, taps=5, mu=2e-3, passes=6):
    """Decision-directed LMS against the (+-1,+-3) lattice.

    This is also a test of the hypothesis, not just a clean-up: if the true
    constellation is that lattice, a decision-directed equaliser converges and
    the residual error falls. If it is some other set of points, it does not.
    """
    mags = sorted(abs(s) for s in syms)
    inner = mags[int(0.12 * len(mags))]
    g = math.sqrt(2.0) / max(inner, 1e-9)
    z = [s * g for s in syms]
    w = [0j] * taps
    w[taps // 2] = 1.0 + 0j
    hist = []
    for p in range(passes):
        err2 = 0.0
        n = 0
        for i in range(taps, len(z)):
            xv = z[i - taps + 1:i + 1]
            y = sum(w[k] * xv[taps - 1 - k] for k in range(taps))
            d = slice_to(y)
            e = d - y
            err2 += abs(e) ** 2
            n += 1
            for k in range(taps):
                w[k] += mu * e * xv[taps - 1 - k].conjugate()
        hist.append(err2 / max(n, 1))
    out = []
    for i in range(taps, len(z)):
        xv = z[i - taps + 1:i + 1]
        out.append(sum(w[k] * xv[taps - 1 - k] for k in range(taps)))
    return out, hist


if __name__ == "__main__":
    path = sys.argv[1]
    for a, b in ((4.0, 6.0), (8.0, 10.0), (14.0, 16.0)):
        report(path, a, b)
