"""How much of what arrived is explained by what we sent? (no demodulator)

Convention: cap[i] ~= sum_k h[k] * ref[i + T + D - k], i.e. the capture is the
reference delayed by T and filtered by h. Fitting h per block absorbs delay, gain
and frequency response -- everything an equaliser would remove -- so what is left
is noise and nonlinearity, which is the part that eats the decision margin.

Two things to know before believing a number this prints.

**It has a floor, and the floor is the recorder.** Paired with voicecap/vcap, the
capture comes through a modem's *voice* front end, which is not the path its data
receiver uses. A pure 1800 Hz tone -- which a linear path predicts exactly -- came
back at 7.1% residue, and 12 000 and 14 400 data signals both came back at 11.2%,
agreeing to within 0.1%. Two different signals giving the same answer is this
project's standing signature for measuring the instrument. So this resolves gross
faults, not fine ones: it will show a dropout, a level problem or a spurious tone,
and it will not tell you whether a channel is at 1% or at 9%.

**Blocks have to be short.** The two ends' clocks differ -- 46 ppm measured on
this rig -- so the alignment moves within a block, and a fixed lag plus a 41-tap
filter cannot follow it. At 0.5 s the tone reads 7.1%; at 2.0 s the same capture
reads 28%, which is drift being scored as noise. Keep blocks at or under 0.5 s
unless the offset column is provably still.

  python3 chanfit.py <reference.alaw> <capture.s16> [block_s]
"""
import math, struct, sys
import g711

SR = 8000
NTAP = 41
D = NTAP // 2


def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-9:
            continue
        M[c], M[p] = M[p], M[c]
        pv = M[c][c]
        for r in range(n):
            if r == c:
                continue
            f = M[r][c] / pv
            if f:
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-9 else 0.0 for i in range(n)]


def corr_at(ref, cap, i0, n, T, stride=3):
    if i0 + T + n > len(ref):
        return -1.0
    s = ew = es = 0.0
    for k in range(0, n, stride):
        a, b = cap[i0 + k], ref[i0 + T + k]
        s += a * b
        es += a * a
        ew += b * b
    return abs(s) / math.sqrt(max(es * ew, 1e-9))


def find_T(ref, cap, i0, n, lo, hi, stride=3):
    best, bT = -1.0, lo
    for T in range(lo, hi):
        c = corr_at(ref, cap, i0, n, T, stride)
        if c > best:
            best, bT = c, T
    return bT, best


def fit(ref, cap, i0, n, T):
    lo = max(0, i0)
    hi = min(i0 + n, len(cap))
    idx = [i for i in range(lo, hi) if i + T + D < len(ref) and i + T + D - NTAP >= 0]
    if len(idx) < 4 * NTAP:
        return None
    A = [[0.0] * NTAP for _ in range(NTAP)]
    b = [0.0] * NTAP
    for i in idx:
        w = [ref[i + T + D - k] for k in range(NTAP)]
        ci = cap[i]
        for r in range(NTAP):
            wr = w[r]
            if wr:
                for c in range(r, NTAP):
                    A[r][c] += wr * w[c]
                b[r] += wr * ci
    for r in range(NTAP):
        for c in range(r):
            A[r][c] = A[c][r]
    h = solve(A, b)
    num = den = 0.0
    for i in idx:
        y = 0.0
        for k in range(NTAP):
            y += h[k] * ref[i + T + D - k]
        e = cap[i] - y
        num += float(cap[i]) * cap[i]
        den += e * e
    return 10 * math.log10(num / max(den, 1e-9))


def main(refpath, cappath, block=0.5):
    ref = g711.decode(open(refpath, "rb").read(), 8)
    b = open(cappath, "rb").read()
    cap = list(struct.unpack("<%dh" % (len(b) // 2), b[:2 * (len(b) // 2)]))
    print("  reference %d samples, capture %d samples" % (len(ref), len(cap)))
    T0, c0 = find_T(ref, cap, 60000, 2000, 0, min(12000, len(ref) - 62000))
    print("  coarse offset %d samples (%.1f ms), correlation %.3f" % (T0, 1000.0 * T0 / SR, c0))
    print()
    print("  %-9s %-8s %-7s %s" % ("t (s)", "offset", "corr", "unexplained residue"))
    n = int(block * SR)
    vals = []
    for i0 in range(8000, len(cap) - n - 1, 4 * n):
        T, c = find_T(ref, cap, i0, n, T0 - 40, T0 + 40, 2)
        s = fit(ref, cap, i0, n, T)
        if s is None:
            continue
        vals.append(s)
        print("  %-9.1f %-8d %-7.3f %.1f dB  ->  %.2f%% of amplitude"
              % (i0 / float(SR), T, c, s, 100.0 * 10 ** (-s / 20.0)))
    if vals:
        vals.sort()
        m = vals[len(vals) // 2]
        print()
        print("  median %.1f dB = %.2f%% residue, over %d blocks"
              % (m, 100.0 * 10 ** (-m / 20.0), len(vals)))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2],
         float(sys.argv[3]) if len(sys.argv) > 3 else 0.5)
