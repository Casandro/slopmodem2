"""ITU-T V.22 1200 bit/s modem: differential QPSK, 600 baud.

Parameters, all from ITU-T V.22 (11/1988):

  §2.1   carriers: low channel 1200 ± 0.5 Hz, high channel 2400 ± 1 Hz.
         Guard tone 1800 ± 20 Hz, transmitted only in the high channel, 6 ± 1 dB
         below the data power there, and disableable as a national option.
  §2.4   transmitted spectrum is the square root of a raised cosine with 75%
         roll-off.
  §2.5.1 600 baud, 1200 bit/s (2 bits per symbol).
  §2.5.2 Table 1: each dibit is a phase change relative to the preceding
         element -- 00 = +90°, 01 = 0°, 11 = +270°, and 10 = +180° by
         elimination. The left-hand digit is the earlier bit.
  §5     scrambler 1 + x^-14 + x^-17, with a guard that inverts the next input
         bit after 64 consecutive ones at the output.

The answer side transmits in the high channel and receives in the low channel;
the call side does the reverse.

Differential encoding is why this is tractable: the decision only needs the
phase *change* between adjacent symbols, so no absolute carrier phase reference
is required, and a frequency offset of a few Hz rotates the constellation by
under a degree per symbol (V.22bis §2.6 allows ±7 Hz).
"""
import math, cmath

SR = 8000
BAUD = 600.0
SPS = SR / BAUD                 # 13.333...
LOW, HIGH = 1200.0, 2400.0
GUARD_TONE = 1800.0
ROLLOFF = 0.75

# Table 1/V.22: dibit -> phase change in degrees
DIBIT_PHASE = {(0, 0): 90, (0, 1): 0, (1, 1): 270, (1, 0): 180}
PHASE_DIBIT = {v: k for k, v in DIBIT_PHASE.items()}


class Scrambler:
    """§5.1: self-synchronising, 1 + x^-14 + x^-17, with the 64-ones guard."""

    def __init__(self):
        self.reg = 0
        self.ones = 0

    def _step(self, bit, descramble=False):
        b14 = (self.reg >> 13) & 1
        b17 = (self.reg >> 16) & 1
        fb = b14 ^ b17
        if descramble:
            out = bit ^ fb
            self.reg = ((self.reg << 1) | bit) & 0x1FFFF
        else:
            out = bit ^ fb
            self.reg = ((self.reg << 1) | out) & 0x1FFFF
        return out

    def scramble(self, bits):
        out = []
        for b in bits:
            if self.ones >= 64:
                b ^= 1              # invert the next input after 64 ones out
                self.ones = 0
            o = self._step(b)
            self.ones = self.ones + 1 if o else 0
            out.append(o)
        return out

    def descramble(self, bits):
        """§5.2: the descrambler carries the matching 64-ones guard.

        The scrambler inverts its next *input* after 64 consecutive ones at its
        output; the descrambler sees that same sequence at its input and must
        invert its next *output* to undo it. Implementing one without the other
        leaves a single-bit error every time the guard fires -- and in an async
        stream a stray zero inside idle marks reads as a start bit, which is
        exactly the sort of thing that costs character framing.
        """
        out = []
        for b in bits:
            o = self._step(b, descramble=True)
            if self.ones >= 64:
                o ^= 1
                self.ones = 0
            self.ones = self.ones + 1 if b else 0
            out.append(o)
        return out


