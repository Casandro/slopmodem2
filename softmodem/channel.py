"""A telephone line that is not perfect, so impairments can be dialled in.

Everything the rig can tell us about 14 400 is now bounded by the rig itself:
the voice-mode capture floors at 7.1% of the rms radius on a pure tone, which is
most of the 11.0% margin under argument. This is the other way at it -- a channel
whose impairments are known exactly, sitting between two soft modems, so the
question changes from "what is the line doing to us" to "how much impairment does
our receiver actually survive, and does 14 400 fall over sooner than it should".

Three impairments, because three are what a real line mostly has:

**Amplitude tilt and band edges.** A tilt in dB across 300..3400 Hz plus a
raised-cosine skirt at each edge. Tilt is what loaded cable does; the skirts are
where the 128-point constellation's outer corners live.

**Group delay distortion.** Parabolic about 1800 Hz, quoted as the extra delay in
ms at 1500 Hz from centre. This is the impairment a V.32bis equaliser exists to
remove, and the one most likely to separate 12 000 from 14 400: it smears symbols
into their neighbours without costing any amplitude at all, so it is invisible to
every level measurement we have taken.

**Clock drift.** The far end's sampling clock in ppm. The rig showed -43 ppm
between us and the ATA, corroborated at 46 ppm by an independent measurement, so
this is not hypothetical -- it is the one impairment already known to be present
at roughly the level simulated here.

Amplitude, delay and drift are one filter, not three. Each polyphase branch is
the line response designed with an extra sub-sample of linear phase, so picking a
branch *is* the fractional resample: one dot product per output sample does all
of it. That matters only because this has to run inside a soft-to-soft call in
pure Python, where a second convolution per sample is a second minute of runtime.

Noise is added after the filter, as a fraction of the signal's own rms, so
--snr is in dB relative to whatever level the modem chose.
"""
import cmath
import math
import random

SR = 8000.0

TAPS = 48       # 6 ms. Longer is not better here: the correction loop below
                # oscillates past this length, and 48 already realises 2 ms of
                # delay spread to within 0.3 dB
NPH = 128       # resampler phases; matches the receiver's own fractional filter
FRAC = 8        # fractional-delay sinc length
GRID = 512      # design grid


def _target(f, tilt_db, lo_hz, hi_hz, skirt_hz):
    """Nominal gain (linear) at one frequency: tilt across the band, cosine skirts."""
    af = abs(f)
    g = 10.0 ** ((tilt_db * (af - 300.0) / 3100.0) / 20.0)
    if af < lo_hz:
        t = max(0.0, (af - (lo_hz - skirt_hz)) / skirt_hz)
        g *= 0.5 - 0.5 * math.cos(math.pi * min(1.0, t))
    if af > hi_hz:
        t = max(0.0, ((hi_hz + skirt_hz) - af) / skirt_hz)
        g *= 0.5 - 0.5 * math.cos(math.pi * min(1.0, t))
    return g


