"""ITU-T V.21 300 bit/s FSK modem.

Frequencies (V.21 3: channel 1 mean 1080 Hz, channel 2 mean 1750 Hz, deviation
+/-100 Hz, and "the higher characteristic frequency (FA) corresponds to a
binary 0"):

  channel "L" (V.21(L), low band)  mark/1 = 980 Hz   space/0 = 1180 Hz
  channel "H" (V.21(H), high band) mark/1 = 1650 Hz  space/0 = 1850 Hz

In V.8 the call DCE uses V.21(L) for CI/CM/CJ and the answer DCE uses V.21(H)
for JM, so an answer DCE transmits on H and receives on L, and vice versa.
"""
import math

SR = 8000
BAUD = 300.0

CHANNELS = {"L": (980.0, 1180.0), "H": (1650.0, 1850.0)}

class V21Mod:
    """Continuous-phase FSK modulator.

    Phase is carried across bits and across calls, so there are no
    discontinuities at bit boundaries (which would splatter). The bit clock uses
    a fractional accumulator because 8000/300 = 26.667 samples per bit is not an
    integer.
    """
    def __init__(self, channel="H", sr=SR, baud=BAUD, level_dbfs=-30.0):
        self.mark, self.space = CHANNELS[channel]
        self.sr = sr
        self.spb = sr / baud
        self.amp = 32768.0 * (10 ** (level_dbfs / 20.0)) * math.sqrt(2.0)
        self.ph = 0.0
        self.frac = 0.0

    def modulate(self, bits):
        out = []
        for b in bits:
            f = self.mark if b else self.space
            self.frac += self.spb
            n = int(self.frac)
            self.frac -= n
            dph = 2 * math.pi * f / self.sr
            for _ in range(n):
                out.append(int(self.amp * math.sin(self.ph)))
                self.ph += dph
                if self.ph > 2 * math.pi:
                    self.ph -= 2 * math.pi
        return out

    def mark_tone(self, seconds):
        """Continuous mark, used for the 10-ONEs preamble lead-in / idle."""
        return self.modulate([1] * int(seconds * BAUD))


class V21Demod:
    """FSK demodulator with transition-tracking bit-timing recovery.

    Per sample it correlates the last W samples against mark and space, takes
    the magnitude difference as the discriminator, and runs a first-order
    timing loop that is nudged towards alignment by every observed transition.
    Bits are sliced at mid-bit. A squelch suppresses output when neither tone is
    present, so silence does not produce random bits.

    Emits raw bits: V.8 preambles (10 ONEs then 10 sync bits) carry no start
    bits, so framing has to happen above this layer.
    """
    def __init__(self, channel="L", sr=SR, baud=BAUD, loop_gain=0.10,
                 squelch_rel=0.30, squelch_floor=200.0):
        self.mark, self.space = CHANNELS[channel]
        self.sr = sr
        self.baud = baud
        self.W = int(round(sr / baud))
        self.buf = [0.0] * self.W
        self.pos = 0
        self.n = 0
        w1 = 2 * math.pi * self.mark / sr
        w2 = 2 * math.pi * self.space / sr
        # correlator tables, oldest sample first
        self.c1 = [(math.cos(w1 * k), math.sin(w1 * k)) for k in range(self.W)]
        self.c2 = [(math.cos(w2 * k), math.sin(w2 * k)) for k in range(self.W)]
        self.phase = 0.0
        self.k = loop_gain
        self.prev_sign = 0
        self.sampled = False
        # Adaptive squelch: the correlator magnitude scales with signal
        # amplitude times W/2, so any absolute threshold is level-dependent.
        # Track a decaying peak and gate relative to it instead.
        self.squelch_rel = squelch_rel
        self.squelch_floor = squelch_floor
        self.peak = 0.0
        self.last_mag = 0.0

    def _disc(self):
        b = self.buf
        p = self.pos
        W = self.W
        i1r = i1i = i2r = i2i = 0.0
        for k in range(W):
            v = b[(p + k) % W]
            cr, ci = self.c1[k]
            i1r += v * cr
            i1i += v * ci
            cr, ci = self.c2[k]
            i2r += v * cr
            i2i += v * ci
        m1 = math.sqrt(i1r * i1r + i1i * i1i)
        m2 = math.sqrt(i2r * i2r + i2i * i2i)
        self.last_mag = m1 + m2
        return m1 - m2

    def feed(self, samples):
        """Push linear samples, return (bits, n_squelched) for this block."""
        bits = []
        step = self.baud / self.sr
        for x in samples:
            self.buf[self.pos] = float(x)
            self.pos = (self.pos + 1) % self.W
            self.n += 1
            if self.n < self.W:
                continue
            d = self._disc()
            if self.last_mag > self.peak:
                self.peak = self.last_mag
            else:
                self.peak *= 0.9995
            live = (self.last_mag > self.squelch_floor and
                    self.last_mag > self.squelch_rel * self.peak)
            s = 1 if d > 0 else -1
            if live and self.prev_sign and s != self.prev_sign:
                err = self.phase
                if err > 0.5:
                    err -= 1.0
                self.phase -= self.k * err
            self.prev_sign = s if live else 0
            self.phase += step
            if not self.sampled and self.phase >= 0.5:
                bits.append((1 if d > 0 else 0) if live else None)
                self.sampled = True
            if self.phase >= 1.0:
                self.phase -= 1.0
                self.sampled = False
        return bits
