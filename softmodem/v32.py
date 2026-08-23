"""ITU-T V.32 / V.32bis start-up signals (answer side, non-V.8 path).

Only the Phase-1/2 start-up signals are here, not the data-phase modem. This is
the path a caller takes when it hears plain ANS instead of ANSam: V.8 8.1.1
sends it to Annex A/V.32 bis, so the ceiling is 14.4 kbit/s.

Signal definitions (V.32 Table 4 and A.2.2 / 5.4.2):

  AA   repetitively carrier state A          -> a pure 1800 Hz tone
  AC   signal states ACAC..AC                -> 600 Hz + 3000 Hz, carrier suppressed
  CA   signal states CACA..CA                -> same spectrum, opposite phase
  CC   repetitively carrier state C          -> 1800 Hz in antiphase to AA

States A and C are antipodal points of the start-up constellation, so
alternating them at the 2400 baud symbol rate is a 180 degree phase reversal
every symbol. That places the energy at 1800 +/- 1200 Hz and suppresses the
carrier, which is why V.32 5.4.2 Note refers to "the three 200 Hz bands centred
at 600 Hz, 1800 Hz and 3000 Hz".

The symbol rate is 2400 baud against an 8000 Hz sample clock, i.e. 3 1/3
samples per symbol, so the symbol clock uses a fractional accumulator.
"""
import math

SR = 8000
BAUD = 2400.0
CARRIER = 1800.0
SPS = SR / BAUD           # 3.3333...

# phase of each start-up carrier state, in units of pi/2 (V.32 Figure 1:
# A and C antipodal, B and D antipodal and in quadrature with them)
STATE_PHASE = {"A": 0.0, "B": math.pi / 2, "C": math.pi, "D": 3 * math.pi / 2}


def _amp(level_dbfs):
    return 32768.0 * (10 ** (level_dbfs / 20.0)) * math.sqrt(2.0)


def states(pattern, seconds, level_dbfs=-24.0, sr=SR, carrier=CARRIER,
           baud=BAUD, phase0=0.0):
    """Transmit a repeating pattern of carrier states, e.g. "AC" or "A".

    Continuous carrier phase with a 180 degree jump at each symbol boundary
    where the state changes; rectangular symbols.
    """
    amp = _amp(level_dbfs)
    n = int(seconds * sr)
    sps = sr / baud
    out = []
    ph = phase0
    dph = 2 * math.pi * carrier / sr
    for i in range(n):
        k = int(i / sps)
        st = pattern[k % len(pattern)]
        out.append(int(amp * math.sin(ph + STATE_PHASE[st])))
        ph += dph
        if ph > 2 * math.pi:
            ph -= 2 * math.pi
    return out


def aa(seconds, **kw):
    """AA: repetitive state A -- a pure 1800 Hz tone (what the caller sends)."""
    return states("A", seconds, **kw)


def ac(seconds, **kw):
    """AC: alternating states A and C."""
    return states("AC", seconds, **kw)


def ca(seconds, **kw):
    """CA: alternating states C and A (AC with the phase inverted)."""
    return states("CA", seconds, **kw)


def cc(seconds, **kw):
    """CC: repetitive state C -- 1800 Hz in antiphase to AA."""
    return states("C", seconds, **kw)


SYMBOL_S = 1.0 / BAUD

def symbols(n):
    """Duration of n symbol intervals, in seconds."""
    return n * SYMBOL_S


# ---------------------------------------------------------------------------
# Data phase: constellation, differential coding, scramblers
# ---------------------------------------------------------------------------
#
# Table 3/V.32 lists the coordinates, but the PDF's OCR strips every sign from
# that table -- it renders "-0" and repeats "-1 -1" for states that must differ --
# so it cannot be read directly. Figure 1's *labels* survive intact, though, and
# the figure's geometry is unambiguous: a 4x4 grid with axis ticks at +/-2, so the
# points are at odd coordinates, and the labels read
#
#     Im=+3:  1011  1001  1110  1111
#     Im=+1:  1010  1000  1100  1101
#     Im=-1:  0001  0000  0100  0110
#     Im=-3:  0011  0010  0101  0111      (columns Re = -3, -1, +1, +3)
#
# with the note "the binary numbers denote Y1n Y2n Q3n Q4n". That reading is then
# checked against Table 3 by magnitude only, which the OCR did preserve, and
# against two structural properties the code asserts at import (see selfcheck).

_FIG1_ROWS = ((3, ("1011", "1001", "1110", "1111")),
              (1, ("1010", "1000", "1100", "1101")),
              (-1, ("0001", "0000", "0100", "0110")),
              (-3, ("0011", "0010", "0101", "0111")))
_FIG1_COLS = (-3, -1, 1, 3)

# label (Y1,Y2,Q3,Q4) -> (Re, Im)
NONRED = {}
for _im, _row in _FIG1_ROWS:
    for _re, _lab in zip(_FIG1_COLS, _row):
        NONRED[tuple(int(c) for c in _lab)] = (_re, _im)
POINT_TO_NONRED = {v: k for k, v in NONRED.items()}

# 2.4.2 / Figure 1: the subset used at 4800 bit/s and for training is the four
# points with Q3Q4 = 00. Table 1's "signal state" column names them.
ABCD = {"A": NONRED[(0, 0, 0, 0)], "B": NONRED[(0, 1, 0, 0)],
        "C": NONRED[(1, 1, 0, 0)], "D": NONRED[(1, 0, 0, 0)]}
YY_TO_STATE = {(0, 0): "A", (0, 1): "B", (1, 1): "C", (1, 0): "D"}

# Table 1/V.32, transcribed. (Q1,Q2,Y1prev,Y2prev) -> (Y1,Y2). The printed phase
# column reads "+190" for two of the four blocks, which is a scan artefact; the
# Y values are clean, and selfcheck() confirms they are exactly the rotations
# +90, 0, +180, +270 -- the same mapping as Table 1/V.22.
TABLE1 = {}
for _q, _rows in (((0, 0), (((0, 0), (0, 1)), ((0, 1), (1, 1)),
                            ((1, 0), (0, 0)), ((1, 1), (1, 0)))),
                  ((0, 1), (((0, 0), (0, 0)), ((0, 1), (0, 1)),
                            ((1, 0), (1, 0)), ((1, 1), (1, 1)))),
                  ((1, 0), (((0, 0), (1, 1)), ((0, 1), (1, 0)),
                            ((1, 0), (0, 1)), ((1, 1), (0, 0)))),
                  ((1, 1), (((0, 0), (1, 0)), ((0, 1), (0, 0)),
                            ((1, 0), (1, 1)), ((1, 1), (0, 1))))):
    for _prev, _out in _rows:
        TABLE1[(_q[0], _q[1], _prev[0], _prev[1])] = _out

QUAD_ROT = {(0, 0): 90, (0, 1): 0, (1, 0): 180, (1, 1): 270}


class Scrambler:
    """Section 4: self-synchronising, and the two directions differ.

    Call mode uses GPC = 1 + x^-18 + x^-23, answer mode GPA = 1 + x^-5 + x^-23,
    and each descrambles with the other's polynomial (4.1.1). That is unlike
    V.22bis, which uses one polynomial in both directions -- a difference worth
    stating, because assuming V.22bis's arrangement here would produce a link
    that scrambles correctly and descrambles to noise.

    No 64-consecutive-ones guard: section 4 does not specify one, and V.22's
    exists to avoid instigating its remote loop 2, which V.32 does not have.
    """

    GPC = (18, 23)          # calling modem
    GPA = (5, 23)           # answering modem

    def __init__(self, taps):
        self.taps = tuple(taps)
        self.width = max(self.taps)
        self.reg = 0

    def _fb(self):
        f = 0
        for t in self.taps:
            f ^= (self.reg >> (t - 1)) & 1
        return f

    def scramble(self, bits):
        out = []
        mask = (1 << self.width) - 1
        for b in bits:
            o = b ^ self._fb()
            self.reg = ((self.reg << 1) | o) & mask
            out.append(o)
        return out

    def descramble(self, bits):
        out = []
        mask = (1 << self.width) - 1
        for b in bits:
            out.append(b ^ self._fb())
            self.reg = ((self.reg << 1) | b) & mask
        return out


