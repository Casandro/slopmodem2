"""Blind equalisation for the V.22bis receiver.

Two stages, which is the standard arrangement:

  CMA (Godard)  Constant-modulus / dispersion-minimising. Converges from a cold
                start with no knowledge of the data, which is what makes it
                usable here -- a decision-directed loop diverges when a quarter
                of the decisions are already wrong.
  DD-LMS        Decision-directed refinement, switched on only once CMA has
                brought the error low enough for decisions to be mostly right.

The CMA dispersion constant is R2 = E[|a|^4] / E[|a|^2] for the source
constellation. For the V.22bis lattice on odd coordinates the 16 points have
|a|^2 in {2 (x4), 10 (x8), 18 (x4)}, so E[|a|^2] = 10, E[|a|^4] = 132, and
R2 = 13.2. Using the wrong R2 just rescales the output, but getting it right
means the equaliser output lands on the lattice without further scaling.
"""
import cmath, math
import v22bis

LATTICE = [complex(i, q) for i in (-3, -1, 1, 3) for q in (-3, -1, 1, 3)]
R2_V22BIS = 13.2          # E[|a|^4] / E[|a|^2] for the odd-coordinate 16-QAM


def slice_to(z):
    return min(LATTICE, key=lambda p: abs(z - p))


def _normalise(syms, target_power=10.0):
    p = sum(abs(s) ** 2 for s in syms) / max(len(syms), 1)
    if p <= 0:
        return list(syms)
    g = math.sqrt(target_power / p)
    return [s * g for s in syms]


