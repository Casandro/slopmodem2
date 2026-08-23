"""Offline tests for V.32: constellation, coding, scramblers, modem, signals.

The PDF's OCR of Table 3 strips every sign, so the constellation cannot simply be
read off; it is derived from Figure 1's labels and then checked four ways. §5.2.3
is the gift of this Recommendation: it publishes the first fifteen dibits and
signal states of the TRN segment for *both* scrambler polynomials, which is a
ready-made test vector for the scrambler, its register convention, the bit order
and the A/C mapping all at once.
"""
import math, random, sys
import dsp, dte, tracking, v22, v22bis, v32

FAIL = []


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def band(seg, f, half=60, step=10):
    fs = [f + d for d in range(-half, half + 1, step)]
    return sum(dsp.goertzel(seg, v) for v in fs) / len(fs)


def run_link(mode, bps, frames=1500, level=-24.0, taps=None, noise_db=None,
             pattern=b"V32DATA "):
    taps = taps or v32.Scrambler.GPC
    enc = dte.AsyncEncoder(idle=2)
    enc.put(pattern * 6000)
    m = v32.Mod(level_dbfs=level, scrambler_taps=taps)
    live = tracking.LiveRx(carrier=v32.CARRIER, mode=mode, sps=v32.SPS,
                           baud=v32.BAUD, beta=v32.ROLLOFF, span=10,
                           descrambler=v32.Scrambler(taps))
    # SNR against the *measured* signal power, not the level label: the label
    # sits 10*log10(SPS) = 5.2 dB above the actual RMS (see v32.Mod), so using it
    # would understate the noise by that much and mislabel every result.
    nz = 0.0
    if noise_db is not None:
        ref = v32.Mod(level_dbfs=level, scrambler_taps=taps)
        probe = ref.modulate([random.randint(0, 1) for _ in range(48 * bps * 200)],
                             bps=bps) + ref.flush()
        nz = math.sqrt(dsp.mean_square(probe)) * 10 ** (-noise_db / 20.0)
    for _ in range(frames):
        f = m.modulate(enc.take(48 * bps), bps=bps)
        if nz:
            f = [v + int(random.gauss(0, nz)) for v in f]
        live.feed(f)
    got = bytes(live.data)
    hits = max(sum(1 for k, c in enumerate(got)
                   if c == pattern[(k + p) % len(pattern)])
               for p in range(len(pattern))) if got else 0
    return got, hits, live.summary()