def encode_4800(bits, y=(0, 0)):
    """2.4.2: dibits, differentially encoded, on the ABCD subset."""
    pts = []
    for i in range(0, len(bits) - 1, 2):
        y = TABLE1[(bits[i], bits[i + 1], y[0], y[1])]
        pts.append(complex(*NONRED[(y[0], y[1], 0, 0)]))
    return pts, y


def encode_9600(bits, y=(0, 0)):
    """2.4.1.1: quadbits; Q1Q2 differentially encoded, Q3Q4 pick the point."""
    pts = []
    for i in range(0, len(bits) - 3, 4):
        y = TABLE1[(bits[i], bits[i + 1], y[0], y[1])]
        pts.append(complex(*NONRED[(y[0], y[1], bits[i + 2], bits[i + 3])]))
    return pts, y


def decode_points(pts, bps, y=None):
    """Points -> data bits, undoing the differential quadrant coding."""
    bits = []
    inv = {v: k for k, v in TABLE1.items()}
    for z in pts:
        lab = POINT_TO_NONRED[_nearest(z)]
        cur = (lab[0], lab[1])
        if y is not None:
            q = None
            for (q1, q2, p1, p2), out in TABLE1.items():
                if (p1, p2) == y and out == cur:
                    q = (q1, q2)
                    break
            bits.extend(q)
            if bps == 4:
                bits.extend((lab[2], lab[3]))
        y = cur
    return bits, y


def _nearest(z):
    best, bd = None, None
    for p in NONRED.values():
        d = abs(z - complex(*p))
        if bd is None or d < bd:
            bd, best = d, p
    return best


def selfcheck():
    """Two structural properties, plus the Table 3 magnitude cross-check."""
    ok = True
    # 16 distinct points on the odd lattice, four per quadrant
    if len(set(NONRED.values())) != 16:
        ok = False
    for yy in ((0, 0), (0, 1), (1, 0), (1, 1)):
        quad = {NONRED[(yy[0], yy[1], a, b)] for a in (0, 1) for b in (0, 1)}
        if len(quad) != 4:
            ok = False
        sre = {1 if re > 0 else -1 for re, im in quad}
        sim = {1 if im > 0 else -1 for re, im in quad}
        if len(sre) != 1 or len(sim) != 1:
            ok = False          # all four must share a quadrant
    # a 90 degree rotation advances Y1Y2 and leaves Q3Q4 alone
    for lab, (re, im) in NONRED.items():
        rot = (-im, re)
        if rot not in POINT_TO_NONRED:
            ok = False
            continue
        r = POINT_TO_NONRED[rot]
        if (r[2], r[3]) != (lab[2], lab[3]):
            ok = False
    # Table 1 is exactly the rotation named in its phase column
    for (q1, q2, p1, p2), out in TABLE1.items():
        a = complex(*NONRED[(p1, p2, 0, 0)])
        b = complex(*NONRED[(out[0], out[1], 0, 0)])
        want = QUAD_ROT[(q1, q2)]
        got = round((math.degrees(math.atan2(b.imag, b.real))
                     - math.degrees(math.atan2(a.imag, a.real))) % 360)
        if got != want:
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Modulator
# ---------------------------------------------------------------------------
#
# The arithmetic lines up as neatly as V.22bis's did, which matters because the
# same frame-boundary continuity argument is what lets a pre-rendered start-up
# stream be cut short and continued on demand:
#
#   160 samples (one RTP frame) = 48 symbols at 2400 baud
#                              = 4 periods of the 1800 Hz carrier table
#                                (1800/8000 repeats exactly every 40 samples)
#   and 48 symbols is 192 bits at 9600 bit/s or 96 bits at 4800.
#
# 2.2 asks for the transmitted energy density at 600 and 3000 Hz -- the Nyquist
# edges, 1800 +/- 1200 -- to be 4.5 +/- 2.5 dB below the maximum. A square-root
# raised cosine is exactly 1/sqrt(2) in amplitude at the Nyquist frequency
# whatever its roll-off, i.e. 3.0 dB down in energy density, which sits inside
# that window with room either side.

ROLLOFF = 0.2


def srrc_at(t, beta=ROLLOFF):
    """Square-root raised cosine at continuous time t, in symbols."""
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


def srrc(beta=ROLLOFF, sps=SPS, span=10):
    n = int(span * sps)
    if n % 2 == 0:
        n += 1
    h = [srrc_at((i - (n - 1) / 2.0) / sps, beta) for i in range(n)]
    e = math.sqrt(sum(v * v for v in h))
    return [v / e for v in h]