def _design(tilt_db=0.0, lo_hz=300.0, hi_hz=3400.0, skirt_hz=250.0,
            delay_ms=0.0, taps=TAPS, grid=GRID, iters=30):
    """One line response, as an FIR, from an amplitude and group-delay curve.

    The group delay is parabolic about 1800 Hz, so the phase is its integral --
    a cubic. Writing the phase directly rather than cascading two filters keeps
    amplitude and delay independent, which is the point: a run can vary delay
    distortion with the frequency response held flat.

    Held flat is the hard part, and the first version of this was not. A cubic
    phase makes the impulse response a chirp, whose energy sits several samples
    from centre -- at 1 ms and 400 Hz, seven of them -- exactly where the window
    has begun to taper. The window then eats it, and the result is up to 6 dB of
    amplitude ripple in a filter whose amplitude was supposed to be flat. That is
    an instrument reporting an impairment it was not asked to apply, so the loop
    is closed instead: measure what the taps really do and fold the error back
    into the target until the two agree. It converges in about four passes.
    """
    a = (delay_ms * 1e-3) / (1500.0 ** 2)        # s per Hz^2
    fc = 1800.0
    freqs = []
    for k in range(grid):
        f = k * SR / grid
        if k > grid // 2:
            f -= SR                               # negative frequencies
        freqs.append(f)
    want = [_target(f, tilt_db, lo_hz, hi_hz, skirt_hz) for f in freqs]
    phase = [-2.0 * math.pi * a * ((f - fc) ** 3 + fc ** 3) / 3.0 for f in freqs]
    goal = [want[k] * cmath.exp(1j * phase[k]) for k in range(grid)]
    # correct only where there is signal to correct; chasing a stopband null
    # would spend the whole filter on frequencies the modem never uses
    live = [i for i, f in enumerate(freqs) if 250.0 <= abs(f) <= 3500.0
            and want[i] > 0.03]
    corr = [1.0 + 0j] * grid
    c = taps // 2
    h = []
    best = None
    for _ in range(max(1, iters)):
        h = []
        for n in range(taps):
            m = n - c
            s = 0j
            for k in range(grid):
                s += goal[k] * corr[k] * cmath.exp(2j * math.pi * k * m / grid)
            v = (s / grid).real
            w = 0.54 - 0.46 * math.cos(2.0 * math.pi * n / (taps - 1))
            h.append(v * w)
        # What did the taps actually do? Correct the *complex* response, not
        # just its magnitude. Correcting amplitude alone leaves the phase
        # unconstrained, and the phase is where the group delay lives: asking
        # for a gentle 0.5 ms parabola produced a realised curve swinging over
        # 0.85 ms and changing sign twice, which read as 6.6% of the rms radius
        # at every rate -- worse than the channel nominally three times harsher.
        # A channel that misreports its own delay is the one impairment this
        # file exists to measure, so it is the one it must not have.
        # Keep the best iterate, not the last. There are more grid points being
        # corrected than there are taps to correct them with, so the loop is
        # solving an overdetermined system and is under no obligation to
        # converge -- left to run, it reached 15.8 dB of error at 256 taps,
        # which is the correction diverging and not the filter's real limit.
        # Scoring each pass and keeping the winner makes the loop unable to
        # return anything worse than where it started.
        worst = 0.0
        errs = []
        for i in live:
            f = freqs[i]
            H = sum(h[n] * cmath.exp(-2j * math.pi * f * (n - c) / SR)
                    for n in range(taps))
            if abs(H) > 1e-9:
                r = goal[i] / H
                errs.append((i, r))
                worst = max(worst, abs(r - 1.0))
        if best is None or worst < best[0]:
            best = (worst, list(h))
        if worst < 0.01:
            break
        for i, r in errs:
            # under-relaxed in polar form, so it settles rather than rings,
            # and bounded so one bad grid point cannot run away with the filter
            corr[i] *= cmath.rect(min(4.0, max(0.25, abs(r) ** 0.7)),
                                  cmath.phase(r) * 0.7)
    if best is not None:
        h = best[1]
    # unit gain at 1800 Hz, so a channel only distorts and never changes level
    g1800 = abs(sum(h[n] * cmath.exp(-2j * math.pi * 1800.0 * (n - c) / SR)
                    for n in range(taps)))
    if g1800 > 1e-9:
        h = [v / g1800 for v in h]
    return h


def _sinc_frac(d, n=FRAC):
    """A windowed-sinc fractional delay of d samples, d in [0,1)."""
    c = n // 2 - 1
    out = []
    for i in range(n):
        x = i - c - d
        s = 1.0 if abs(x) < 1e-9 else math.sin(math.pi * x) / (math.pi * x)
        w = 0.54 - 0.46 * math.cos(2.0 * math.pi * i / (n - 1))
        out.append(s * w)
    g = sum(out)
    return [v / g for v in out] if abs(g) > 1e-9 else out


