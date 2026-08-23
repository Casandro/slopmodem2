"""ITU-T V.22bis 2400 bit/s: 16-point QAM, 600 baud.

Figure 2/V.22bis is a figure and is not recoverable from the PDF text. The
labelling below was read off page 6 of the Recommendation directly: a 4x4 grid
of bit pairs, left to right and top to bottom,

    11 01 10 11
    10 00 00 01
    01 00 00 10
    11 10 01 11

with the four 2x2 blocks being quadrants 2, 1, 3, 4 in that same reading order -
which places quadrant 1 upper right, 2 upper left, 3 lower left, 4 lower right,
i.e. the ordinary complex-plane quadrants. On odd coordinates the columns are
x = -3, -1, +1, +3 and the rows y = +3, +1, -1, -3.

§2.5.2.1: the first two bits of a quadbit give a phase *quadrant change*
(Table 1/V.22bis, identical to Table 1/V.22 - 00 +90°, 01 0°, 11 +270°,
10 +180°) and the last two select one of the four points inside the new
quadrant.

Two properties fall out of the labelling and both are required by the
Recommendation, so they serve as checks that it has been read correctly:

  * The four points labelled 01 - (3,1), (-1,3), (-3,-1), (1,-3) - all have
    radius sqrt(10) and sit 90° apart. §2.5.2.2 requires exactly this: at
    1200 bit/s "the signalling elements corresponding to 01 ... shall be
    transmitted irrespective of the quadrant concerned", which is what makes
    V.22bis compatible with V.22, and that only works if those four points form
    a single-amplitude QPSK set.
  * Rotating any quadrant's four points by +90° reproduces the next quadrant's
    with the labels unchanged. Differential quadrant encoding requires that
    symmetry.
"""
import cmath, math
import v22

# (quadrant, last two bits) -> constellation point, from Figure 2/V.22bis
POINTS = {
    (1, (0, 0)): (1, 1),   (1, (0, 1)): (3, 1),   (1, (1, 0)): (1, 3),   (1, (1, 1)): (3, 3),
    (2, (0, 0)): (-1, 1),  (2, (0, 1)): (-1, 3),  (2, (1, 0)): (-3, 1),  (2, (1, 1)): (-3, 3),
    (3, (0, 0)): (-1, -1), (3, (0, 1)): (-3, -1), (3, (1, 0)): (-1, -3), (3, (1, 1)): (-3, -3),
    (4, (0, 0)): (1, -1),  (4, (0, 1)): (1, -3),  (4, (1, 0)): (3, -1),  (4, (1, 1)): (3, -3),
}
POINT_TO_LABEL = {v: k for k, v in POINTS.items()}

# Table 1/V.22bis: first two bits -> quadrant change (degrees)
QUAD_CHANGE = {(0, 0): 90, (0, 1): 0, (1, 1): 270, (1, 0): 180}
CHANGE_QUAD = {v: k for k, v in QUAD_CHANGE.items()}

# +90° steps through the quadrants
NEXT_QUAD = {1: 2, 2: 3, 3: 4, 4: 1}


def quadrant_of(z):
    """Complex-plane quadrant, numbered 1..4 anticlockwise from upper right."""
    if z.real > 0:
        return 1 if z.imag > 0 else 4
    return 2 if z.imag > 0 else 3


def rotate_quad(q, deg):
    for _ in range((deg // 90) % 4):
        q = NEXT_QUAD[q]
    return q


def encode(bits, start_quadrant=1):
    """Quadbits -> complex constellation points (unit = 1 lattice step)."""
    q = start_quadrant
    out = []
    b = list(bits)
    while len(b) % 4:
        b.append(1)
    for i in range(0, len(b), 4):
        first, last = (b[i], b[i + 1]), (b[i + 2], b[i + 3])
        q = rotate_quad(q, QUAD_CHANGE[first])
        x, y = POINTS[(q, last)]
        out.append(complex(x, y))
    return out


def decode(syms):
    """Complex points -> quadbits. Differential, so the absolute start is free."""
    bits = []
    prev_q = None
    for z in syms:
        p = min(POINTS.values(), key=lambda t: abs(z - complex(*t)))
        q, last = POINT_TO_LABEL[p]
        if prev_q is not None:
            deg = 0
            for d in (0, 90, 180, 270):
                if rotate_quad(prev_q, d) == q:
                    deg = d
                    break
            bits.extend(CHANGE_QUAD[deg])
            bits.extend(last)
        prev_q = q
    return bits


def selfcheck():
    """The two structural properties the Recommendation implies."""
    ok = True
    r = [abs(complex(*POINTS[(q, (0, 1))])) for q in (1, 2, 3, 4)]
    if max(r) - min(r) > 1e-9 or abs(r[0] - math.sqrt(10)) > 1e-9:
        ok = False
    ang = sorted((math.degrees(cmath.phase(complex(*POINTS[(q, (0, 1))]))) + 360) % 360
                 for q in (1, 2, 3, 4))
    gaps = [(ang[(i + 1) % 4] - ang[i]) % 360 for i in range(4)]
    if any(abs(g - 90) > 1e-6 for g in gaps):
        ok = False
    for q in (1, 2, 3, 4):
        for last in ((0, 0), (0, 1), (1, 0), (1, 1)):
            x, y = POINTS[(q, last)]
            rx, ry = -y, x                       # rotate +90 degrees
            if POINTS[(NEXT_QUAD[q], last)] != (rx, ry):
                ok = False
    return ok


class Mod(v22.Mod):
    """V.22bis 2400 bit/s modulator."""

    def __init__(self, channel="high", level_dbfs=-18.0, guard_tone=False):
        v22.Mod.__init__(self, channel, level_dbfs, guard_tone)
        self.q = 1

    def symbols_from_bits(self, bits, scramble=True, bps=4):
        """bps=4 is §2.5.2.1 (2400 bit/s, quadbits); bps=2 is §2.5.2.2.

        §2.5.2.2, for the 1200 bit/s phases of the handshake: the dibit gives the
        phase quadrant change exactly as at 2400 bit/s, and "the signalling
        elements corresponding to 01 in the signal constellation shall be
        transmitted irrespective of the quadrant concerned. This ensure[s]
        compatibility with Recommendation V.22."

        Those four points -- (3,1), (-1,3), (-3,-1), (1,-3) -- are all of
        magnitude sqrt(10), so after normalisation they are unit-amplitude like
        V.22's QPSK, but they lie at 18.43° + k*90°, not on the axes. Using
        v22.Mod's exp(j*quadrant) here instead (which is what an earlier version
        did) therefore transmits the 1200 bit/s training rotated 18.43° off the
        16-QAM lattice the far receiver is about to make 16-way decisions
        against. It is spectrally invisible and differentially decodable, which
        is why it went unnoticed; it is still wrong.
        """
        b = self.scr.scramble(bits) if scramble else list(bits)
        while len(b) % bps:
            b.append(1)
        syms = []
        for i in range(0, len(b), bps):
            self.q = rotate_quad(self.q, QUAD_CHANGE[(b[i], b[i + 1])])
            last = (b[i + 2], b[i + 3]) if bps == 4 else (0, 1)
            x, y = POINTS[(self.q, last)]
            syms.append(complex(x, y) / math.sqrt(10.0))
        return syms
