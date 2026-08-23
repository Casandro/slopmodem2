"""Test signals for characterising the audio path.

These deliberately avoid depending on how one interprets "AM depth":

twotone  - two equal tones. A linear path reproduces exactly two lines; any
           amplitude nonlinearity (compressor, limiter, clipping) generates
           intermodulation products at 2f1-f2 and 2f2-f1. This tests for
           nonlinearity directly.
steplevel- a tone that alternates between two levels. Any gain control reveals
           its attack/release time constant as a settling curve after each step,
           and its ratio as the residual step size.
flat     - a single steady tone, as a control.
"""
import math

SR = 8000

def _amp(level_dbfs):
    return 32768.0 * (10 ** (level_dbfs / 20.0)) * math.sqrt(2.0)

def twotone(seconds=8.0, level_dbfs=-12.0, f1=1000.0, f2=2100.0, sr=SR):
    """Two equal-amplitude tones; total power equals `level_dbfs`."""
    a = _amp(level_dbfs) / math.sqrt(2.0)      # split power between the tones
    n = int(seconds * sr)
    return [int(a * (math.sin(2 * math.pi * f1 * i / sr) +
                     math.sin(2 * math.pi * f2 * i / sr))) for i in range(n)]

def steplevel(seconds=8.0, level_dbfs=-12.0, step_db=12.0, period=0.400,
              f=2100.0, sr=SR):
    """Tone alternating between level_dbfs and level_dbfs-step_db."""
    hi = _amp(level_dbfs)
    lo = _amp(level_dbfs - step_db)
    n = int(seconds * sr)
    half = int(period * sr / 2)
    out = []
    for i in range(n):
        a = hi if (i // half) % 2 == 0 else lo
        out.append(int(a * math.sin(2 * math.pi * f * i / sr)))
    return out

def flat(seconds=8.0, level_dbfs=-12.0, f=2100.0, sr=SR):
    a = _amp(level_dbfs)
    n = int(seconds * sr)
    return [int(a * math.sin(2 * math.pi * f * i / sr)) for i in range(n)]

def amtone(seconds=8.0, level_dbfs=-12.0, f=2100.0, am_rate=15.0,
           am_depth=0.20, sr=SR):
    """AM tone on an arbitrary carrier.

    The discriminating test: if 15 Hz AM survives on a 1500 Hz carrier but is
    destroyed on a 2100 Hz carrier at the same level, then the mechanism is
    specific to the answer-tone frequency (a detector acting on it), and cannot
    be a general level-dependent compressor.
    """
    a = _amp(level_dbfs)
    n = int(seconds * sr)
    return [int(a * (1.0 + am_depth * math.sin(2 * math.pi * am_rate * i / sr))
                * math.sin(2 * math.pi * f * i / sr)) for i in range(n)]

def am1500(seconds=8.0, level_dbfs=-12.0, sr=SR):
    return amtone(seconds, level_dbfs, f=1500.0, sr=sr)

def am2100(seconds=8.0, level_dbfs=-12.0, sr=SR):
    return amtone(seconds, level_dbfs, f=2100.0, sr=sr)

def am2103(seconds=8.0, level_dbfs=-12.0, sr=SR):
    """Offset carrier: if the path regenerates the tone, the output frequency
    will snap to the generator's own value instead of staying at 2103 Hz."""
    return amtone(seconds, level_dbfs, f=2103.0, sr=sr)

def am2600(seconds=8.0, level_dbfs=-12.0, sr=SR):
    return amtone(seconds, level_dbfs, f=2600.0, sr=sr)

def _mk(f):
    def g(seconds=8.0, level_dbfs=-12.0, sr=SR, _f=f):
        return amtone(seconds, level_dbfs, f=_f, sr=sr)
    return g

def altnoise(seconds=60.0, level_dbfs=-24.0, block=5.0, f=2100.0,
             am_depth=0.20, sr=SR, seed=12345):
    """Alternate band-limited noise and an AM tone, in `block`-second slabs.

    Purpose: see whether the answer-tone detector latches only near the start of
    a call, or re-acquires every time the tone reappears. Noise between the tone
    slabs gives the detector something to un-latch on.
    """
    import random
    rnd = random.Random(seed)
    amp = _amp(level_dbfs)
    nrms = 32768.0 * (10 ** (level_dbfs / 20.0))
    out = []
    n = int(seconds * sr)
    nb = int(block * sr)
    i = 0
    while i < n:
        tone_slab = (i // nb) % 2 == 1
        for k in range(min(nb, n - i)):
            j = i + k
            if tone_slab:
                env = 1.0 + am_depth * math.sin(2 * math.pi * 15.0 * j / sr)
                v = amp * env * math.sin(2 * math.pi * f * j / sr)
            else:
                v = rnd.gauss(0, nrms)
            out.append(max(-32768, min(32767, int(v))))
        i += nb
    return out

def v22bis_answer(seconds=12.0, level_dbfs=-18.0, sr=SR):
    """The answering side of a V.22bis handshake, to make a hardware modem
    reveal its 16-point constellation by proceeding to 2400 bit/s."""
    import v22
    x = v22.answer_handshake(level_dbfs=level_dbfs, sb1_s=max(1.0, seconds - 3.5))
    return x[:int(seconds * sr)] if len(x) > int(seconds * sr) else x

def _mkalt(f):
    def g(seconds=60.0, level_dbfs=-24.0, sr=SR, _f=f):
        return altnoise(seconds, level_dbfs, f=_f, sr=sr)
    return g

def _mkec(f):
    def g(seconds=8.0, level_dbfs=-12.0, sr=SR, _f=f):
        import ansam
        return ansam.ec_disable_samples(seconds, sr=sr, level_dbfs=level_dbfs, f=_f)
    return g

SIGNALS = {"twotone": twotone, "steplevel": steplevel, "flat": flat,
           "v22bis_answer": v22bis_answer}
# pure echo-canceller disabling tones (2100 Hz-family, phase reversals, no AM)
for _f in (2100, 2110, 1500):
    SIGNALS["ecdis%d" % _f] = _mkec(float(_f))
for _f in (2100, 1500):
    SIGNALS["alt%d" % _f] = _mkalt(float(_f))
# AM tones on a range of carriers, to map how selective the 2100 Hz effect is
for _f in (1500, 2080, 2090, 2095, 2098, 2100, 2102, 2103, 2105, 2110, 2130, 2600):
    SIGNALS["am%d" % _f] = _mk(float(_f))

def _mkd(f, depth):
    def g(seconds=8.0, level_dbfs=-12.0, sr=SR, _f=f, _d=depth):
        return amtone(seconds, level_dbfs, f=_f, am_depth=_d, sr=sr)
    return g

# Depth sweep: does a heavily modulated tone still look enough like a steady
# tone for the answer-tone detector to latch onto it? V.8 7.2 specifies 20%,
# so anything above that is a diagnostic probe rather than conformant ANSam.
for _f in (2100, 1500):
    for _d in (22, 24, 26, 28, 30, 32, 36, 40, 60, 80, 100):
        SIGNALS["am%dd%d" % (_f, _d)] = _mkd(float(_f), _d / 100.0)