def srrc_at(t, beta=ROLLOFF):
    """Square-root raised cosine evaluated at continuous time t, in symbols.

    Needed because 8000/600 = 13.333 samples per symbol is not an integer. Laying
    a sampled pulse table down at rounded positions gives every symbol up to half
    a sample of timing error -- 3.75% of a symbol period -- and a receiver's
    async character framing eventually slips on that. Evaluating the pulse at the
    exact fractional offset for each output sample removes the jitter entirely.
    """
    if abs(t) < 1e-9:
        return 1.0 - beta + 4.0 * beta / math.pi
    if abs(abs(t) - 1.0 / (4.0 * beta)) < 1e-9:
        return (beta / math.sqrt(2.0)) * (
            (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
            - (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
    num = (math.sin(math.pi * t * (1 - beta))
           + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
    den = math.pi * t * (1 - (4 * beta * t) ** 2)
    return num / den


def srrc(beta=ROLLOFF, sps=SPS, span=6):
    """Square-root raised cosine impulse response, §2.4 (75% roll-off)."""
    n = int(span * sps)
    if n % 2 == 0:
        n += 1
    h = []
    for i in range(n):
        t = (i - (n - 1) / 2.0) / sps
        if abs(t) < 1e-9:
            v = 1.0 - beta + 4.0 * beta / math.pi
        elif abs(abs(t) - 1.0 / (4.0 * beta)) < 1e-9:
            v = (beta / math.sqrt(2.0)) * (
                (1 + 2 / math.pi) * math.sin(math.pi / (4 * beta))
                - (1 - 2 / math.pi) * math.cos(math.pi / (4 * beta)))
        else:
            num = (math.sin(math.pi * t * (1 - beta))
                   + 4 * beta * t * math.cos(math.pi * t * (1 + beta)))
            den = math.pi * t * (1 - (4 * beta * t) ** 2)
            v = num / den
        h.append(v)
    e = math.sqrt(sum(x * x for x in h))
    return [x / e for x in h]


class Mod:
    """V.22 modulator. `channel` is "low" (call side) or "high" (answer side)."""

    def __init__(self, channel="high", level_dbfs=-24.0, guard_tone=False):
        self.fc = HIGH if channel == "high" else LOW
        self.amp = 32768.0 * (10 ** (level_dbfs / 20.0)) * math.sqrt(2.0)
        self.guard = guard_tone and channel == "high"
        self.h = srrc()
        self.quadrant = 0.0        # accumulated absolute phase, radians
        self.scr = Scrambler()
        # continuous-modulator state, see shape()
        self.i = 0                 # carrier sample index
        self.sym = 0               # global symbol counter
        self.out = 0               # global output sample index
        self.carry = [0j] * (len(self.h) - 1)

    def symbols_from_bits(self, bits, scramble=True):
        """Bits -> complex symbols via Table 1 differential phase changes."""
        b = self.scr.scramble(bits) if scramble else list(bits)
        if len(b) % 2:
            b = b + [1]
        syms = []
        for i in range(0, len(b), 2):
            d = (b[i], b[i + 1])
            self.quadrant += math.radians(DIBIT_PHASE[d])
            syms.append(cmath.exp(1j * self.quadrant))
        return syms

    def shape(self, syms):
        """Upsample by SPS with the SRRC pulse, then upconvert onto the carrier.

        Stateful and continuous across calls. A real V.22bis modem does not
        restart its carrier between handshake segments, and the concatenated
        segments of the answer sequence -- USB1, S1, SB1 at 1200, scrambled ones
        at 2400, then data -- must join without a phase step, an amplitude dip
        or a symbol-clock jump. Three pieces of state carry that:

          self.i    carrier sample index, so cos(w*i) never restarts
          self.sym  global symbol counter, so the fractional 13.333-sample
                    symbol grid stays coherent across calls
          self.carry  the pulse-shaper's overlap-add residue, so the tail of the
                    last symbol of one call sums into the head of the next
                    instead of being discarded

        Rendering each segment with its own modulator (which is what an earlier
        version did) put all three discontinuities at the 1200 -> 2400 handover,
        which is the exact moment the far receiver switches to 16-way decisions.
        The Conexant rode through it; the Cirrus never locked.

        Pulse placement is unchanged: the pulse is laid down from a sampled tap
        table at the nearest sample to each symbol centre. Evaluating it at exact
        fractional offsets instead was tried and measured *worse* offline --
        constellation spread rose from 1.2% to 8.7% of the mean -- so the sampled
        table is kept. (round(k*SPS + j - D) == round(k*SPS) + j - D for integer
        j, so this is bit-identical to the old placement.)
        """
        h = self.h
        L = len(h)
        n = (int(round((self.sym + len(syms)) * SPS))
             - int(round(self.sym * SPS)))
        base = list(self.carry) + [0j] * n          # base[0] -> absolute self.out
        for k, a in enumerate(syms):
            start = int(round((self.sym + k) * SPS)) - self.out
            for j, hv in enumerate(h):
                base[start + j] += a * hv
        self.sym += len(syms)
        self.out += n
        self.carry = base[n:n + L - 1]
        while len(self.carry) < L - 1:
            self.carry.append(0j)

        out = []
        w = 2 * math.pi * self.fc / SR
        wg = 2 * math.pi * GUARD_TONE / SR
        g = 10 ** (-6.0 / 20.0) if self.guard else 0.0
        for k in range(n):
            i = self.i + k
            z = base[k]
            v = (z.real * math.cos(w * i) - z.imag * math.sin(w * i))
            if g:
                v += g * math.sin(wg * i)
            out.append(max(-32768, min(32767, int(self.amp * v))))
        self.i += n
        return out

    def modulate(self, bits, scramble=True, **kw):
        return self.shape(self.symbols_from_bits(bits, scramble, **kw))

    def flush(self):
        """Drain the pulse-shaper carry, ending the burst cleanly.

        shape() holds the trailing SRRC tail in self.carry so it can sum into
        the head of the next call; a caller that renders a burst in one shot and
        then stops has to ask for that tail explicitly, or its last few symbols
        arrive with their energy clipped off. Streaming callers must NOT call
        this mid-stream -- it would insert a gap.
        """
        tail, self.carry = self.carry, [0j] * (len(self.h) - 1)
        out = []
        w = 2 * math.pi * self.fc / SR
        wg = 2 * math.pi * GUARD_TONE / SR
        g = 10 ** (-6.0 / 20.0) if self.guard else 0.0
        for k, z in enumerate(tail):
            i = self.i + k
            v = (z.real * math.cos(w * i) - z.imag * math.sin(w * i))
            if g:
                v += g * math.sin(wg * i)
            out.append(max(-32768, min(32767, int(self.amp * v))))
        self.i += len(tail)
        self.out += len(tail)
        return out


class Demod:
    """V.22 demodulator with Gardner timing recovery and differential decision."""

    def __init__(self, channel="low", level_hint=None):
        self.fc = LOW if channel == "low" else HIGH
        self.h = srrc()

    def _baseband(self, x):
        w = 2 * math.pi * self.fc / SR
        z = [x[i] * cmath.exp(-1j * w * i) for i in range(len(x))]
        # matched filter
        h = self.h
        n = len(z)
        out = [0j] * n
        L = len(h)
        for i in range(n):
            acc = 0j
            lo = max(0, i - L + 1)
            for j in range(lo, i + 1):
                acc += z[j] * h[i - j]
            out[i] = acc
        return out

    def demod(self, x, scramble=True):
        """Return (bits, symbols).

        Symbol timing is recovered with a Gardner detector driving a
        proportional-integral loop. Gardner is used because it needs no carrier
        phase reference, which suits differential detection: the error is
        mid-sample * (current - previous), which is zero when sampling at the
        symbol centres regardless of constellation rotation.
        """
        y = self._baseband(x)
        pos = SPS
        prev = None
        syms = []
        acc = 0.0                  # integral term, in samples per symbol
        KP, KI = 0.10, 0.004
        while pos + 1 < len(y):
            def at(p):
                i = int(p)
                if i < 0 or i + 1 >= len(y):
                    return 0j
                f = p - i
                return y[i] * (1 - f) + y[i + 1] * f
            cur = at(pos)
            mid = at(pos - SPS / 2.0)
            if prev is not None:
                d = cur - prev
                err = mid.real * d.real + mid.imag * d.imag
                nrm = abs(cur) ** 2 + abs(prev) ** 2 + 1e-9
                err /= nrm
                acc += KI * err
                acc = max(-0.2, min(0.2, acc))
                step = SPS - (KP * err + acc)
            else:
                step = SPS
            syms.append(cur)
            prev = cur
            pos += step
        bits = []
        scr = Scrambler()
        for k in range(1, len(syms)):
            if abs(syms[k]) < 1e-9 or abs(syms[k - 1]) < 1e-9:
                continue
            d = cmath.phase(syms[k] * syms[k - 1].conjugate())
            deg = (math.degrees(d) + 360) % 360
            best = min(DIBIT_PHASE.values(), key=lambda p: min(abs(deg - p), 360 - abs(deg - p)))
            bits.extend(PHASE_DIBIT[best])
        return (scr.descramble(bits) if scramble else bits), syms


# ---------------------------------------------------------------------------
# V.22bis handshake signals (V.22bis §6.3.1.1)
#
# All three are 1200 bit/s V.22 modulation, so they need only the machinery
# above -- which is what makes it possible to drive a V.22bis handshake without
# knowing the 16-point constellation yet.
#
#   USB1  unscrambled binary 1        -> dibits "11", i.e. +270 deg per symbol
#   S1    unscrambled repetitive double dibit 00 and 11
#   SB1   scrambled binary 1
#
# The answering side's schedule, from §6.3.1.1.2 and the caller's §6.3.1.1.1:
#   ANS, then USB1. The caller needs 155 +/- 10 ms of USB1, stays silent a
#   further 456 +/- 10 ms, sends its own S1 for 100 +/- 3 ms, then SB1. We reply
#   with S1 for 100 +/- 3 ms and then SB1; the caller turns on circuit 112 at the
#   end of our S1 and 600 +/- 10 ms later starts transmitting scrambled binary 1
#   *at 2400 bit/s* -- which is the first time the 16-point constellation appears
#   on the line.
# ---------------------------------------------------------------------------

def usb1_bits(nbits):
    """Unscrambled binary 1."""
    return [1] * nbits

def s1_bits(nbits):
    """Unscrambled repetitive double dibit 00 and 11."""
    pat = [0, 0, 1, 1]
    return [pat[i % 4] for i in range(nbits)]

def bits_for(seconds, rate=1200.0):
    return int(seconds * rate)

def answer_handshake(level_dbfs=-18.0, ans_s=2.5, usb1_s=0.75, s1_s=0.100,
                     sb1_s=6.0, guard_tone=False, sr=SR):
    """Render the answering side's V.22bis handshake, open loop.

    Open loop is adequate because the caller's timings are specified to +/-10 ms
    and its progression to 2400 bit/s depends on the S1 exchange, not on
    anything we send afterwards.
    """
    import ansam
    out = list(ansam.ans_samples(ans_s, level_dbfs=level_dbfs))
    m = Mod("high", level_dbfs=level_dbfs, guard_tone=guard_tone)
    # USB1 - unscrambled, so the scrambler is bypassed
    out += m.modulate(usb1_bits(bits_for(usb1_s)), scramble=False)
    # our S1, timed to land just after the caller's own S1 ends
    out += m.modulate(s1_bits(bits_for(s1_s)), scramble=False)
    # SB1 at 1200 bit/s: scrambled binary 1
    out += m.modulate([1] * bits_for(sb1_s), scramble=True)
    return out