class Mod:
    """V.32 modulator: 2400 baud on an 1800 Hz carrier, stateful and continuous.

    Same structure as v22.Mod's shape(): a persistent carrier sample index, a
    global symbol counter for the fractional 3.333-sample grid, and the pulse
    shaper's overlap-add residue. Nothing restarts between calls, so a stream
    assembled from many modulate() calls has no phase step, no amplitude dip and
    no symbol-clock jump at the joins.
    """

    def __init__(self, level_dbfs=-24.0, scrambler_taps=Scrambler.GPA,
                 beta=ROLLOFF, fc=CARRIER):
        # level_dbfs follows the same convention as v22.Mod: the label is the
        # actual RMS plus 10*log10(SPS), because the pulse is normalised to unit
        # energy and there are SPS samples per symbol. SPS is 3.333 here against
        # 13.333 for V.22, so the same label is 6.1 dB hotter -- V.32 at -24
        # puts the same power on the wire as the V.22bis runs at -18, which is
        # the level both hardware modems locked onto.
        self.amp = 32768.0 * (10 ** (level_dbfs / 20.0)) * math.sqrt(2.0)
        self.h = srrc(beta)
        # Three exact sub-phases; see shape().
        _n = len(self.h)
        _d = (_n - 1) // 2
        self.tab3 = [[srrc_at((j - _d - p / 3.0) / SPS, beta) for j in range(_n)]
                     for p in range(3)]
        _e = math.sqrt(sum(v * v for v in self.tab3[0]))
        self.tab3 = [[v / _e for v in row] for row in self.tab3]
        self.scr = Scrambler(scrambler_taps)
        self.y = (0, 0)                # differential quadrant state
        self.tre = None                # 2.4.1.2 trellis encoder, on first use
        # 2.1 fixes the carrier at 1800 +/- 1 Hz, but it also requires the
        # *receiver* to work with up to +/- 7 Hz of offset, and the only honest
        # way to test that is to transmit with one.
        self.fc = fc
        self.i = 0                     # carrier sample index
        self.sym = 0                   # global symbol counter
        self.out = 0                   # global output sample index
        self.carry = [0j] * (len(self.h) - 1)

    # -- symbol mapping --------------------------------------------------

    # The constellation sits on the odd lattice, mean power 10, so symbols are
    # scaled by 1/sqrt(10) on the way out: level_dbfs then means what it says
    # rather than being 10 dB optimistic. Same convention as v22bis.Mod.
    SCALE = 1.0 / math.sqrt(10.0)

    # ... and the four-point signals get their own, so that the handshake and the
    # data phase go out at the *same* power. The constellations do not have the
    # same mean power -- 2 for the A/B/C/D subset that 5.2's conditioning signal
    # and the rate signals use, 10 for either 9600 alternative -- so scaling
    # everything by 1/sqrt(10) puts a 7 dB step at the start of the data phase.
    #
    # The Recommendation is ambiguous about this: its coordinates are relative,
    # and TRN really is a subset of the data constellation, so a 7 dB step is a
    # defensible reading. Hardware settles it. Measured off a Conexant, its level
    # was -25.0 dBFS through S, TRN and every rate signal and -24.6 dBFS in the
    # data phase: constant line power, no step. Ours stepped from -36.2 to -29.3,
    # so the far end's equaliser -- trained on our TRN, like ours on its -- was
    # 7 dB out the moment our data phase began. It asked for a retrain 7.7 s
    # later, which is exactly what a receiver does when it cannot hold the eye.
    SCALE_4 = 1.0 / math.sqrt(2.0)

    def symbols(self, bits, bps=4, scramble=True, trellis=False, ts=None):
        """Data bits -> constellation points, scrambled per section 4.

        With trellis=True this is 2.4.1.2: the *scrambled* stream is grouped into
        quadbits and driven through Figure 1's encoder, so the scrambler comes
        first and the trellis coder second. The encoder persists across calls
        because it is recursive -- restarting it per frame would break the code.
        """
        b = self.scr.scramble(bits) if scramble else list(bits)
        if trellis or ts is not None:
            t = ts if ts is not None else TRELLIS_9600T
            if bps != t.nbits - 1:
                raise ValueError("%s carries %d data bits per symbol, not %d"
                                 % (t.name, t.nbits - 1, bps))
            if self.tre is None or self.tre.ts is not t:
                self.tre = TrellisEncoder(t)
            # Each constellation has its own mean power -- 10 at 7200 and 9600,
            # 42 at 12 000, 41 at 14 400 -- and the line level must not change
            # with the rate, so the scale is 1/sqrt(power) rather than a constant.
            # At 9600 that is exactly SCALE, which is why nothing below moves.
            g = 1.0 / math.sqrt(t.power)
            return [p * g for p in self.tre.encode(b)]
        if bps == 4:
            pts, self.y = encode_9600(b, self.y)
            return [p * self.SCALE for p in pts]
        pts, self.y = encode_4800(b, self.y)
        return [p * self.SCALE_4 for p in pts]

    def reset_trellis(self, keep_diff=True, ts=None):
        """5.4.1 and 5.4.2: "the initial states of the delay elements of the
        convolution encoder ... should be set to zero" where the trellis-coded
        transmission begins. (One clause says Figure 2 and the other Figure 3;
        Figure 2 is the encoder, so that is what is meant.)

        Note what that does *not* say: nothing about the differential encoder of
        Table 2. 5.3 has it initialised from TRN's final symbol for the rate
        signals, and no clause resets it again, so by default its state carries
        across into the data phase -- `keep_diff`. Rebuilding the whole encoder
        zeroed it too, which is one symbol's worth of difference at the join and
        the only spec-ambiguous choice left in the trellis transmit path.
        """
        prev = self.tre.prev if (keep_diff and self.tre is not None) else (0, 0)
        old = self.tre.ts if self.tre is not None else None
        self.tre = TrellisEncoder(ts if ts is not None else old)
        self.tre.prev = prev

    def states(self, seq):
        """Raw carrier states by name, for the start-up signals of 5.2/5.4."""
        return [complex(*ABCD[s]) * self.SCALE_4 for s in seq]

    # -- shaping ---------------------------------------------------------

    def shape(self, syms):
        """Pulse-shape and upconvert, with the pulse evaluated at its exact
        fractional offset rather than rounded to the nearest sample.

        This matters far more at 2400 baud than at 600. Rounding a symbol's
        centre to the nearest sample costs up to half a sample of timing error,
        which is 3.75% of a symbol at V.22's 13.333 samples per symbol -- and
        measured harmless there -- but **15%** at V.32's 3.333. Measured: with
        rounding, the received constellation sat 0.335 from the lattice at a
        known-good sampling phase, against 0.05 for V.22bis. That is ISI put
        there by the transmitter.

        The fractions are exact and there are only three of them. SPS is 10/3, so
        symbol k sits at 10k/3 samples and its fractional part is
        (10k mod 3)/3 = (k mod 3)/3, i.e. 0, 1/3 or 2/3 for ever. So a three-
        phase tap table is not an approximation to the exact filter -- it is the
        exact filter.
        """
        h_n = len(self.h)
        D = (h_n - 1) // 2
        n = (int(round((self.sym + len(syms)) * SPS))
             - int(round(self.sym * SPS)))
        base = list(self.carry) + [0j] * n
        while len(base) < n + h_n:
            base.append(0j)
        for k, a in enumerate(syms):
            c = (self.sym + k) * SPS
            m = int(c)
            p = int(round((c - m) * 3.0)) % 3
            start = m - self.out
            row = self.tab3[p]
            for j in range(h_n):
                base[start + j] += a * row[j]
        self.sym += len(syms)
        self.out += n
        self.carry = base[n:n + h_n - 1]
        while len(self.carry) < h_n - 1:
            self.carry.append(0j)
        out = []
        w = 2 * math.pi * self.fc / SR
        for k in range(n):
            i = self.i + k
            z = base[k]
            v = z.real * math.cos(w * i) - z.imag * math.sin(w * i)
            out.append(max(-32768, min(32767, int(self.amp * v))))
        self.i += n
        return out

    def modulate(self, bits, bps=4, scramble=True):
        return self.shape(self.symbols(bits, bps, scramble))

    def modulate_states(self, seq):
        return self.shape(self.states(seq))

    def flush(self):
        tail, self.carry = self.carry, [0j] * (len(self.h) - 1)
        out = []
        w = 2 * math.pi * self.fc / SR
        for k, z in enumerate(tail):
            i = self.i + k
            v = z.real * math.cos(w * i) - z.imag * math.sin(w * i)
            out.append(max(-32768, min(32767, int(self.amp * v))))
        self.i += len(tail)
        self.out += len(tail)
        return out


# ---------------------------------------------------------------------------
# Receiver modes
# ---------------------------------------------------------------------------
#
# Duck-typed to match tracking.Mode -- name, points, bps, power, r2, m4ref,
# slice(), decode() -- so the same streaming receiver serves V.22, V.22bis and
# V.32 without knowing which it has. The differential coding is the difference:
# V.22bis rotates a quadrant label and names a point inside it, while V.32 runs
# Table 1 on (Y1,Y2) and lets Q3Q4 pick the point.

class V32Mode:
    def __init__(self, name, points, bps, labelled=True):
        self.name = name
        self.points = [complex(*p) if isinstance(p, tuple) else p
                       for p in points]
        self.bps = bps
        n = len(self.points)
        self.power = sum(abs(z) ** 2 for z in self.points) / n
        self.r2 = (sum(abs(z) ** 4 for z in self.points) / n) / self.power
        self.m4ref = _phase(sum(z ** 4 for z in self.points) / n) / 4.0
        self.labelled = labelled
        if not labelled:
            self.labels = []
            self.q_for = {}
            return
        # Table 1 inverted: (prev Y, new Y) -> the Q1Q2 that caused it
        self.q_for = {}
        for (q1, q2, p1, p2), out in TABLE1.items():
            self.q_for[((p1, p2), out)] = (q1, q2)

    def slice(self, z):
        best = self.points[0]
        bd = abs(z - best)
        for p in self.points[1:]:
            d = abs(z - p)
            if d < bd:
                bd, best = d, p
        return best

    def label(self, z):
        p = self.slice(z)
        return POINT_TO_NONRED[(int(round(p.real)), int(round(p.imag)))]

    def decode(self, syms, prev=None):
        """Symbols -> data bits. `prev` is the (Y1,Y2) of the last symbol."""
        bits = []
        for z in syms:
            lab = self.label(z)
            cur = (lab[0], lab[1])
            if prev is not None:
                bits.extend(self.q_for[(prev, cur)])
                if self.bps == 4:
                    bits.extend((lab[2], lab[3]))
            prev = cur
        return bits, prev


