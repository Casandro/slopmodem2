"""ITU-T V.8 7.2 ANSam generator, with the variants milestone 2 needs.

Spec (V.8 7.2): sinewave at 2100 +/- 1 Hz, phase reversals every 450 +/- 25 ms,
amplitude-modulated by a 15 +/- 0.1 Hz sinewave whose envelope ranges between
0.8 and 1.2 times its *average* amplitude, and average power outside
2100 +/- 200 Hz at least 24 dB below the power inside it.

Phase reversals are optional: "When network echo canceller disabling is not
required, phase reversals shall not be imparted to the ANSam signal."

Note on the envelope: `1 + depth*sin()` gives min/max of 0.8/1.2 about an
average of 1.0, which is what the spec asks for. Writing it as
`1 - depth + depth*sin()` (as an earlier draft in testrig/tools did) yields
0.6..1.0 about an average of 0.8, i.e. 0.75..1.25 relative - out of spec, and
6 dB quieter than intended.

Abrupt phase reversals are a step discontinuity and splatter energy outside the
2100 +/- 200 Hz band, which can breach the 24 dB requirement; `shape="cosine"`
ramps the reversal over `ramp_ms` instead.
"""
import math
import g711

SR = 8000

def ansam_samples(seconds=6.0, sr=SR, level_dbfs=-15.0, f=2100.0,
                  am_rate=15.0, am_depth=0.20,
                  reversal_ms=450.0, shape="cosine", ramp_ms=2.0):
    """Linear 16-bit samples of ANSam.

    reversal_ms=None or 0 disables phase reversals.
    shape: "abrupt" (instant sign flip) or "cosine" (raised-cosine phase ramp).
    """
    n = int(seconds * sr)
    # amplitude such that the *average* envelope gives the requested RMS
    amp = 32768.0 * (10 ** (level_dbfs / 20.0)) * math.sqrt(2.0)
    dph = 2 * math.pi * f / sr
    ramp = max(1, int(ramp_ms * sr / 1000.0)) if shape == "cosine" else 1
    rev_n = int(reversal_ms * sr / 1000.0) if reversal_ms else 0

    out = []
    ph = 0.0
    offset = 0.0            # accumulated phase offset from reversals
    ramp_left = 0
    ramp_from = 0.0
    ramp_to = 0.0
    for i in range(n):
        if rev_n and i and i % rev_n == 0:
            ramp_from = offset
            ramp_to = offset + math.pi
            ramp_left = ramp
        if ramp_left > 0:
            u = 1.0 - (ramp_left / float(ramp))
            offset = ramp_from + (ramp_to - ramp_from) * 0.5 * (1 - math.cos(math.pi * u))
            ramp_left -= 1
            if ramp_left == 0:
                offset = ramp_to
        env = 1.0 + am_depth * math.sin(2 * math.pi * am_rate * i / sr)
        out.append(int(amp * env * math.sin(ph + offset)))
        ph += dph
    return out

def ansam_alaw(seconds=6.0, pt=8, **kw):
    """ANSam as G.711 payload bytes."""
    return g711.encode(ansam_samples(seconds, **kw), pt)

def ans_samples(seconds=3.3, sr=SR, level_dbfs=-24.0, f=2100.0,
                reversal_ms=None):
    """The plain V.25 answer tone: 2100 Hz, no amplitude modulation.

    This is ANS, not ANSam. It tells the caller "a modem answered" and nothing
    more: V.8 8.1.1 has the caller respond by proceeding to Annex A/V.32 bis
    rather than attempting a CM/JM exchange. Which is the point here - this rig
    strips the V.8 modulation anyway, so ANS is what actually arrives.

    V.25 4.3: the answer tone runs 3.3 +/- 0.7 s when reversals are used;
    reversals are optional and are stripped by this rig regardless.
    """
    return ansam_samples(seconds, sr=sr, level_dbfs=level_dbfs, f=f,
                         am_depth=0.0, reversal_ms=reversal_ms, shape="abrupt")

def ec_disable_samples(seconds=3.5, sr=SR, level_dbfs=-24.0, f=2100.0,
                       reversal_ms=450.0):
    """The echo-canceller disabling tone: 2100 Hz with periodic phase reversals,
    no amplitude modulation.

    G.168 7.1 says the tone disabler "should disable the echo canceller only
    upon detection of a signal which consists of a 2100 Hz tone with periodic
    phase reversals inserted in that tone, and not disable with any other
    in-band signal, e.g. ... a 2100 Hz tone without phase reversals", and that
    a disabled canceller "no longer modif[ies] the signals which pass through
    it in either direction".

    V.25 2.3 specifies the reversals: 180 degrees at intervals of 425 to 475 ms,
    the phase within 180 +/- 10 degrees in 1 ms, and the amplitude not more than
    3 dB below its steady-state value for more than 400 us. An instantaneous
    sign flip satisfies all three (it is exactly 180 degrees, immediately, with
    no amplitude dip), so `shape="abrupt"` is used rather than a ramp.
    """
    return ansam_samples(seconds, sr=sr, level_dbfs=level_dbfs, f=f,
                         am_depth=0.0, reversal_ms=reversal_ms, shape="abrupt")

VARIANTS = {
    # name: kwargs -- the sweep for milestone 2
    "cosine450":  dict(reversal_ms=450.0, shape="cosine"),
    "abrupt450":  dict(reversal_ms=450.0, shape="abrupt"),
    "noreversal": dict(reversal_ms=None),
    "deep":       dict(reversal_ms=450.0, shape="cosine", am_depth=0.30),
    "shallow":    dict(reversal_ms=450.0, shape="cosine", am_depth=0.10),
}

if __name__ == "__main__":
    import dsp
    for name, kw in VARIANTS.items():
        x = ansam_samples(6.0, **kw)
        print("=== %s ===" % name)
        dsp.analyse_ansam(x)
        print()