def cma(syms, taps=11, mu=0.02, passes=25, R2=R2_V22BIS):
    """Godard/CMA with a power-normalised step.

    The raw gradient scales with the cube of the signal amplitude, so a fixed mu
    is hopeless across levels -- normalising by the tap-window energy makes the
    step size dimensionless and lets mu be O(0.01) rather than something that
    has to be retuned per capture.

    Note the dispersion floor is not zero: 16-QAM is not constant modulus, so
    E[(|a|^2 - R2)^2] = 42.24 for this lattice. That is the target, not 0.
    """
    z = _normalise(syms)
    w = [0j] * taps
    w[taps // 2] = 1.0 + 0j
    hist = []
    for _ in range(passes):
        disp = 0.0
        n = 0
        for i in range(taps - 1, len(z)):
            xv = z[i - taps + 1:i + 1]
            y = sum(w[k] * xv[taps - 1 - k] for k in range(taps))
            e = y * (R2 - abs(y) ** 2)
            disp += (abs(y) ** 2 - R2) ** 2
            n += 1
            nrm = sum(abs(v) ** 2 for v in xv) + 1e-9
            g = mu / (nrm * R2)
            for k in range(taps):
                w[k] += g * e * xv[taps - 1 - k].conjugate()
        hist.append(disp / max(n, 1))
    out = []
    for i in range(taps - 1, len(z)):
        xv = z[i - taps + 1:i + 1]
        out.append(sum(w[k] * xv[taps - 1 - k] for k in range(taps)))
    return out, hist, w


def dd_lms(syms, w, taps=11, mu=0.05, passes=15):
    """Decision-directed refinement, starting from CMA's taps."""
    z = list(syms)
    w = list(w)
    hist = []
    for _ in range(passes):
        err = 0.0
        n = 0
        for i in range(taps - 1, len(z)):
            xv = z[i - taps + 1:i + 1]
            y = sum(w[k] * xv[taps - 1 - k] for k in range(taps))
            d = slice_to(y)
            e = d - y
            err += abs(e) ** 2
            n += 1
            nrm = sum(abs(v) ** 2 for v in xv) + 1e-9
            g = mu / nrm
            for k in range(taps):
                w[k] += g * e * xv[taps - 1 - k].conjugate()
        hist.append(err / max(n, 1))
    out = []
    for i in range(taps - 1, len(z)):
        xv = z[i - taps + 1:i + 1]
        out.append(sum(w[k] * xv[taps - 1 - k] for k in range(taps)))
    return out, hist, w


def quality(syms):
    """Median and 90th-percentile distance to the nearest lattice point.
    The decision boundary is at 1.0 (points are spaced 2 apart)."""
    if not syms:
        return None
    d = sorted(abs(slice_to(z) - z) for z in syms)
    return d[len(d) // 2], d[int(0.9 * len(d))]


def align(syms):
    """Scale to the lattice and remove the residual rotation.

    Two corrections CMA cannot make itself:

    * Scale. CMA drives |y|^2 towards R2 = 13.2, but the lattice's mean power is
      E[|a|^2] = 10, so its output is systematically 1.149x too large. Slicing
      without rescaling puts every symbol outside its decision region.
    * Rotation. CMA is blind to phase. arg(E[y^4])/4 recovers the rotation
      modulo 90 degrees, which is all that is needed: this lattice maps onto
      itself under a 90 degree rotation *with the labels unchanged*, so any
      remaining multiple of 90 degrees is harmless to differential quadrant
      decoding -- it only changes which quadrant the first symbol is called.
    """
    z = _normalise(syms, target_power=10.0)
    n = len(z)
    if n == 0:
        return z, 0.0
    s4 = sum(v ** 4 for v in z) / n
    rot = cmath.phase(s4) / 4.0
    return [v * cmath.exp(-1j * rot) for v in z], rot


def equalise_full(syms, taps=11, try_45=True):
    """CMA -> align -> DD-LMS. Returns (symbols, info)."""
    eq, disp, w = cma(syms, taps=taps)
    al, rot = align(eq)
    best = None
    for extra in ((0.0, math.pi / 4) if try_45 else (0.0,)):
        cand = [v * cmath.exp(1j * extra) for v in al]
        ref, mse, _ = dd_lms(cand, w=[1.0 + 0j if k == taps // 2 else 0j
                                      for k in range(taps)], taps=taps)
        q = quality(ref)
        if best is None or q[0] < best[1][0]:
            best = (ref, q, mse, extra)
    ref, q, mse, extra = best
    return ref, {"dispersion": (disp[0], disp[-1]), "rotation_deg": math.degrees(rot),
                 "extra_deg": math.degrees(extra), "dd_mse": (mse[0], mse[-1]),
                 "median_err": q[0], "p90_err": q[1]}


def estimate_freq_offset(syms, block=48, sr_sym=600.0):
    """Estimate residual carrier frequency offset from the fourth-order moment.

    A frequency offset rotates the constellation continuously, which leaves the
    modulus statistics untouched but destroys the angular structure. That is why
    CMA cannot fix it -- CMA's cost is a function of |y| only, so its gradient is
    already near zero while the constellation is still a set of smeared rings.

    arg(E[z^4])/4 gives the constellation phase modulo 90 degrees. Taken over
    successive short blocks and unwrapped, its slope is the offset. Blocks must
    be short enough that the phase advances well under 90/4 degrees within one,
    or the unwrap is ambiguous.
    """
    n = len(syms)
    if n < 4 * block:
        return 0.0, []
    ph = []
    for i in range(0, n - block + 1, block):
        b = syms[i:i + block]
        s4 = sum(v ** 4 for v in b) / len(b)
        ph.append(cmath.phase(s4) / 4.0)
    # unwrap modulo pi/2, since the lattice is 90-degree symmetric
    unw = [ph[0]]
    for v in ph[1:]:
        prev = unw[-1]
        k = round((prev - v) / (math.pi / 2))
        unw.append(v + k * math.pi / 2)
    m = len(unw)
    xs = [i * block / sr_sym for i in range(m)]
    mx = sum(xs) / m
    my = sum(unw) / m
    den = sum((v - mx) ** 2 for v in xs)
    slope = sum((xs[i] - mx) * (unw[i] - my) for i in range(m)) / den if den else 0.0
    return slope / (2 * math.pi), unw          # Hz


def derotate(syms, hz, sr_sym=600.0):
    w = 2 * math.pi * hz / sr_sym
    return [syms[i] * cmath.exp(-1j * w * i) for i in range(len(syms))]


def carrier_track(syms, kp=0.02, ki=0.0008):
    """Decision-directed second-order carrier loop, for residual drift."""
    out = []
    ph = 0.0
    freq = 0.0
    for z in syms:
        y = z * cmath.exp(-1j * ph)
        d = slice_to(y)
        if abs(d) > 0:
            e = cmath.phase(y * d.conjugate())
        else:
            e = 0.0
        freq += ki * e
        ph += kp * e + freq
        out.append(y)
    return out, freq