def _phase(z):
    return math.atan2(z.imag, z.real)


QAM9600 = V32Mode("9600", list(NONRED.values()), 4)

# Figure 3/V.32: the 32-point constellation for trellis coding at 9600 bit/s.
#
# Table 3's trellis column is the part of the scan that lost its signs beyond
# repair -- it prints "-0" and repeats coordinates that must differ -- so the
# coordinates are not readable. What *is* readable is their magnitudes, and every
# fragment of them, (1,0) (0,1) (1,2) (2,1) (0,3) (3,0) (2,3) (3,2) (1,4) (4,1),
# has Re+Im odd. The integer points with Re+Im odd and |z|^2 <= 17 number exactly
# 32, with mean power exactly 10 -- the same as the 16-point nonredundant set, as
# equal transmitted power at both alternatives requires -- and radii
# 1, sqrt5, 3, sqrt13, sqrt17.
#
# That reconstruction was then checked against two real modems: see
# testrig/v32-ans-path.md. Bridging a call between them and measuring the data
# phase gave radius quantiles of 1.0, 2.29, 3.47/3.70, 4.18/4.26 against the
# predicted 1.000, 2.236, 3.606, 4.123.
TRELLIS_POINTS = [complex(x, y) for x in range(-5, 6) for y in range(-5, 6)
                  if (x + y) % 2 and x * x + y * y <= 17]
TRELLIS9600 = V32Mode("9600T", TRELLIS_POINTS, 4, labelled=False)
QPSK4800 = V32Mode("4800", [ABCD[s] for s in "ABCD"], 2)


# ---------------------------------------------------------------------------
# 5.2 receiver conditioning signal, 5.3 rate signal
# ---------------------------------------------------------------------------

# Table 5/V.32: the direct dibit -> state mapping used after TRN's first 256
# symbols, with the differential encoder disabled.
TABLE5 = {(0, 0): "A", (0, 1): "B", (1, 1): "C", (1, 0): "D"}


def s_states(n=256):
    """5.2.1 Segment 1: alternations of A and B, 256 symbol intervals."""
    return ["AB"[i % 2] for i in range(n)]


def sbar_states(n=16):
    """5.2.2 Segment 2: alternations of C and D, 16 symbol intervals.

    The step from segment 1 to segment 2 is the "well-defined event" the far
    receiver uses as a time reference.
    """
    return ["CD"[i % 2] for i in range(n)]


def trn_states(nsym, taps, first=256):
    """5.2.3 Segment 3: scrambled ones at 4800 bit/s, differential coding off.

    Two halves, and the spec publishes the start of both so an implementation
    can be checked rather than believed. The first `first` symbols take the
    *first* bit of each dibit: 0 -> A, 1 -> C. After that the whole dibit maps
    through Table 5. Scrambler starts at all zeros with a continuous binary one
    on its input.
    """
    scr = Scrambler(taps)
    bits = scr.scramble([1] * (2 * nsym))
    out = []
    for k in range(nsym):
        d = (bits[2 * k], bits[2 * k + 1])
        out.append(("C" if d[0] else "A") if k < first else TABLE5[d])
    return out


# Table 6/V.32. The fixed bits are B0-B3 = 0000, B7 = B11 = B15 = 1; B4-B6 are
# the receive-rate capabilities, B8 says trellis is available at the highest of
# them, and B9-B14 = 001000 means no special operational modes. Signal E
# (Table 7) is the same shape with B0-B3 = 1111 instead.
RATE_FIXED = {0: 0, 1: 0, 2: 0, 3: 0, 7: 1, 9: 0, 10: 0, 11: 1, 12: 0,
              13: 0, 14: 0, 15: 1}
E_FIXED = dict(RATE_FIXED)
E_FIXED.update({0: 1, 1: 1, 2: 1, 3: 1})


def rate_sequence(can2400=False, can4800=True, can9600=True, trellis=False,
                  end=False):
    """A 16-bit rate sequence (Table 6), or signal E (Table 7) if end=True."""
    b = [0] * 16
    for i, v in (E_FIXED if end else RATE_FIXED).items():
        b[i] = v
    b[4] = 1 if can2400 else 0
    b[5] = 1 if can4800 else 0
    b[6] = 1 if can9600 else 0
    b[8] = 1 if trellis else 0
    return b


# ---------------------------------------------------------------------------
# Table 5 and Table 6/V.32 bis: the rate signal, with three more rates
# ---------------------------------------------------------------------------
#
# The same 16-bit sequence, the same seven sync bits (B0-B3, B7, B11, B15) and the
# same two-identical detection rule. What changes is that B4 and B8 are no longer
# capability bits but are both forced to 1 -- which is precisely what Note 1 to
# Table 6/V.32 means by "the combination of B4 equal one and B8 equal one
# indicates V.32 bis operation". A V.32-only modem reads them as "2400 capable"
# and "trellis available", and Note 1 to Table 5/V.32 bis says that if either
# arrives as zero then interworking proceeds under V.32 alone.
#
# The three new rates go in bits V.32 had spare: B9, B10 and B12, which V.32's
# Table 6 required to be 0 as part of "001000 denotes absence of special
# operational modes". So a V.32 bis rate signal offering only 4800 and 9600 is
# bit-identical to a V.32 one except for B4 and B8.
BIS_FIXED = {0: 0, 1: 0, 2: 0, 3: 0, 4: 1, 7: 1, 8: 1, 11: 1, 13: 0, 14: 0,
             15: 1}
BIS_E_FIXED = dict(BIS_FIXED)
BIS_E_FIXED.update({0: 1, 1: 1, 2: 1, 3: 1})
BIS_RATE_BIT = {4800: 5, 9600: 6, 7200: 9, 12000: 10, 14400: 12}
BIS_SYNC = (0, 1, 2, 3, 7, 11, 15)


def parse_any(b):
    """Table 6/V.32 or Table 5/V.32bis, dispatched the way Note 1 to Table 6/V.32
    says to: "the combination of B4 equal one and B8 equal one indicates V.32 bis
    operation". There is no ambiguity to resolve -- a V.32 sequence with both set
    *means* V.32bis -- so one look at two bits picks the table.
    """
    if len(b) == 16 and b[4] and b[8]:
        p = bis_parse_rate(b)
        if p is not None:
            return p
    p = parse_rate(b)
    if p is not None:
        p["bis"] = False
    return p


def bis_rate_sequence(rates=(), end=False):
    """A 16-bit V.32bis rate sequence (Table 5), or signal E (Table 6)."""
    b = [0] * 16
    for i, v in (BIS_E_FIXED if end else BIS_FIXED).items():
        b[i] = v
    for r in rates:
        if r not in BIS_RATE_BIT:
            raise ValueError("no bit for %s bit/s" % r)
        b[BIS_RATE_BIT[r]] = 1
    return b


def bis_parse_rate(b):
    """Decode a V.32bis rate sequence. None if the fixed bits are wrong.

    5.3.1 makes the seven sync bits the detection requirement, so those are what
    is checked; B13 and B14 are "reserved ... ignored during the reception of a
    rate signal" per Note 2, and are ignored here rather than required to be zero.
    """
    if len(b) != 16:
        return None
    end = b[0] == 1 and b[1] == 1 and b[2] == 1 and b[3] == 1
    want = BIS_E_FIXED if end else BIS_FIXED
    for i in BIS_SYNC:
        if b[i] != want[i]:
            return None
    if not (b[4] and b[8]):
        return None                     # Note 1: fall back to V.32 proper
    rates = sorted(r for r, i in BIS_RATE_BIT.items() if b[i])
    return {"end": end, "rates": rates, "bis": True,
            "cleardown": not rates}