class Channel:
    """One direction of an impaired line. Feed it samples, get samples back.

    Sample-rate offset means output and input counts genuinely differ, which is
    the whole point of simulating it -- but the soft-to-soft harness is built on
    a fixed 160-sample frame each way. The difference is absorbed by a priming
    buffer: at 50 ppm a whole minute accrues 24 samples, so a few hundred of
    head start covers runs far longer than any test here.
    """

    def __init__(self, tilt_db=0.0, lo_hz=300.0, hi_hz=3400.0, skirt_hz=250.0,
                 delay_ms=0.0, drift_ppm=0.0, snr_db=None, seed=1):
        h = _design(tilt_db, lo_hz, hi_hz, skirt_hz, delay_ms)
        # each phase is the response carrying an extra p/NPH samples of delay,
        # so choosing a phase resamples and shapes in the same dot product
        self.bank = []
        for p in range(NPH):
            f = _sinc_frac(p / float(NPH))
            n = len(h) + len(f) - 1
            c = [0.0] * n
            for i, a in enumerate(h):
                for j, b in enumerate(f):
                    c[i + j] += a * b
            self.bank.append(c)
        self.L = len(self.bank[0])
        self.ratio = 1.0 + drift_ppm * 1e-6
        self.snr_db = snr_db
        self.rng = random.Random(seed)
        self.buf = [0.0] * (self.L + 512)     # priming head start, see docstring
        self.pos = float(self.L)              # read cursor into buf
        self.rms = 0.0
        self.h = h

    def realised(self, freqs=(300, 600, 1000, 1400, 1800, 2200, 2600, 3000, 3400)):
        """What the taps actually do, as (Hz, dB, group delay in ms).

        Nominal is what was asked for; this is what was applied. They differ by
        up to 0.8 dB because a cubic phase cannot be realised exactly in 48 taps,
        and a run that quotes the nominal figure is quoting the wrong one.
        """
        c = len(self.h) // 2
        out = []
        for f in freqs:
            H = sum(self.h[n] * cmath.exp(-2j * math.pi * f * (n - c) / SR)
                    for n in range(len(self.h)))
            d = 20.0
            H2 = sum(self.h[n] * cmath.exp(-2j * math.pi * (f + d) * (n - c) / SR)
                     for n in range(len(self.h)))
            gd = -cmath.phase(H2 * H.conjugate()) / (2.0 * math.pi * d) * 1e3
            out.append((f, 20.0 * math.log10(abs(H) + 1e-12), gd))
        return out

    def step(self, frame):
        """Consume one frame, return one frame of the same length."""
        self.buf.extend(float(v) for v in frame)
        out = []
        for _ in range(len(frame)):
            i = int(self.pos)
            frac = self.pos - i
            p = int(frac * NPH)
            if p >= NPH:
                p = NPH - 1
            taps = self.bank[p]
            b = self.buf
            s = 0.0
            base = i - self.L + 1
            if base < 0:
                base = 0
            for k, t in enumerate(taps):
                j = base + k
                if 0 <= j < len(b):
                    s += b[j] * t
            out.append(s)
            self.pos += self.ratio
        # noise relative to the signal's own level, tracked slowly
        if self.snr_db is not None:
            m = math.sqrt(sum(v * v for v in out) / max(1, len(out)))
            self.rms = m if self.rms == 0.0 else 0.95 * self.rms + 0.05 * m
            sigma = self.rms * (10.0 ** (-self.snr_db / 20.0))
            if sigma > 0:
                out = [v + self.rng.gauss(0.0, sigma) for v in out]
        # drop what the cursor has passed, keeping one filter length of history
        drop = int(self.pos) - self.L
        if drop > 0:
            del self.buf[:drop]
            self.pos -= drop
        return [int(round(v)) for v in out]


PRESETS = {
    # Delay values are 0.25, 1.0 and 1.5 ms and the skirts are wide, because
    # those are what 48 taps can actually realise -- see _design. A preset is a
    # request; run `python3 channel.py <name>` for what the taps really do.
    "perfect":    dict(),
    "mild":       dict(tilt_db=-3.0, hi_hz=3200.0, skirt_hz=450.0,
                       delay_ms=0.25, drift_ppm=-43.0, snr_db=38.0),
    "typical":    dict(tilt_db=-6.0, hi_hz=3000.0, delay_ms=1.0,
                       drift_ppm=-43.0, snr_db=32.0),
    "poor":       dict(tilt_db=-9.0, lo_hz=350.0, hi_hz=2800.0, skirt_hz=450.0,
                       delay_ms=1.5, drift_ppm=80.0, snr_db=26.0),
    # each impairment alone, to attribute a failure to one cause
    "drift":      dict(drift_ppm=-43.0),
    "drift-hard": dict(drift_ppm=200.0),
    "tilt":       dict(tilt_db=-6.0, hi_hz=3000.0),
    "delay":      dict(delay_ms=1.0),
    "delay-hard": dict(delay_ms=1.5, skirt_hz=450.0),
    "noise":      dict(snr_db=32.0),
}


def make(name, **over):
    if name not in PRESETS:
        raise SystemExit("unknown channel %r; have %s"
                         % (name, ", ".join(sorted(PRESETS))))
    kw = dict(PRESETS[name])
    kw.update(over)
    return Channel(**kw)


def describe(name):
    kw = PRESETS[name]
    if not kw:
        return "flat, no drift, no noise"
    bits = []
    if "tilt_db" in kw:
        bits.append("%+.0f dB tilt" % kw["tilt_db"])
    if "lo_hz" in kw or "hi_hz" in kw:
        bits.append("%.0f-%.0f Hz" % (kw.get("lo_hz", 300.0), kw.get("hi_hz", 3400.0)))
    if kw.get("delay_ms"):
        bits.append("%.1f ms delay distortion" % kw["delay_ms"])
    if kw.get("drift_ppm"):
        bits.append("%+.0f ppm" % kw["drift_ppm"])
    if kw.get("snr_db"):
        bits.append("%.0f dB SNR" % kw["snr_db"])
    return ", ".join(bits)


if __name__ == "__main__":
    # Measure the channel it claims to build, so a run's header quotes the
    # impairment applied and not the one requested.
    import sys
    for name in (sys.argv[1:] or sorted(PRESETS)):
        ch = make(name)
        print("channel %-11s %s" % (name, describe(name)))
        print("    %-9s %8s %10s" % ("freq", "gain dB", "delay ms"))
        for f, g, d in ch.realised():
            print("    %6d Hz %+7.2f %9.3f" % (f, g, d))
        print()
