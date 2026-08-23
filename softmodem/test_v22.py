"""Offline tests for the V.22 1200 bit/s modem. No hardware needed."""
import math, random, sys
import v22

FAIL = []
def check(name, cond, detail=""):
    print("  %-50s %s%s" % (name, "PASS" if cond else "FAIL", ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)

SKIP = 24   # bits discarded while the timing loop acquires (~12 symbols)

def ber(sent, got, maxoff=40, minlen=200):
    """Steady-state BER, excluding the timing-acquisition transient.

    Errors are confined to the first few symbols while the Gardner loop pulls
    in; a real modem trains before carrying data, so the meaningful figure is
    the BER after acquisition. `raw` keeps the un-skipped number for reference.
    """
    best = (1.0, 0, 0, 1.0)
    for off in range(maxoff):
        n = min(len(sent), len(got) - off)
        if n < minlen:
            break
        errs = [i for i in range(n) if sent[i] != got[off + i]]
        raw = len(errs) / n
        ss = len([i for i in errs if i >= SKIP]) / max(n - SKIP, 1)
        if ss < best[0] or (ss == best[0] and raw < best[3]):
            best = (ss, off, n, raw)
    return best

def loop(bits, channel="high", level=-18.0, noise_db=None, guard=False,
         freq_off=0.0):
    m = v22.Mod(channel, level_dbfs=level, guard_tone=guard)
    if freq_off:
        # A frequency offset means the transmit carrier sits away from where the
        # receiver looks. Multiplying the passband signal by a cosine is *not*
        # that -- it is DSB modulation, and it destroys the signal.
        m.fc += freq_off
    x = m.modulate(bits) + m.flush()
    if noise_db is not None:
        amp = 32768.0 * 10 ** (level / 20.0)
        n = amp * 10 ** (-noise_db / 20.0)
        x = [v + random.gauss(0, n) for v in x]
    d = v22.Demod(channel)
    got, syms = d.demod(x)
    return ber(bits, got), syms

print("V.22 scrambler (§5.1)")
random.seed(1)
bits = [random.randint(0, 1) for _ in range(2000)]
s, dsc = v22.Scrambler(), v22.Scrambler()
sc = s.scramble(bits)
check("scramble/descramble round-trips", dsc.descramble(sc) == bits)
check("output is balanced", 0.45 < sum(sc) / len(sc) < 0.55, "%.3f ones" % (sum(sc) / len(sc)))
z = v22.Scrambler().scramble([0] * 2000)
runs, cur = 0, 0
for b in z:
    cur = cur + 1 if b else 0
    runs = max(runs, cur)
check("all-zeros input does not produce a long ones run", runs <= 70, "longest run %d" % runs)

print("Table 1/V.22 phase mapping (§2.5.2)")
check("00 -> +90 deg", v22.DIBIT_PHASE[(0, 0)] == 90)
check("01 -> 0 deg", v22.DIBIT_PHASE[(0, 1)] == 0)
check("11 -> +270 deg", v22.DIBIT_PHASE[(1, 1)] == 270)
check("10 -> +180 deg", v22.DIBIT_PHASE[(1, 0)] == 180)

print("carrier and rate (§2.1, §2.5.1)")
check("low channel 1200 Hz, high channel 2400 Hz", v22.LOW == 1200.0 and v22.HIGH == 2400.0)
check("600 baud", v22.BAUD == 600.0)
check("13.33 samples per symbol at 8 kHz", abs(v22.SPS - 8000/600.0) < 1e-9)

print("loopback, clean")
random.seed(2)
bits = [random.randint(0, 1) for _ in range(1200)]
for ch in ("high", "low"):
    for lvl in (-12.0, -24.0, -36.0):
        (b, off, n, raw), _ = loop(bits, ch, lvl)
        check("%s channel @ %.0f dBFS: steady-state BER = 0" % (ch, lvl), b == 0.0,
              "%.5f (raw %.4f incl. acquisition) over %d bits" % (b, raw, n))

print("loopback with impairments")
# Averaged over 10 noise realisations, not one. A single seed at 15 dB SNR is a
# lottery: the same modem scores anywhere from 0.00000 to 0.00765 depending only
# on which samples the noise landed on, so a one-seed assertion against a 0.5%
# threshold passes or fails on the draw rather than on the modem. (This bit once
# already -- an earlier version of this test read 0.00000 here purely because a
# 40-sample shift in the transmitter moved the noise alignment.)
for snr in (30.0, 20.0, 15.0):
    rs = []
    for seed in range(1, 11):
        random.seed(seed)
        (b, off, n, raw), _ = loop(bits, "high", -18.0, noise_db=snr)
        rs.append(b)
    mean = sum(rs) / len(rs)
    check("%.0f dB SNR: mean steady-state BER < 0.5%%" % snr, mean < 0.005,
          "mean %.5f, worst %.5f over %d seeds" % (mean, max(rs), len(rs)))
(b, off, n, raw), _ = loop(bits, "high", -18.0, guard=True)
check("with 1800 Hz guard tone (§2.2): steady-state BER = 0", b == 0.0, "%.5f" % b)
for fo in (3.0, 7.0):
    (b, off, n, raw), _ = loop(bits, "high", -18.0, freq_off=fo)
    check("%.0f Hz offset (§2.6 allows 7): steady-state BER = 0" % fo, b == 0.0, "%.5f" % b)

print("constellation")
(b, off, n, raw), syms = loop(bits, "high", -18.0)
mags = [abs(s) for s in syms[5:-5]]
mm = sum(mags) / len(mags)
sd = math.sqrt(sum((v - mm) ** 2 for v in mags) / len(mags))
check("single-amplitude constellation (sd/mean < 5%)", sd / mm < 0.05, "%.3f" % (sd / mm))


# ---------------------------------------------------------------------------
# V.22bis 2400 bit/s: constellation from Figure 2/V.22bis
# ---------------------------------------------------------------------------
import v22bis, cmath

print("V.22bis constellation (Figure 2/V.22bis)")
check("structural self-check (both spec-implied properties)", v22bis.selfcheck())
check("16 distinct points", len(set(v22bis.POINTS.values())) == 16)
rad = {}
for (q, lab), (px, py) in v22bis.POINTS.items():
    rad.setdefault(round(abs(complex(px, py)), 4), []).append((q, lab))
rr = sorted(rad)
check("three radius rings", len(rr) == 3, "%s" % [round(r, 3) for r in rr])
check("ring populations 4 / 8 / 4", [len(rad[r]) for r in rr] == [4, 8, 4],
      str([len(rad[r]) for r in rr]))
check("ring ratios 1 : 2.236 : 3.000",
      abs(rr[1] / rr[0] - math.sqrt(5)) < 1e-3 and abs(rr[2] / rr[0] - 3.0) < 1e-3,
      "1 : %.4f : %.4f (radii were rounded when grouped)" % (rr[1] / rr[0], rr[2] / rr[0]))
r01 = [abs(complex(*v22bis.POINTS[(q, (0, 1))])) for q in (1, 2, 3, 4)]
check("the four '01' points are equal-amplitude (§2.5.2.2, V.22 compatibility)",
      max(r01) - min(r01) < 1e-9, "radius %.4f" % r01[0])
a01 = sorted((math.degrees(cmath.phase(complex(*v22bis.POINTS[(q, (0, 1))]))) + 360) % 360
             for q in (1, 2, 3, 4))
check("the four '01' points are 90 deg apart",
      all(abs(((a01[(i + 1) % 4] - a01[i]) % 360) - 90) < 1e-6 for i in range(4)),
      "%s" % [round(a, 2) for a in a01])
check("Table 1 quadrant changes match V.22's Table 1",
      v22bis.QUAD_CHANGE == {(0, 0): 90, (0, 1): 0, (1, 1): 270, (1, 0): 180})

print("V.22bis quadbit round-trip")
random.seed(11)
qb = [random.randint(0, 1) for _ in range(1600)]
syms = v22bis.encode(qb)
got = v22bis.decode(syms)
best = min(((sum(1 for i in range(min(len(qb) - o, len(got))) if qb[o + i] != got[i]), o)
            for o in (0, 2, 4)), key=lambda t: t[0])
check("noiseless encode -> decode is exact (bar the differential lead-in)",
      best[0] == 0, "%d errors, offset %d" % best)
check("all 16 points exercised", len(set((s.real, s.imag) for s in syms)) == 16)


# ---------------------------------------------------------------------------
# End-to-end: decode a real V.22bis transmission captured from the hardware
# ---------------------------------------------------------------------------
import os

print("V.22bis end-to-end against a captured hardware transmission")
CAP = "ref/v22bis_cap.raw"
if not os.path.exists(CAP):
    print("  (%s absent - hardware decode test skipped)" % CAP)
else:
    import v22bis_decode
    total_bits = 0
    worst = 1.0
    for a, b in ((6.0, 8.0), (8.0, 10.0), (10.0, 12.0), (14.0, 16.0)):
        frac, q, ref = v22bis_decode.run(CAP, a, b, verbose=False)
        total_bits += len(ref) * 4
        worst = min(worst, frac)
        check("segment %.0f-%.0f s descrambles to all ones" % (a, b), frac >= 0.999,
              "ones=%.4f, median dist to lattice %.3f" % (frac, q[0]))
    check("every segment decoded without error", worst >= 0.999,
          "worst ones fraction %.4f across ~%d bits" % (worst, total_bits))

print()
print("CMA equaliser on a synthetic distorted channel")
import equalise, v22bis as _vb, random as _r
_r.seed(5)
_bits = [_r.randint(0, 1) for _ in range(4000)]
_syms = _vb.encode(_bits)
_chan = [1.0 + 0j, 0.35 - 0.2j, -0.15 + 0.1j]
_y = []
for _i in range(len(_syms)):
    _acc = 0j
    for _k, _c in enumerate(_chan):
        if _i - _k >= 0:
            _acc += _c * _syms[_i - _k]
    _y.append(_acc * complex(math.cos(0.7), math.sin(0.7)))
_ref, _info = equalise.equalise_full(_y)
check("CMA+DD opens the eye (median dist < 0.25)", _info["median_err"] < 0.25,
      "%.3f" % _info["median_err"])
_got = _vb.decode(_ref)
_best = None
for _o in range(0, 400, 2):
    _n = min(len(_bits) - _o, len(_got))
    if _n < 1000:
        break
    _e = sum(1 for _i in range(_n) if _bits[_o + _i] != _got[_i])
    if _best is None or _e < _best[0]:
        _best = (_e, _o, _n)
check("distorted synthetic channel decodes with zero errors", _best[0] == 0,
      "%d errors in %d bits (delay %d symbols)" % (_best[0], _best[2], _best[1] // 4))


print("V.22 scrambler guard (§5.1/§5.2)")
# construct an input that forces 64 consecutive ones at the scrambler output,
# so the guard actually fires: Di[n] = 1 XOR Ds[n-14] XOR Ds[n-17]
ds = [0] * 17
di = []
for n in range(400):
    fb = ds[-14] ^ ds[-17]
    b = 1 ^ fb
    di.append(b)
    ds.append(b ^ fb)
sc = v22.Scrambler().scramble(di)
run = best = 0
for b in sc:
    run = run + 1 if b else 0
    best = max(best, run)
check("adversarial input drives a long ones run at the output", best >= 64,
      "longest run %d" % best)
check("scramble/descramble still round-trips when the guard fires",
      v22.Scrambler().descramble(sc) == di)

print()
if FAIL:
    print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
    sys.exit(1)
print("all V.22 tests passed")