def parse_rate(b):
    """Decode a 16-bit rate sequence. Returns None if the fixed bits are wrong.

    5.3.1 makes the minimum for detection "two consecutive identical 16-bit
    sequences each with bits B0-3, B7, 11 and 15 conforming to Table 6", so
    those seven bits are the sync pattern and the rest is payload.
    """
    if len(b) != 16:
        return None
    sync = {0, 1, 2, 3, 7, 11, 15}
    end = b[0] == 1 and b[1] == 1 and b[2] == 1 and b[3] == 1
    want = E_FIXED if end else RATE_FIXED
    for i in sync:
        if b[i] != want[i]:
            return None
    # Table 6 gives B9-B14 = 001000 for "absence of special operational modes",
    # and Note 2 says the other values belong to V.32bis. A V.32-only receiver
    # must therefore reject anything else -- and it has to, because the seven
    # sync bits alone are satisfied by all sixteen bits set, which is exactly
    # what signal B1 descrambles to. Without this check, B1 masqueraded as
    # signal E and the negotiated rate was read off scrambled ones.
    if [b[i] for i in (9, 10, 12, 13, 14)] != [0, 0, 0, 0, 0]:
        return None
    rates = [r for r, bit in ((2400, 4), (4800, 5), (9600, 6)) if b[bit]]
    return {"end": end, "rates": rates, "trellis": bool(b[8]),
            "cleardown": not rates, "special_modes": False}



# ---------------------------------------------------------------------------
# 2.4.1.2 Trellis coding at 9600 bit/s
# ---------------------------------------------------------------------------
#
# V.32's own copy of this is unreadable. Table 3/V.32 survived with its rows
# intact but every sign destroyed -- the OCR stamped "-" on every number and
# prints "-0" -- and Figure 2/V.32 is a figure. So the constellation was first
# reconstructed from Table 3's surviving *magnitudes* and confirmed against two
# hardware modems bridged to each other, and the labelling and the encoder were
# then recovered by fitting the captured symbols. See testrig/v32-ans-path.md.
#
# The naming below, though, does not come from that measurement. It comes from
# **V.32 bis**, whose scan is clean and whose 9600 bit/s mode is V.32's: §2.3.3
# builds it with the same words, Table 1/V.32 bis is V.32's missing Table 2, and
# Figure 2-3/V.32 bis prints all five bits at all 32 points with the signs
# intact. Every one of its 32 magnitudes agrees with Table 3/V.32, so the two
# documents confirm each other, and the recovered code turned out to be this one
# under a different set of names.

# Figure 2-3/V.32 bis: (Y0, Y1, Y2, Q3, Q4) -> (Re, Im)
_FIG23 = {
    (0, 0, 0, 0, 0): (-4,  1),
    (0, 0, 0, 0, 1): ( 0, -3),
    (0, 0, 0, 1, 0): ( 0,  1),
    (0, 0, 0, 1, 1): ( 4,  1),
    (0, 0, 1, 0, 0): ( 4, -1),
    (0, 0, 1, 0, 1): ( 0,  3),
    (0, 0, 1, 1, 0): ( 0, -1),
    (0, 0, 1, 1, 1): (-4, -1),
    (0, 1, 0, 0, 0): (-2,  3),
    (0, 1, 0, 0, 1): (-2, -1),
    (0, 1, 0, 1, 0): ( 2,  3),
    (0, 1, 0, 1, 1): ( 2, -1),
    (0, 1, 1, 0, 0): ( 2, -3),
    (0, 1, 1, 0, 1): ( 2,  1),
    (0, 1, 1, 1, 0): (-2, -3),
    (0, 1, 1, 1, 1): (-2,  1),
    (1, 0, 0, 0, 0): (-3, -2),
    (1, 0, 0, 0, 1): ( 1, -2),
    (1, 0, 0, 1, 0): (-3,  2),
    (1, 0, 0, 1, 1): ( 1,  2),
    (1, 0, 1, 0, 0): ( 3,  2),
    (1, 0, 1, 0, 1): (-1,  2),
    (1, 0, 1, 1, 0): ( 3, -2),
    (1, 0, 1, 1, 1): (-1, -2),
    (1, 1, 0, 0, 0): ( 1,  4),
    (1, 1, 0, 0, 1): (-3,  0),
    (1, 1, 0, 1, 0): ( 1,  0),
    (1, 1, 0, 1, 1): ( 1, -4),
    (1, 1, 1, 0, 0): (-1, -4),
    (1, 1, 1, 0, 1): ( 3,  0),
    (1, 1, 1, 1, 0): (-1,  0),
    (1, 1, 1, 1, 1): (-1,  4),}
TRELLIS_MAP = {k: complex(*v) for k, v in _FIG23.items()}
TRELLIS_LABEL = {v: k for k, v in TRELLIS_MAP.items()}
TRELLIS_POINTS_T = tuple(sorted(TRELLIS_MAP.values(), key=lambda z: (z.real, z.imag)))


def trellis_subset(point):
    """The 4Z2 coset of a point: (Re mod 4, Im mod 4), one of eight.

    Ungerboeck's partition chain for two dimensions is Z2 > D2 > 2Z2 > 2D2 > 4Z2.
    All 32 points lie in the odd coset of D2; inside it the cosets of 2D2 number
    four of eight points, too coarse for three coded bits, and the cosets of 4Z2
    number eight of four -- which is what a rate-2/3 encoder needs. Figure
    2-3/V.32 bis duly makes every (Y0,Y1,Y2) block one of those cosets, so the
    three coded bits choose the subset and the uncoded pair Q3Q4 chooses inside
    it. That is what makes the decoder cheap.
    """
    return (int(round(point.real)) % 4, int(round(point.imag)) % 4)


# (Y0, Y1, Y2) -> the subset's four points, indexed by 2*Q3 + Q4
TRELLIS_SUBSETS = {}
for _k, _z in TRELLIS_MAP.items():
    TRELLIS_SUBSETS.setdefault(_k[:3], [None] * 4)[2 * _k[3] + _k[4]] = _z
TRELLIS_SUBSETS = {k: tuple(v) for k, v in TRELLIS_SUBSETS.items()}
SUBSET_KEYS = tuple(sorted(TRELLIS_SUBSETS))
SUBSET_INDEX = {k: i for i, k in enumerate(SUBSET_KEYS)}
SUBSET_XY = tuple(tuple((z.real, z.imag) for z in TRELLIS_SUBSETS[k])
                  for k in SUBSET_KEYS)


def trellis_point(y0, y1, y2, q3, q4):
    """The point Figure 2-3 assigns to the five bits."""
    return TRELLIS_MAP[(y0, y1, y2, q3, q4)]


# Table 1/V.32 bis, which is V.32's Table 2: differential quadrant coding.
# (Q1, Q2, Y1[n-1], Y2[n-1]) -> (Y1[n], Y2[n]).
_T2_ROWS = (
    (0, 0, 0, 0, 0, 0), (0, 0, 0, 1, 0, 1), (0, 0, 1, 0, 1, 0), (0, 0, 1, 1, 1, 1),
    (0, 1, 0, 0, 0, 1), (0, 1, 0, 1, 0, 0), (0, 1, 1, 0, 1, 1), (0, 1, 1, 1, 1, 0),
    (1, 0, 0, 0, 1, 0), (1, 0, 0, 1, 1, 1), (1, 0, 1, 0, 0, 1), (1, 0, 1, 1, 0, 0),
    (1, 1, 0, 0, 1, 1), (1, 1, 0, 1, 1, 0), (1, 1, 1, 0, 0, 0), (1, 1, 1, 1, 0, 1),
)
TABLE2 = {r[:4]: r[4:] for r in _T2_ROWS}
# and its inverse, which is how a receiver gets the data back:
# (Y1[n-1], Y2[n-1], Y1[n], Y2[n]) -> (Q1, Q2)
TABLE2_DEC = {(r[2], r[3], r[4], r[5]): (r[0], r[1]) for r in _T2_ROWS}