if __name__ == "__main__":
    print("carrier, rate and frame arithmetic (2.1, 2.3)")
    check("carrier 1800 Hz, 2400 baud", v32.CARRIER == 1800.0 and v32.BAUD == 2400.0)
    check("a 160-sample RTP frame is exactly 48 symbols",
          abs(160 / v32.SPS - 48) < 1e-12, "%.6f" % (160 / v32.SPS))
    lut, per = tracking.carrier_lut(v32.CARRIER)
    check("the 1800 Hz carrier table is exactly 40 samples, dividing 160",
          per == 40 and 160 % per == 0, "period %d" % per)
    check("48 symbols is 192 bits at 9600 and 96 at 4800",
          48 * 4 == 192 and 48 * 2 == 96)

    print()
    print("constellation, from Figure 1 and cross-checked against Table 3")
    check("structural self-check (16 distinct points, four per quadrant, "
          "90 deg label invariance, Table 1 = its rotations)", v32.selfcheck())
    check("mean power 10, the same lattice V.22bis uses",
          abs(sum(abs(complex(*p)) ** 2 for p in v32.NONRED.values()) / 16
              - 10.0) < 1e-12)
    check("it *is* the V.22bis point set, differently labelled",
          {complex(*p) for p in v32.NONRED.values()}
          == {complex(*p) for p in v22bis.POINTS.values()})
    tab3 = {(0, 0): ((1, 1), (3, 1), (1, 3), (3, 3)),
            (0, 1): ((1, 1), (1, 3), (3, 1), (3, 3)),
            (1, 0): ((1, 1), (1, 3), (3, 1), (3, 3)),
            (1, 1): ((1, 1), (3, 1), (1, 3), (3, 3))}
    okall = True
    for yy in ((0, 0), (0, 1), (1, 0), (1, 1)):
        mine = tuple(tuple(abs(v) for v in v32.NONRED[(yy[0], yy[1], a, b)])
                     for a, b in ((0, 0), (0, 1), (1, 0), (1, 1)))
        okall = okall and mine == tab3[yy]
    check("all four Table 3 rows match by magnitude (the OCR lost the signs)",
          okall)
    check("2.4.2 subset: A=(-1,-1) B=(1,-1) C=(1,1) D=(-1,1)",
          v32.ABCD == {"A": (-1, -1), "B": (1, -1), "C": (1, 1), "D": (-1, 1)},
          "%s" % v32.ABCD)

    print()
    print("scramblers (4, 4.1.1) against the TRN vectors published in 5.2.3")
    for name, taps, want_d, want_s in (
            ("GPC", v32.Scrambler.GPC,
             "11 11 11 11 11 11 11 11 11 00 00 01 11 11 11",
             "CCCCCCCCCAAACCC"),
            ("GPA", v32.Scrambler.GPA,
             "11 11 10 00 00 11 11 10 00 00 11 10 01 11 11",
             "CCCAACCCAACCACC")):
        out = v32.Scrambler(taps).scramble([1] * 30)
        got_d = " ".join("%d%d" % (out[i], out[i + 1]) for i in range(0, 30, 2))
        got_s = "".join(v32.trn_states(15, taps))
        check("%s reproduces the published dibits" % name, got_d == want_d, got_d)
        check("%s reproduces the published signal states" % name,
              got_s == want_s, got_s)
    check("the two polynomials really differ (4: each direction its own)",
          v32.Scrambler.GPC != v32.Scrambler.GPA,
          "GPC 1+x^-18+x^-23, GPA 1+x^-5+x^-23")
    for taps in (v32.Scrambler.GPC, v32.Scrambler.GPA):
        random.seed(4)
        b = [random.randint(0, 1) for _ in range(4000)]
        rt = v32.Scrambler(taps).descramble(v32.Scrambler(taps).scramble(b))
        check("scramble/descramble round-trips (%s)"
              % ("GPC" if taps == v32.Scrambler.GPC else "GPA"),
              rt[max(taps):] == b[max(taps):], "past the %d-bit fill" % max(taps))

    print()
    print("transmitted spectrum (2.2) and level")
    random.seed(1)
    m = v32.Mod()
    x = m.modulate([random.randint(0, 1) for _ in range(192 * 400)], bps=4) \
        + m.flush()
    lvl = 10 * math.log10(dsp.mean_square(x) / 32768.0 ** 2)
    seg = x[8000:40000]
    peak = max(band(seg, f) for f in range(660, 2941, 60))
    d600 = 10 * math.log10(peak / band(seg, 600.0))
    d3000 = 10 * math.log10(peak / band(seg, 3000.0))
    check("2.2: 600 Hz attenuated 4.5 +/- 2.5 dB", abs(d600 - 4.5) <= 2.5,
          "%.1f dB" % d600)
    check("2.2: 3000 Hz attenuated 4.5 +/- 2.5 dB", abs(d3000 - 4.5) <= 2.5,
          "%.1f dB" % d3000)
    check("level label follows v22.Mod's convention (actual + 10log10(SPS))",
          abs(lvl - (-24.0 - 10 * math.log10(v32.SPS))) < 0.2,
          "label -24.0 -> actual %.2f dBFS" % lvl)
    b = v22bis.Mod("high", level_dbfs=-18.0)
    xb = b.modulate([random.randint(0, 1) for _ in range(4800)]) + b.flush()
    lb = 10 * math.log10(dsp.mean_square(xb) / 32768.0 ** 2)
    check("V.32 at -24 puts the same power on the wire as V.22bis at -18",
          abs(lvl - lb) < 0.3, "%.2f vs %.2f dBFS" % (lvl, lb))

    print()
    print("pulse shaping: the fraction has to be exact at 3.333 samples/symbol")
    # Rounding each symbol centre to the nearest sample is 3.75% of a symbol at
    # V.22's 13.333 sps and measured harmless there, but 15% at V.32's 3.333.
    check("SPS is exactly 10/3, so there are only three sub-phases",
          abs(v32.SPS - 10.0 / 3.0) < 1e-12
          and len({round((k * v32.SPS) % 1, 9) for k in range(60)}) == 3,
          "phases %s" % sorted({round((k * v32.SPS) % 1, 3) for k in range(60)}))
    got, hits, q = run_link(v32.QAM9600, 4, frames=400)
    check("that exactness shows up as a clean eye, not just in theory",
          hits == len(got) and len(got) > 2000,
          "%d/%d characters" % (hits, len(got)))

    print()
    print("data phase, our modulator into our receiver")
    for mode, bps, name in ((v32.QAM9600, 4, "9600 bit/s, 16-point nonredundant"),
                            (v32.QPSK4800, 2, "4800 bit/s, ABCD subset")):
        got, hits, q = run_link(mode, bps)
        check("%s: every character correct" % name,
              got and hits == len(got),
              "%d/%d, acq@%d, %d retrain(s), %d framing errors, %.2f ms/frame"
              % (hits, len(got), q["acquired_at"], q["retrains"],
                 q["framing_bad"], q["mean_ms"]))
    check("4800 bit/s is constant modulus, so its CMA floor is zero",
          abs(v32.QPSK4800.r2 - v32.QPSK4800.power) < 1e-12
          and v32.QAM9600.r2 > v32.QAM9600.power,
          "4800 r2=%.2f power=%.2f; 9600 r2=%.2f power=%.2f"
          % (v32.QPSK4800.r2, v32.QPSK4800.power,
             v32.QAM9600.r2, v32.QAM9600.power))

    print()
    print("with noise")
    for snr, lim in ((24.0, 0.0), (18.0, 0.01)):
        random.seed(9)
        got, hits, q = run_link(v32.QAM9600, 4, frames=600, noise_db=snr)
        r = 1.0 - hits / max(len(got), 1) if got else 1.0
        check("9600 bit/s at %.0f dB SNR: character errors <= %.0f%%"
              % (snr, 100 * lim), got and r <= lim,
              "%.4f%% wrong of %d" % (100 * r, len(got)))

    print()
    print("Figure 3: the 32-point trellis constellation, reconstructed")
    t = v32.TRELLIS9600
    check("exactly 32 points", len(t.points) == 32, "%d" % len(t.points))
    check("every point has Re+Im odd, which is what the readable Table 3 "
          "magnitudes all share",
          all((int(round(z.real)) + int(round(z.imag))) % 2 for z in t.points))
    check("radii are 1, sqrt5, 3, sqrt13, sqrt17",
          sorted({round(abs(z) ** 2) for z in t.points}) == [1, 5, 9, 13, 17],
          "%s" % sorted({round(abs(z) ** 2) for z in t.points}))
    check("ring populations 4 / 8 / 4 / 8 / 8",
          [sum(1 for z in t.points if round(abs(z) ** 2) == r)
           for r in (1, 5, 9, 13, 17)] == [4, 8, 4, 8, 8])
    check("mean power 10, the same as the 16-point alternative -- equal "
          "transmitted power at both", abs(t.power - 10.0) < 1e-9
          and abs(t.power - v32.QAM9600.power) < 1e-9, "%.1f" % t.power)
    frag = [(1, 0), (0, 1), (1, 2), (2, 1), (0, 3), (3, 0), (2, 3), (3, 2),
            (1, 4), (4, 1)]
    have = {(abs(int(round(z.real))), abs(int(round(z.imag)))) for z in t.points}
    check("every magnitude the OCR left of Table 3's trellis column is present",
          all(f in have for f in frag))
    check("and it is a different set from the nonredundant one",
          {complex(*p) for p in v32.NONRED.values()} != set(t.points))

    print()
    print("conformance against the printed data in V.32 itself")
    # 5.2.3 prints the first fifteen TRN signal states for each mode. This is
    # the only end-to-end vector the Recommendation gives for the transmitter,
    # and it exercises the scrambler, the "first bit of each dibit" rule and the
    # A/C mapping in one line.
    for mode, taps, want in (("call", v32.Scrambler.GPC, "CCCCCCCCCAAACCC"),
                             ("answer", v32.Scrambler.GPA, "CCCAACCCAACCACC")):
        got = "".join(v32.trn_states(15, taps))
        check("5.2.3's printed TRN vector for %s mode" % mode, got == want,
              "spec %s, ours %s" % (want, got))
    # Table 2/V.32, transcribed from the rendered page rather than from V.32 bis
    T2 = ((0,0,0,0,0,0),(0,0,0,1,0,1),(0,0,1,0,1,0),(0,0,1,1,1,1),
          (0,1,0,0,0,1),(0,1,0,1,0,0),(0,1,1,0,1,1),(0,1,1,1,1,0),
          (1,0,0,0,1,0),(1,0,0,1,1,1),(1,0,1,0,0,1),(1,0,1,1,0,0),
          (1,1,0,0,1,1),(1,1,0,1,1,0),(1,1,1,0,0,0),(1,1,1,1,0,1))
    bad = [r for r in T2 if v32.TABLE2[r[:4]] != r[4:]]
    check("Table 2/V.32 agrees with Table 1/V.32 bis on all 16 rows",
          not bad, "%d disagreements" % len(bad))
    # Table 3/V.32, both columns, transcribed from the rendered page. This is
    # the authoritative numeric source for the mapping -- Figure 3 is dots on a
    # diagram, Table 3 is coordinates -- and it had never been read directly:
    # the sign-stripped OCR of it is what sent this whole investigation to
    # V.32 bis in the first place.
    T3T = {(0,0,0,0,0):(-4,1),(0,0,0,0,1):(0,-3),(0,0,0,1,0):(0,1),
           (0,0,0,1,1):(4,1),(0,0,1,0,0):(4,-1),(0,0,1,0,1):(0,3),
           (0,0,1,1,0):(0,-1),(0,0,1,1,1):(-4,-1),(0,1,0,0,0):(-2,3),
           (0,1,0,0,1):(-2,-1),(0,1,0,1,0):(2,3),(0,1,0,1,1):(2,-1),
           (0,1,1,0,0):(2,-3),(0,1,1,0,1):(2,1),(0,1,1,1,0):(-2,-3),
           (0,1,1,1,1):(-2,1),(1,0,0,0,0):(-3,-2),(1,0,0,0,1):(1,-2),
           (1,0,0,1,0):(-3,2),(1,0,0,1,1):(1,2),(1,0,1,0,0):(3,2),
           (1,0,1,0,1):(-1,2),(1,0,1,1,0):(3,-2),(1,0,1,1,1):(-1,-2),
           (1,1,0,0,0):(1,4),(1,1,0,0,1):(-3,0),(1,1,0,1,0):(1,0),
           (1,1,0,1,1):(1,-4),(1,1,1,0,0):(-1,-4),(1,1,1,0,1):(3,0),
           (1,1,1,1,0):(-1,0),(1,1,1,1,1):(-1,4)}
    T3N = {(0,0,0,0):(-1,-1),(0,0,0,1):(-3,-1),(0,0,1,0):(-1,-3),
           (0,0,1,1):(-3,-3),(0,1,0,0):(1,-1),(0,1,0,1):(1,-3),
           (0,1,1,0):(3,-1),(0,1,1,1):(3,-3),(1,0,0,0):(-1,1),
           (1,0,0,1):(-1,3),(1,0,1,0):(-3,1),(1,0,1,1):(-3,3),
           (1,1,0,0):(1,1),(1,1,0,1):(3,1),(1,1,1,0):(1,3),(1,1,1,1):(3,3)}
    off = [k for k, v in T3T.items() if v32.TRELLIS_MAP[k] != complex(*v)]
    check("Table 3/V.32's trellis column, all 32 rows off the rendered page",
          len(T3T) == 32 and not off, "%d disagree" % len(off))
    offn = [k for k, v in T3N.items() if v32.NONRED[k] != v]
    check("Table 3/V.32's nonredundant column, all 16 rows",
          len(T3N) == 16 and not offn, "%d disagree" % len(offn))
    # Figure 3/V.32, transcribed from the rendered page: row by row, Im = 4..-4
    F3 = {4: ((-1,"11111"),(1,"11000")),
          3: ((-2,"01000"),(0,"00101"),(2,"01010")),
          2: ((-3,"10010"),(-1,"10101"),(1,"10011"),(3,"10100")),
          1: ((-4,"00000"),(-2,"01111"),(0,"00010"),(2,"01101"),(4,"00011")),
          0: ((-3,"11001"),(-1,"11110"),(1,"11010"),(3,"11101")),
          -1: ((-4,"00111"),(-2,"01001"),(0,"00110"),(2,"01011"),(4,"00100")),
          -2: ((-3,"10000"),(-1,"10111"),(1,"10001"),(3,"10110")),
          -3: ((-2,"01110"),(0,"00001"),(2,"01100")),
          -4: ((-1,"11100"),(1,"11011"))}
    n = bad = 0
    for im, row in F3.items():
        for re_, lab in row:
            n += 1
            key = tuple(int(c) for c in lab)
            if v32.TRELLIS_MAP.get(key) != complex(re_, im):
                bad += 1
    check("Figure 3/V.32 agrees with Figure 2-3/V.32 bis on all 32 points",
          n == 32 and bad == 0, "%d of %d disagree" % (bad, n))
    # 2.2: with continuous ones into the scrambler, 600 and 3000 Hz shall be
    # 4.5 +/- 2.5 dB below the peak energy density in that band
    mm = v32.Mod(level_dbfs=-19.0)
    sig = []
    for _ in range(120):
        sig.extend(mm.shape(mm.symbols([1] * 400, bps=4, trellis=True)))
    sig = sig[4000:]

    def dens(f, nfft=400):
        tot = 0.0
        cnt = 0
        for i in range(0, len(sig) - nfft, nfft // 2):
            tot += dsp.goertzel(sig[i:i + nfft], f)
            cnt += 1
        return tot / max(cnt, 1)
    peak = max(dens(float(f)) for f in range(600, 3001, 60))
    for f in (600.0, 3000.0):
        att = 10 * math.log10(peak / dens(f))
        check("2.2: %d Hz is %.2f dB below the in-band peak" % (f, att),
              2.0 <= att <= 7.0, "spec 4.5 +/- 2.5 dB")

    print()
    print("2.4.1.2 trellis coding: the labelling, from V.32 bis")
    import os
    import random as _r
    # Figure 2-3/V.32 bis, parsed geometrically: 32 labels, 32 points
    check("all 32 five-bit labels are distinct",
          len(v32.TRELLIS_MAP) == 32 and len(set(v32.TRELLIS_MAP.values())) == 32)
    check("and they are exactly the constellation reconstructed from Table 3's "
          "surviving magnitudes",
          set(v32.TRELLIS_MAP.values()) == set(v32.TRELLIS_POINTS))
    # Table 3/V.32 lost its signs but kept its rows: cross-check every magnitude
    T3 = ((4,1),(0,3),(0,1),(4,1), (4,1),(0,3),(0,1),(4,1),
          (2,3),(2,1),(2,3),(2,1), (2,3),(2,1),(2,3),(2,1),
          (3,2),(1,2),(3,2),(1,2), (3,2),(1,2),(3,2),(1,2),
          (1,4),(3,0),(1,0),(1,4), (1,4),(3,0),(1,0),(1,4))
    mism = 0
    for i, lab in enumerate(sorted(v32.TRELLIS_MAP)):
        z = v32.TRELLIS_MAP[lab]
        if (abs(z.real), abs(z.imag)) != T3[i]:
            mism += 1
    check("every one of the 32 magnitudes agrees with Table 3/V.32, so the two "
          "documents confirm each other", mism == 0, "%d mismatches" % mism)
    # the partition
    blocks = {}
    for lab, z in v32.TRELLIS_MAP.items():
        blocks.setdefault(lab[:3], []).append(z)
    check("each (Y0,Y1,Y2) block holds 4 points",
          sorted(len(v) for v in blocks.values()) == [4] * 8)
    check("and each is a single coset of 4Z2, so the coded bits pick the subset "
          "and Q3Q4 picks inside it",
          all(len({v32.trellis_subset(z) for z in v}) == 1
              for v in blocks.values()))
    check("the 8 subsets are the 8 distinct cosets",
          len({v32.trellis_subset(v[0]) for v in blocks.values()}) == 8)
    # rotational invariance, which is the whole reason for Table 2
    rot = lambda z: complex(-z.imag, z.real)
    L = v32.TRELLIS_LABEL
    check("a 90 degree rotation leaves Q3Q4 alone",
          all(L[rot(z)][3:] == L[z][3:] for z in v32.TRELLIS_MAP.values()))
    ind = {}
    for z in v32.TRELLIS_MAP.values():
        ind.setdefault(L[z][1:3], set()).add(L[rot(z)][1:3])
    check("and shifts Y1Y2 by a single well-defined permutation",
          all(len(v) == 1 for v in ind.values()))
    ind = {k: next(iter(v)) for k, v in ind.items()}
    check("which is exactly Table 2's Q1Q2 = 11 row: one rotation is one "
          "quadrant of differential input",
          all(v32.TABLE2[(1, 1) + k] == ind[k] for k in ind))
    check("Table 2 is a permutation of Y1Y2 for each Q1Q2, and Q1Q2 = 00 is the "
          "identity",
          all(len({v32.TABLE2[(p, q, a, b)] for a in (0, 1) for b in (0, 1)}) == 4
              for p in (0, 1) for q in (0, 1))
          and all(v32.TABLE2[(0, 0, a, b)] == (a, b)
                  for a in (0, 1) for b in (0, 1)))
    check("and its inverse recovers Q1Q2 from two consecutive Y1Y2",
          all(v32.diff_decode((a, b), v32.diff_encode(p, q, (a, b))) == (p, q)
              for p in (0, 1) for q in (0, 1)
              for a in (0, 1) for b in (0, 1)))

    print()
    print("the convolutional encoder as Figure 1 draws it")
    _r.seed(3)
    prs0 = [(_r.randint(0, 1), _r.randint(0, 1)) for _ in range(50000)]
    fsr = v32.TrellisFSR()
    drawn = [fsr.step(a, b) for a, b in prs0]
    h1, h2, h0 = [0, 0, 0], [0, 0, 0], [0, 0, 0]
    fit = []
    for a, b in prs0:
        y = v32.trellis_y0((h1, h2, h0))
        h1, h2, h0 = h1[1:] + [a], h2[1:] + [b], h0[1:] + [y]
        fit.append(y)
    check("the drawn circuit and the fitted GF(2) relation are the same code",
          drawn == fit, "%d differ in %d"
          % (sum(1 for a, b in zip(drawn, fit) if a != b), len(prs0)))
    st = 0
    tb = []
    for a, b in prs0:
        y, st = v32.trellis_step(st, a, b)
        tb.append(y)
    check("and so is the minimised 8-state table", drawn == tb,
          "%d differ" % sum(1 for a, b in zip(drawn, tb) if a != b))
    check("it is a Moore machine: three delays, and Y0 is one of their outputs",
          all(v32.TrellisFSR(a, b, c).step(x, y) == c
              for a in (0, 1) for b in (0, 1) for c in (0, 1)
              for x in (0, 1) for y in (0, 1)))

    print()
    print("the convolutional encoder, and its 8 states")
    check("Y0 uses no current input, so the encoder is a Moore machine",
          all(len({v32.trellis_step(s, a, b)[0]
                   for a in (0, 1) for b in (0, 1)}) == 1 for s in range(8)))
    check("minimising the fitted recursion gives 8 states, as Figure 2 has",
          len(v32.TRELLIS_TABLE) == 8)
    check("every state has 4 distinct successors",
          all(len({n for _, n in row}) == 4 for row in v32.TRELLIS_TABLE))
    pre = {}
    for st, row in enumerate(v32.TRELLIS_TABLE):
        for _, n in row:
            pre.setdefault(n, set()).add(st)
    check("and 4 distinct predecessors, so the trellis is fully connected",
          all(len(pre[s]) == 4 for s in range(8)))
    # the explicit recursion and the minimised table must be the same code
    _r.seed(7)
    prs = [(_r.randint(0, 1), _r.randint(0, 1)) for _ in range(20000)]
    h1, h2, h0 = [0, 0, 0], [0, 0, 0], [0, 0, 0]
    seq = []
    for a, b in prs:
        y0 = v32.trellis_y0((h1, h2, h0))
        h1 = h1[1:] + [a]
        h2 = h2[1:] + [b]
        h0 = h0[1:] + [y0]
        seq.append(y0)
    st = 0
    tseq = []
    for a, b in prs:
        y0, st = v32.trellis_step(st, a, b)
        tseq.append(y0)
    # both start from the all-zero delay line, so they agree from symbol 0
    check("the explicit recursion and the 8-state table are the same code",
          seq == tseq, "%d differ" % sum(1 for a, b in zip(seq, tseq) if a != b))

    print()
    print("against real captured symbols")
    FIX = "ref/v32_trellis_symbols.txt"
    if not os.path.exists(FIX):
        print("  (%s absent - the measured checks are skipped)" % FIX)
    else:
        pts = []
        for line in open(FIX):
            if line.startswith("#") or not line.strip():
                continue
            a, b = line.split()
            pts.append(complex(int(a), int(b)))
        check("fixture loaded from a real modem-to-modem connection",
              len(pts) > 4000, "%d symbols" % len(pts))
        check("every captured symbol is a labelled constellation point",
              all(z in v32.TRELLIS_LABEL for z in pts))
        labs = [v32.TRELLIS_LABEL[z] for z in pts]
        y0 = [l[0] for l in labs]
        y1 = [l[1] for l in labs]
        y2 = [l[2] for l in labs]
        bad = 0
        for n in range(3, len(labs)):
            if v32.trellis_y0((y1[:n], y2[:n], y0[:n])) != y0[n]:
                bad += 1
        check("the encoder reproduces Y0 on every captured symbol",
              bad == 0, "%d mismatches in %d" % (bad, len(labs) - 3))
        lin = 0
        for n in range(3, len(labs)):
            v = y2[n-1] ^ y1[n-2] ^ y2[n-2] ^ y0[n-3]
            if v != y0[n]:
                lin += 1
        # the drawn circuit has feedback from its own output, so a wrong start
        # state never recovers: exactly one of the 8 must reproduce the stream
        ok = []
        for t1 in (0, 1):
            for t2 in (0, 1):
                for t3 in (0, 1):
                    f = v32.TrellisFSR(t1, t2, t3)
                    n = 0
                    for l in labs:
                        if f.step(l[1], l[2]) != l[0]:
                            n += 1
                            if n > 3:
                                break
                    if n == 0:
                        ok.append((t1, t2, t3))
        check("exactly one initial state of Figure 1's three delays reproduces "
              "every captured symbol", len(ok) == 1, "states that work: %s" % ok)
        check("dropping the AND terms breaks it -- the nonlinear gate is real, "
              "as rotational invariance requires", lin > 0.2 * (len(labs) - 3),
              "%d of %d wrong without them" % (lin, len(labs) - 3))
        subs = [v32.trellis_subset(z) for z in pts]
        worst = max(abs(subs.count(s) / len(subs) - 0.125) for s in set(subs))
        check("the 8 subsets are used uniformly", worst < 0.01,
              "worst deviation %.2f%%" % (100 * worst))
        # 750 symbols per subset: a point's share has a standard error of
        # sqrt(.25*.75/750) = 1.6%, so 5% is 3 sigma, not a fudged threshold
        ins = []
        for s in set(subs):
            grp = [z for z in pts if v32.trellis_subset(z) == s]
            ins.append(max(abs(grp.count(q) / len(grp) - 0.25) for q in set(grp)))
        check("and so are the 4 points inside each -- Q3Q4 is uncoded",
              max(ins) < 0.05,
              "worst %.1f%% (3 sigma = 4.7%%)" % (100 * max(ins)))

    print()
    print("the Viterbi decoder")
    d2 = v32.trellis_free_distance()
    u2 = min(abs(complex(*p) - complex(*q)) ** 2
             for i, p in enumerate(v32.NONRED.values())
             for q in list(v32.NONRED.values())[i + 1:])
    check("the code's free distance is 10", abs(d2 - 10.0) < 1e-9,
          "d^2 = %.3f" % d2)
    check("the uncoded 16-point set's minimum distance is 4",
          abs(u2 - 4.0) < 1e-9, "d^2 = %.3f" % u2)
    pw = (sum(x * x + y * y for x, y in v32.NONRED.values()) / 16.0,
          sum(abs(z) ** 2 for z in v32.TRELLIS_POINTS) / 32.0)
    check("both constellations have mean power 10, so the ratio *is* the gain",
          abs(pw[0] - 10) < 1e-9 and abs(pw[1] - 10) < 1e-9,
          "%.3f / %.3f" % pw)
    gain = 10 * math.log10(d2 / u2)
    check("=> asymptotic coding gain 3.98 dB, which is V.32's 4 dB",
          3.9 < gain < 4.05, "%.2f dB" % gain)
    within = [min(abs(a - b) ** 2
                  for i, a in enumerate(v32.TRELLIS_SUBSETS[k])
                  for b in list(v32.TRELLIS_SUBSETS[k])[i + 1:])
              for k in v32.SUBSET_KEYS]
    check("points inside a subset are d^2 = 16 apart, which is what lets the "
          "uncoded pair ride unprotected", set(within) == {16.0})
    across = min(abs(complex(*a) - complex(*b)) ** 2
                 for i in range(8) for j in range(8) if i != j
                 for a in v32.SUBSET_XY[i] for b in v32.SUBSET_XY[j])
    check("different subsets come as close as d^2 = 2, so a single symbol "
          "cannot be decided alone", abs(across - 2.0) < 1e-9)

    print()
    print("the full 9600 bit/s trellis data path")
    _r.seed(4)
    bits = [_r.randint(0, 1) for _ in range(40000)]
    pts2 = v32.TrellisEncoder().encode(bits)
    check("4 bits per symbol", len(pts2) == len(bits) // 4)
    got = v32.trellis_decode_bits(pts2)
    off = len(bits) - len(got)
    check("the differential step costs exactly one symbol", off == 4,
          "%d bits held back" % off)
    err = sum(1 for a, b in zip(bits[off:], got) if a != b)
    check("encode -> decode is bit exact", err == 0,
          "%d errors in %d bits" % (err, len(got)))
    base = v32.trellis_decode_bits(pts2)
    same = all(v32.trellis_decode_bits([z * (1j ** k) for z in pts2]) == base
               for k in (1, 2, 3))
    check("and an unresolved 90/180/270 degree phase gives identical data -- "
          "this is what Table 2 and the nonlinear gate are for", same)

    if os.path.exists(FIX):
        dec, rep = v32.trellis_decode(pts)
        check("on the captured symbols the decoder agrees with the hard "
              "decisions", rep == 0, "%d repairs over %d real symbols"
              % (rep, len(pts)))
        # inject single errors: correctable while d^2 < d2free/4 = 2.5
        res = []
        for amp in (0.9, 1.8):
            _r.seed(5)
            hits = sorted(_r.sample(range(100, len(pts) - 100), 300))
            bad2 = list(pts)
            for i in hits:
                bad2[i] += complex(amp, amp)
            dd, _ = v32.trellis_decode(bad2)
            fixed = sum(1 for i in hits if dd[i][5] == pts[i])
            hard2 = sum(1 for i in hits
                        if min(v32.TRELLIS_POINTS,
                               key=lambda p: abs(p - bad2[i])) == pts[i])
            res.append((2 * amp * amp, fixed, hard2))
        check("it repairs isolated errors inside d^2free/4 = 2.5 that a hard "
              "decision loses", res[0][1] > 290 and res[0][2] < 120,
              "d^2 %.2f: trellis %d/300, hard %d/300" % res[0])
        check("and outside that radius it degrades, as it must", res[1][1] < 200,
              "d^2 %.2f: trellis %d/300, hard %d/300" % res[1])

        # the end of the whole chain: real audio -> plaintext
        raw = v32.trellis_decode_bits(pts)
        de = [0] * len(raw)
        for n in range(len(raw)):
            v = raw[n]
            if n >= 18:
                v ^= raw[n - 18]
            if n >= 23:
                v ^= raw[n - 23]
            de[n] = v
        i, chars, rej = 0, [], 0
        while i < len(de) - 10:
            if de[i] == 1:
                i += 1
                continue
            if de[i + 9] != 1:
                rej += 1
                i += 1
                continue
            chars.append(sum(de[i + 1 + k] << k for k in range(8)))
            i += 10
        txt = "".join(chr(c) if 32 <= c < 127 else "." for c in chars)
        check("descrambling with GPC and framing per V.14 gives printable text",
              sum(1 for c in chars if 32 <= c < 127) > 0.98 * len(chars),
              "%d chars, %.1f%% printable, %d framing rejects"
              % (len(chars), 100.0 * sum(1 for c in chars if 32 <= c < 127)
                 / max(len(chars), 1), rej))
        check("and the text is what the modem was actually sending",
              txt.count("AAA2BBB ") > 50,
              "%d repeats of 'AAA2BBB '" % txt.count("AAA2BBB "))
    print()
    print("V.32bis: 7200, 12 000 and 14 400 bit/s")
    # 2.3.1-2.3.4 are the same four sentences with a different bit count each
    # time, so every added rate is a constellation and nothing else. The check
    # that matters is structural: a mis-parsed figure does not accidentally have
    # 8 equal subsets, closure under rotation, the uncoded bits preserved, and an
    # induced Y1Y2 shift equal to Table 1's Q1Q2 = 11 row.
    for rate, npts, power in ((7200, 16, 10.0), (9600, 32, 10.0),
                              (12000, 64, 42.0), (14400, 128, 41.0)):
        ts = v32.TRELLIS_SETS[rate]
        check("%d: %d points at mean power %.0f" % (rate, npts, power),
              len(ts.points) == npts and abs(ts.power - power) < 1e-9,
              "%d points, power %.3f" % (len(ts.points), ts.power))
        check("  8 subsets of %d, so 3 coded bits and %d uncoded"
              % (npts // 8, ts.uncoded),
              sorted(len(v) for v in ts.subsets.values()) == [npts // 8] * 8
              and ts.uncoded == ts.nbits - 3)
        P = set(ts.points)
        rot = lambda z: complex(-z.imag, z.real)
        closed = all(rot(z) in ts.label for z in P)
        check("  closed under 90 degree rotation", closed)
        if closed:
            check("  rotation preserves the uncoded bits",
                  all(ts.label[rot(z)][3:] == ts.label[z][3:] for z in P))
            ind = {}
            for z in P:
                ind.setdefault(ts.label[z][1:3], set()).add(ts.label[rot(z)][1:3])
            ok = all(len(v) == 1 for v in ind.values())
            if ok:
                ind = {k: next(iter(v)) for k, v in ind.items()}
                ok = all(v32.TABLE2[(1, 1) + k] == ind[k] for k in ind)
            check("  and shifts Y1Y2 by exactly Table 1's Q1Q2 = 11 row", ok)
    check("the 9600 set rebuilt through TrellisSet matches the tables verified "
          "against V.32's own Table 3",
          all(v32.TRELLIS_9600T.map[k] == v for k, v in v32.TRELLIS_MAP.items()))

    print()
    print("V.32bis: one encoder and one Viterbi for all four rates")
    for rate in (7200, 9600, 12000, 14400):
        ts = v32.TRELLIS_SETS[rate]
        n = ts.nbits - 1
        _r.seed(rate)
        bits = [_r.randint(0, 1) for _ in range(n * 2500)]
        pts = v32.TrellisEncoder(ts).encode(bits)
        got = v32.trellis_decode_bits(pts, ts=ts)
        off = len(bits) - len(got)
        err = sum(1 for a, b in zip(bits[off:], got) if a != b)
        check("%d: %d data bits per symbol, encode -> decode bit exact"
              % (rate, n), err == 0 and len(pts) == len(bits) // n,
              "%d symbols, %d bits back, %d errors" % (len(pts), len(got), err))
        same = all(v32.trellis_decode_bits([z * (1j ** k) for z in pts], ts=ts)
                   == got for k in (1, 2, 3))
        check("  and identical data under a 90/180/270 degree phase error", same)
        d2 = v32.trellis_free_distance(ts=ts)
        check("  free distance %.0f, normalised %.4f" % (d2, d2 / ts.power),
              d2 > 0)

    print()
    print("Table 5 and Table 6/V.32bis: the rate signal")
    for rates in ((4800, 9600), (4800, 7200, 9600, 12000, 14400), (14400,)):
        p = v32.bis_parse_rate(v32.bis_rate_sequence(rates))
        check("Table 5 round trip: %s" % list(rates),
              p is not None and p["rates"] == sorted(rates))
    check("B4-B6, B9-B10, B12 all zero calls for a GSTN cleardown (Note 3)",
          v32.bis_parse_rate(v32.bis_rate_sequence(()))["cleardown"])
    e = v32.bis_parse_rate(v32.bis_rate_sequence((14400,), end=True))
    check("Table 6: signal E carries the one rate that follows it",
          e is not None and e["end"] and e["rates"] == [14400])
    bad = 0
    for i in v32.BIS_SYNC:
        q = v32.bis_rate_sequence((9600,))
        q[i] ^= 1
        bad += v32.bis_parse_rate(q) is None
    check("5.3.1: the seven sync bits are load bearing",
          bad == len(v32.BIS_SYNC), "%d of %d rejected" % (bad, len(v32.BIS_SYNC)))
    check("Note 2: B13 and B14 are ignored on reception, not required to be zero",
          all(v32.bis_parse_rate([1 if k == i else v
                                  for k, v in enumerate(
                                      v32.bis_rate_sequence((9600,)))]) is not None
              for i in (13, 14)))
    check("Note 1: B4 or B8 zero means interworking under V.32 alone",
          all(v32.bis_parse_rate([0 if k == i else v
                                  for k, v in enumerate(
                                      v32.bis_rate_sequence((9600,)))]) is None
              for i in (4, 8)))
    d = [i for i in range(16)
         if v32.bis_rate_sequence((4800, 9600))[i]
         != v32.rate_sequence(can2400=False, can4800=True, can9600=True)[i]]
    check("a bis signal offering only 4800/9600 differs from a V.32 one in "
          "exactly B4 and B8 -- which is Note 1 to Table 6/V.32 from the other "
          "side", d == [4, 8], "differing bits %s" % d)

    print()
    print("5.2 receiver conditioning and 5.3 rate signal")
    check("5.2.1 segment 1 is 256 alternations of A and B",
          v32.s_states() == ["AB"[i % 2] for i in range(256)])
    check("5.2.2 segment 2 is 16 alternations of C and D",
          v32.sbar_states() == ["CD"[i % 2] for i in range(16)])
    trn = v32.trn_states(300, v32.Scrambler.GPC)
    check("5.2.3 TRN uses only A and C for its first 256 symbols",
          set(trn[:256]) <= {"A", "C"} and set(trn[256:]) - {"A", "C"},
          "then Table 5 opens it up to %s" % sorted(set(trn[256:])))
    r = v32.rate_sequence(can4800=True, can9600=True, trellis=False)
    p = v32.parse_rate(r)
    check("Table 6: a rate sequence round-trips",
          p and p["rates"] == [4800, 9600] and not p["trellis"]
          and not p["end"], "%s" % p)
    check("Table 6: B0-3, B7, B11, B15 are the sync pattern",
          all(v32.parse_rate([b ^ (1 if i == k else 0)
                              for i, b in enumerate(r)]) is None
              for k in (0, 1, 2, 3, 7, 11, 15)),
          "flipping any one of them is rejected")
    check("Table 6: B4-6 all zero calls for a GSTN cleardown",
          v32.parse_rate(v32.rate_sequence(False, False, False))["cleardown"])
    e = v32.parse_rate(v32.rate_sequence(can9600=True, can4800=False, end=True))
    check("Table 7: signal E is the same shape with B0-3 = 1111",
          e and e["end"] and e["rates"] == [9600], "%s" % e)
    check("Note 1: B4 and B8 together would mean V.32bis",
          v32.parse_rate(v32.rate_sequence(can2400=True, trellis=True))["trellis"])

    print()
    if FAIL:
        print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
        sys.exit(1)
    print("all V.32 tests passed")
