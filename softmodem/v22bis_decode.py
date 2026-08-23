"""Full V.22bis receive chain, applied to a captured modem transmission.

Order matters, and the reason is worth stating: CMA's cost function depends only
on |y|, so it is blind to a carrier frequency offset -- and an offset leaves the
modulus statistics untouched while destroying the angular structure. Run CMA
first and it reports itself converged (dispersion already at the floor) on a
constellation that is still a set of smeared rings. So the frequency offset has
to come out first.

  1. brute-force symbol timing, scored by |E[z^4]|/E[|z|^4]
  2. estimate and remove the residual carrier frequency offset
  3. CMA to open the eye
  4. scale to the lattice and fix rotation modulo 90 degrees
  5. decision-directed carrier tracking for residual drift
  6. DD-LMS refinement
  7. quadbit decode, then descramble -- the modem is sending scrambled binary 1,
     so a correct chain descrambles to all ones, and the descrambler is
     self-synchronising so its state need not be known
"""
import math, sys
import g711, v22, v22bis, v22bis_const as C, equalise


def run(path, a, b, carrier=v22.LOW, taps=11, verbose=True):
    x = g711.decode(open(path, "rb").read(), 8)
    seg = x[int(a * 8000):int(b * 8000)]
    syms, ph, rot, sc = C.extract(seg, carrier)
    steps = [("raw", syms)]
    hz, _ = equalise.estimate_freq_offset(syms)
    syms = equalise.derotate(syms, hz)
    steps.append(("de-rotated", syms))
    eq, disp, w = equalise.cma(syms, taps=taps)
    steps.append(("after CMA", eq))
    al, r2 = equalise.align(eq)
    best = None
    for extra in (0.0, math.pi / 4):
        cand = [v * complex(math.cos(extra), math.sin(extra)) for v in al]
        tr, ferr = equalise.carrier_track(cand)
        ref, mse, _ = equalise.dd_lms(tr, w=[1.0 + 0j if k == taps // 2 else 0j
                                             for k in range(taps)], taps=taps)
        bits = v22bis.decode(ref)
        d = v22.Scrambler().descramble(bits)[64:]
        frac = sum(d) / len(d) if d else 0.0
        if best is None or frac > best[0]:
            best = (frac, extra, ref, mse, len(d))
    frac, extra, ref, mse, nbits = best
    q = equalise.quality(ref)
    if verbose:
        print("  segment %.0f-%.0f s: %d symbols, offset %+.3f Hz, m4 %.3f" % (a, b, len(syms), hz, sc))
        print("    dispersion %.1f -> %.1f (floor 42.24), DD MSE %.3f -> %.3f" % (disp[0], disp[-1], mse[0], mse[-1]))
        print("    median dist to lattice %.3f, 90th pct %.3f (boundary 1.0)" % q)
        print("    descrambled ones: %.4f over %d bits  %s"
              % (frac, nbits, "<== CLEAN" if frac > 0.98 else ("good" if frac > 0.9 else "")))
    return frac, q, ref


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "ref/v22bis_cap.raw"
    for a, b in ((6.0, 8.0), (8.0, 10.0), (10.0, 12.0), (14.0, 16.0)):
        run(path, a, b)