def diff_encode(q1, q2, prev):
    """Table 2: differentially encode Q1Q2 given the previous Y1Y2."""
    return TABLE2[(q1, q2, prev[0], prev[1])]


def diff_decode(prev, cur):
    """Table 2 backwards: recover Q1Q2 from two consecutive Y1Y2."""
    return TABLE2_DEC[(prev[0], prev[1], cur[0], cur[1])]


# The convolutional encoder, Figure 2/V.32 = Figure 1/V.32 bis. The figure does
# not survive text extraction, but it renders: pdftoppm page 5 of the V.32 bis PDF
# and the circuit can be read off directly. Its two gate symbols are defined by a
# shared truth table, "a b -> s1 s2" with s1 = a XOR b (drawn as a circled plus)
# and s2 = a AND b (drawn as a D shape) -- a half-adder.
#
# The drawn circuit is TrellisFSR below. Independently, the relation was fitted
# over GF(2) against 86 789 symbols from a real connection, with zero mismatches
# over every observed context:
#
#   Y0[n] = Y2[n-1] + Y1[n-2] + Y2[n-2] + Y0[n-3]
#         + Y1[n-1].Y0[n-1] + Y1[n-2].Y0[n-2] + Y0[n-1].Y0[n-2]
#
# all sums modulo 2 and "." the AND. Expanding the drawn circuit gives this
# relation term for term, so the figure and the fit agree exactly -- two
# independent routes to the same code, one from the Recommendation and one from
# the wire. The AND terms are not optional and not an artefact of the labelling:
# a purely linear fit is inconsistent. A trellis code
# invariant under 90 degree rotations of a two-dimensional constellation cannot
# be linear (Wei, 1984), and this one is invariant -- rotating every point of
# Figure 2-3 by 90 degrees leaves Q3Q4 alone and shifts Y1Y2 by exactly Table 2's
# Q1Q2 = 11 row, so the differentially decoded data comes out unchanged. That is
# what the AND gate buys.
#
# Y0[n] uses no current input, so the encoder is a Moore machine: the redundant
# bit is a function of the state alone.

def trellis_y0(hist):
    """Y0[n] from the delay line (y1, y2, y0 histories ending at n-1).

    hist is (y1, y2, y0) sequences; y1[-1] is Y1[n-1] and so on.
    """
    y1, y2, y0 = hist
    return ((y2[-1] ^ y1[-2] ^ y2[-2] ^ y0[-3])
            ^ (y1[-1] & y0[-1]) ^ (y1[-2] & y0[-2]) ^ (y0[-1] & y0[-2]))


# Minimising that recursion over its 128-entry delay line collapses it to exactly
# **8 states**, which is what Figure 2 has. Each state has four distinct
# successors and four distinct predecessors, and because the encoder is Moore the
# redundant bit belongs to the state rather than the branch.
# TRELLIS_TABLE[state][2*Y1 + Y2] = (Y0, next state)
TRELLIS_TABLE = (
    ((0, 0), (0, 3), (0, 2), (0, 1)),
    ((1, 4), (1, 6), (1, 5), (1, 7)),
    ((0, 1), (0, 2), (0, 3), (0, 0)),
    ((1, 5), (1, 7), (1, 4), (1, 6)),
    ((0, 2), (0, 1), (0, 0), (0, 3)),
    ((1, 6), (1, 4), (1, 7), (1, 5)),
    ((1, 7), (1, 5), (1, 6), (1, 4)),
    ((0, 3), (0, 0), (0, 1), (0, 2)),
)
TRELLIS_Y0 = tuple(row[0][0] for row in TRELLIS_TABLE)
# branch table for the inner loops: BRANCH[state][2*Y1+Y2] = (subset, next state)
BRANCH = tuple(tuple((SUBSET_INDEX[(TRELLIS_TABLE[s][k][0], k >> 1, k & 1)],
                      TRELLIS_TABLE[s][k][1])
                     for k in range(4))
               for s in range(8))


def trellis_step(state, y1, y2):
    """One branch of the trellis: (Y0, next state)."""
    return TRELLIS_TABLE[state][2 * y1 + y2]


class TrellisFSR:
    """Figure 1/V.32 bis, wired as drawn.

    Three delay elements T1, T2, T3 along a row, with four adders between them
    and two AND gates feeding two of those adders. Reading the figure left to
    right:

      in(T1) = Y0n            the long feedback wire from the output at the far
                              right back to the left end -- this is what makes
                              the encoder recursive
      A1     injects Y1n + Y2n        (the lone adder above the row)
      A2     injects Y0n . N          (the first AND gate)
      A3     injects Y2n              (a tap on the Y2n line)
      A4     injects Y1n . Y0n        (the second AND gate)
      N      the node between A3 and A4, tapped upward to feed the first AND
      Y0n    out(T3)

    Y0n is a delay output, so it is a Moore machine: the redundant bit belongs to
    the state and does not depend on the current input. And because the state
    feeds back from the output, a decoder that starts in the wrong state does not
    resynchronise on its own -- which is why the trellis is searched rather than
    tracked.
    """

    def __init__(self, t1=0, t2=0, t3=0):
        self.t1, self.t2, self.t3 = t1, t2, t3

    def step(self, y1, y2):
        """Clock one symbol: returns Y0 for this symbol."""
        y0 = self.t3
        z1, z2 = self.t1, self.t2
        n = z2 ^ y2                          # after A3, the tapped node
        q = z1 ^ (y1 ^ y2) ^ (y0 & n)        # after A1 then A2 -> in(T2)
        r = n ^ (y1 & y0)                    # after A4 -> in(T3)
        self.t1, self.t2, self.t3 = y0, q, r
        return y0


class TrellisEncoder:
    """The full 9600 bit/s trellis path: four data bits in, one point out.

    Q1Q2 are differentially encoded into Y1Y2 by Table 2, Y1Y2 drive the
    convolutional encoder of Figure 1 to produce Y0, and (Y0,Y1,Y2,Q3,Q4) selects
    the point from Figure 2-3. What runs is the drawn circuit.
    """

    def __init__(self, ts=None):
        self.ts = ts
        self.fsr = TrellisFSR()
        self.prev = (0, 0)

    def _set(self):
        return self.ts if self.ts is not None else TRELLIS_9600T

    def put(self, q1, q2, *q):
        """Q1, Q2 and then the uncoded bits, first in time first."""
        y1, y2 = diff_encode(q1, q2, self.prev)
        self.prev = (y1, y2)
        y0 = self.fsr.step(y1, y2)
        v = 0
        for b in q:
            v = (v << 1) | b
        return self._set().point(y0, y1, y2, v)

    def encode(self, bits):
        """A flat bit list -> points. Each symbol takes the rate's data bits:
        Q1, Q2 and the uncoded ones, so 3 at 7200 and 5 at 12 000."""
        n = self._set().nbits - 1
        return [self.put(*bits[i:i + n])
                for i in range(0, len(bits) - n + 1, n)]


class Viterbi:
    """Maximum-likelihood decoder for the V.32 trellis code.

    Fed one received symbol at a time; releases one decision per symbol once the
    traceback is `depth` deep, so it runs in constant time and constant memory
    and fits inside the RTP callback. Metrics are renormalised every symbol,
    which is what stops them growing without bound over a long call.

    The partition is what makes it cheap: for each of the 8 subsets the receiver
    finds the nearest point in it and how far -- 8 numbers -- and the recursion
    never touches all 32 points. Eight states, four branches: 32
    add-compare-selects per symbol.

    A receiver joining a call does not know the encoder's state, so every state
    starts at metric zero and the survivors converge by themselves.

    Each released decision is (y0, y1, y2, q3, q4, point). Q1Q2 needs two
    consecutive symbols, so it comes from TrellisDecoder rather than from here.
    """

    def __init__(self, depth=24, ts=None):
        self.ts = ts if ts is not None else TRELLIS_9600T
        self.depth = depth
        self.metric = [0.0] * 8
        self.hist = []
        self.symbols = 0

    def _decide(self, x, y):
        """Nearest point in each subset and its squared distance: 8 pairs."""
        d = [0.0] * 8
        q = [0] * 8
        xy = self.ts.xy
        for si in range(8):
            bd = 1e18
            bq = 0
            for j, (px, py) in enumerate(xy[si]):
                dx = x - px
                dy = y - py
                m = dx * dx + dy * dy
                if m < bd:
                    bd = m
                    bq = j
            d[si] = bd
            q[si] = bq
        return d, q

    def feed(self, z):
        """One received symbol in; a list of 0 or 1 released decisions out."""
        d, q = self._decide(z.real, z.imag)
        met = self.metric
        new = [1e18] * 8
        prev = [0] * 8
        brc = [0] * 8
        branch = self.ts.branch
        for s in range(8):
            m = met[s]
            row = branch[s]
            for k in range(4):
                si, ns = row[k]
                cand = m + d[si]
                if cand < new[ns]:
                    new[ns] = cand
                    prev[ns] = s
                    brc[ns] = k
        lo = min(new)
        self.metric = [v - lo for v in new]
        self.hist.append((prev, brc, q))
        self.symbols += 1
        if len(self.hist) <= self.depth:
            return []
        out = [self._traceback()]
        self.hist.pop(0)
        return out

    def _traceback(self):
        """Walk the survivor back to the oldest kept symbol and read it off."""
        state = min(range(8), key=lambda s: self.metric[s])
        for i in range(len(self.hist) - 1, 0, -1):
            state = self.hist[i][0][state]
        prev, brc, q = self.hist[0]
        s0 = prev[state]
        k = brc[state]
        si, _ = self.ts.branch[s0][k]
        key = self.ts.keys[si]
        j = q[si]
        u = self.ts.uncoded
        bits = tuple((j >> (u - 1 - i)) & 1 for i in range(u))
        return key + bits + (self.ts.subsets[key][j],)

    def flush(self):
        """Release the decisions still inside the traceback window."""
        out = []
        while self.hist:
            out.append(self._traceback())
            self.hist.pop(0)
        return out


class TrellisDecoder:
    """Viterbi plus Table 2: received symbols in, the four data bits out.

    The differential step is what makes the whole thing usable on a real line: a
    90 degree phase ambiguity that the receiver never resolves shifts every Y1Y2
    by a constant, and Table 2 cancels a constant shift. The cost is one symbol,
    since Q1Q2 is read from a pair.
    """

    def __init__(self, depth=24, ts=None):
        self.vit = Viterbi(depth, ts)
        self.prev = None

    def _take(self, got, out):
        for dec in got:
            y1, y2 = dec[1], dec[2]
            if self.prev is not None:
                out.append(diff_decode(self.prev, (y1, y2)) + dec[3:-1])
            self.prev = (y1, y2)
        return out

    def feed(self, z):
        """One symbol in; a list of released (Q1, Q2, uncoded...) tuples out."""
        return self._take(self.vit.feed(z), [])

    def flush(self):
        return self._take(self.vit.flush(), [])


def trellis_decode(zs, depth=24, ts=None):
    """Batch Viterbi over a symbol stream: one decision per input symbol.

    Also returns how many decisions differ from the plain nearest-of-32 choice,
    which is a useful link-quality number in its own right -- it counts the
    symbols the code actually had to repair.
    """
    vit = Viterbi(depth, ts)
    pts = vit.ts.points
    out = []
    for z in zs:
        out += vit.feed(z)
    out += vit.flush()
    repaired = sum(1 for z, d in zip(zs, out)
                   if d[-1] != min(pts, key=lambda p: abs(p - z)))
    return out, repaired


def trellis_decode_bits(zs, depth=24, ts=None):
    """Batch: symbols in, a flat list of data bits out (Q1,Q2,Q3,Q4 per symbol)."""
    dec = TrellisDecoder(depth, ts)
    out = []
    for z in zs:
        for q in dec.feed(z):
            out.extend(q)
    for q in dec.flush():
        out.extend(q)
    return out


# ---------------------------------------------------------------------------
# V.32bis: the same trellis code at 7200, 12 000 and 14 400 bit/s
# ---------------------------------------------------------------------------
#
# V.32bis 2.3.1 to 2.3.4 are the same four sentences with a different bit count
# each time: differentially encode Q1Q2 per Table 1/V.32 bis, feed the systematic
# convolutional encoder of Figure 1 for Y0, and map (Y0,Y1,Y2,Q3..Qn) onto a
# constellation. The differential rule and the encoder are shared with V.32's
# 9600 trellis alternative -- already verified against V.32's own Table 2 and
# Figure 2 -- so each added rate is a constellation and nothing else.
#
# Figures 2-4 (7200) and 2-2 (12 000) flatten to clean grids in the text layer,
# 4x4 and 8x8, so they were read from there and then *checked structurally*,
# which is the part that matters: each has its 8 subsets of equal size, each is
# closed under 90 degree rotation, rotation preserves the uncoded bits, and the
# induced shift of Y1Y2 is exactly Table 1's Q1Q2 = 11 row. That is the same
# signature the 9600 mapping has, and a mis-parse would not produce it.
#
# Indexed by the label read as an integer, Y0 first: index = Y0Y1Y2Q3...

_FIG24 = (
    ( 3,-3), (-1, 1), (-3, 3), ( 1,-1),
    ( 3, 1), (-1,-3), (-3,-1), ( 1, 3),
    (-1, 3), ( 3,-1), ( 1,-3), (-3, 1),
    (-3,-3), ( 1, 1), ( 3, 3), (-1,-1),
)

_FIG22 = (
    ( 7, 1), ( 3, 5), ( 7,-7), (-5, 5),
    ( 3,-3), (-1, 1), (-1,-7), (-5,-3),
    (-7,-1), (-3,-5), (-7, 7), ( 5,-5),
    (-3, 3), ( 1,-1), ( 1, 7), ( 5, 3),
    (-1, 5), (-5, 1), ( 7, 5), (-5,-7),
    ( 3, 1), (-1,-3), ( 7,-3), ( 3,-7),
    ( 1,-5), ( 5,-1), (-7,-5), ( 5, 7),
    (-3,-1), ( 1, 3), (-7, 3), (-3, 7),
    (-5,-1), (-1,-5), (-5, 7), ( 7,-5),
    (-1, 3), ( 3,-1), ( 3, 7), ( 7, 3),
    ( 5, 1), ( 1, 5), ( 5,-7), (-7, 5),
    ( 1,-3), (-3, 1), (-3,-7), (-7,-3),
    ( 1,-7), ( 5,-3), (-7,-7), ( 5, 5),
    (-3,-3), ( 1, 1), (-7, 1), (-3, 5),
    (-1, 7), (-5, 3), ( 7, 7), (-5,-5),
    ( 3, 3), (-1,-1), ( 7,-1), ( 3,-5),
)


# Figure 2-1 (14 400) does not flatten usefully -- its rows are offset by half a
# column so the text layer's spacing is irregular, with residuals up to 0.79 of a
# unit. It was read from the PDF's own word coordinates instead (pdftotext -bbox),
# which give every label to a fraction of a point: fit the two axes against the
# tick labels by least squares, then fit the one constant offset between a label
# and the dot above it. All 128 land on the integer lattice with an rms deviation
# of **0.048**, all with Re + Im odd, mean power 41.
_FIG21 = (
    (-8,-3), ( 8,-3), ( 4,-3), ( 4,-7),
    (-4,-3), (-4,-7), ( 0,-3), ( 0,-7),
    (-8, 1), ( 8, 1), ( 4, 1), ( 4, 5),
    (-4, 1), (-4, 5), ( 0, 1), ( 0, 5),
    ( 8, 3), (-8, 3), (-4, 3), (-4, 7),
    ( 4, 3), ( 4, 7), ( 0, 3), ( 0, 7),
    ( 8,-1), (-8,-1), (-4,-1), (-4,-5),
    ( 4,-1), ( 4,-5), ( 0,-1), ( 0,-5),
    ( 2,-9), ( 2, 7), ( 2, 3), ( 6, 3),
    ( 2,-5), ( 6,-5), ( 2,-1), ( 6,-1),
    (-2,-9), (-2, 7), (-2, 3), (-6, 3),
    (-2,-5), (-6,-5), (-2,-1), (-6,-1),
    (-2, 9), (-2,-7), (-2,-3), (-6,-3),
    (-2, 5), (-6, 5), (-2, 1), (-6, 1),
    ( 2, 9), ( 2,-7), ( 2,-3), ( 6,-3),
    ( 2, 5), ( 6, 5), ( 2, 1), ( 6, 1),
    ( 9, 2), (-7, 2), (-3, 2), (-3, 6),
    ( 5, 2), ( 5, 6), ( 1, 2), ( 1, 6),
    ( 9,-2), (-7,-2), (-3,-2), (-3,-6),
    ( 5,-2), ( 5,-6), ( 1,-2), ( 1,-6),
    (-9,-2), ( 7,-2), ( 3,-2), ( 3,-6),
    (-5,-2), (-5,-6), (-1,-2), (-1,-6),
    (-9, 2), ( 7, 2), ( 3, 2), ( 3, 6),
    (-5, 2), (-5, 6), (-1, 2), (-1, 6),
    (-3, 8), (-3,-8), (-3,-4), (-7,-4),
    (-3, 4), (-7, 4), (-3, 0), (-7, 0),
    ( 1, 8), ( 1,-8), ( 1,-4), ( 5,-4),
    ( 1, 4), ( 5, 4), ( 1, 0), ( 5, 0),
    ( 3,-8), ( 3, 8), ( 3, 4), ( 7, 4),
    ( 3,-4), ( 7,-4), ( 3, 0), ( 7, 0),
    (-1,-8), (-1, 8), (-1, 4), (-5, 4),
    (-1,-4), (-5,-4), (-1, 0), (-5, 0),
)


class TrellisSet:
    """One trellis-coded rate: the mapping and the tables a codec needs.

    Splitting this out is what lets V.32's 9600 and V.32bis's other three rates
    share one encoder and one Viterbi. The coded bits (Y0,Y1,Y2) always choose
    one of eight subsets; only the number of uncoded bits changes, from one at
    7200 to four at 14 400.
    """

    def __init__(self, name, rate, flat, nbits):
        self.name = name
        self.rate = rate
        self.nbits = nbits                 # bits per symbol, including Y0
        self.uncoded = nbits - 3
        self.map = {}
        for i, (re_, im) in enumerate(flat):
            key = tuple((i >> (nbits - 1 - k)) & 1 for k in range(nbits))
            self.map[key] = complex(re_, im)
        self.label = {v: k for k, v in self.map.items()}
        self.points = tuple(sorted(self.map.values(),
                                   key=lambda z: (z.real, z.imag)))
        n = len(self.points)
        self.power = sum(abs(z) ** 2 for z in self.points) / n
        # (Y0,Y1,Y2) -> the subset's points, indexed by the uncoded bits
        subs = {}
        for key, z in self.map.items():
            q = 0
            for b in key[3:]:
                q = (q << 1) | b
            subs.setdefault(key[:3], [None] * (1 << self.uncoded))[q] = z
        self.subsets = {k: tuple(v) for k, v in subs.items()}
        self.keys = tuple(sorted(self.subsets))
        self.index = {k: i for i, k in enumerate(self.keys)}
        self.xy = tuple(tuple((z.real, z.imag) for z in self.subsets[k])
                        for k in self.keys)
        self.branch = tuple(
            tuple((self.index[(TRELLIS_TABLE[s][k][0], k >> 1, k & 1)],
                   TRELLIS_TABLE[s][k][1])
                  for k in range(4))
            for s in range(8))

    def point(self, y0, y1, y2, q):
        """q is the uncoded bits as an integer, first-in-time most significant."""
        return self.subsets[(y0, y1, y2)][q]


TRELLIS_7200 = TrellisSet("7200T", 7200, _FIG24, 4)
TRELLIS_9600T = TrellisSet("9600T", 9600, tuple(
    (int(TRELLIS_MAP[k].real), int(TRELLIS_MAP[k].imag))
    for k in sorted(TRELLIS_MAP)), 5)
TRELLIS_12000 = TrellisSet("12000T", 12000, _FIG22, 6)
TRELLIS_14400 = TrellisSet("14400T", 14400, _FIG21, 7)
TRELLIS_SETS = {7200: TRELLIS_7200, 9600: TRELLIS_9600T,
                12000: TRELLIS_12000, 14400: TRELLIS_14400}

# receiver-side modes, for tracking.StreamRx: the constellation and how many
# data bits ride on each symbol
TRELLIS7200 = V32Mode("7200T", TRELLIS_7200.points, 3, labelled=False)
TRELLIS12000 = V32Mode("12000T", TRELLIS_12000.points, 5, labelled=False)
TRELLIS14400 = V32Mode("14400T", TRELLIS_14400.points, 7, labelled=False)
TRELLIS_MODES = {7200: TRELLIS7200, 9600: TRELLIS9600,
                 12000: TRELLIS12000, 14400: TRELLIS14400}


def trellis_free_distance(maxlen=12, ts=None):
    """Minimum squared Euclidean distance between two distinct coded sequences.

    The measure of a trellis code: with both constellations at mean power 10, the
    ratio of this to the uncoded minimum distance *is* the asymptotic coding gain,
    so it is worth computing rather than quoting. Searched as a shortest path over
    pairs of states, starting from a divergence and ending at a remerge.

    A remerge into the same state with a different input is a "parallel
    transition" -- one symbol long -- and its cost is the distance *within* a
    subset, which is why the partition has to put far-apart points together.
    """
    ts = ts if ts is not None else TRELLIS_9600T
    xy, BR = ts.xy, ts.branch
    sd = [[1e18] * 8 for _ in range(8)]
    for a in range(8):
        for b in range(8):
            for (ax, ay) in xy[a]:
                for (bx, by) in xy[b]:
                    if a == b and (ax, ay) == (bx, by):
                        continue
                    m = (ax - bx) ** 2 + (ay - by) ** 2
                    if m < sd[a][b]:
                        sd[a][b] = m
    best = 1e18
    front = {}
    for s in range(8):
        for k1 in range(4):
            for k2 in range(4):
                if k1 == k2:
                    continue
                s1, n1 = BR[s][k1]
                s2, n2 = BR[s][k2]
                w = sd[s1][s2]
                if n1 == n2:
                    best = min(best, w)
                    continue
                if w < front.get((n1, n2), 1e18):
                    front[(n1, n2)] = w
    for _ in range(maxlen):
        nxt = {}
        for (a, b), w in front.items():
            if w >= best:
                continue
            for k1 in range(4):
                s1, n1 = BR[a][k1]
                for k2 in range(4):
                    s2, n2 = BR[b][k2]
                    ww = w + sd[s1][s2]
                    if ww >= best:
                        continue
                    if n1 == n2:
                        best = ww
                    elif ww < nxt.get((n1, n2), 1e18):
                        nxt[(n1, n2)] = ww
        if not nxt:
            break
        front = nxt
    return best
