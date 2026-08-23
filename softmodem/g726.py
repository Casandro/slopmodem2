"""ITU-T G.726 32 kbit/s ADPCM (4 bits/sample), encoder and decoder.

Implemented from ITU-T G.726 (12/1990). The 32 kbit/s tables come straight out
of the Recommendation:

  Table 8  - quantizer decision levels: DLN thresholds 0, 80, 178, 246, 300,
             349, 400, plus the -124 boundary below which the encoder emits the
             opposite-sign minimum codeword
  Table 12 - quantizer output levels DQLN: 4, 135, 213, 273, 323, 373, 425
             (and -2048 for the illegal all-zero codeword)
  4.2.4    - W(I) scale factor multipliers: -12, 18, 41, 64, 112, 198, 355, 1122
  FUNCTF   - F(I) = 0 for IM 0..2, 1 for IM 3..5, 3 for IM 6, 7 for IM 7

The arithmetic follows the Recommendation's fixed-point specification, which is
also what the widely used reference C implementation does, so output should be
bit-exact against any conformant codec. That is checked against ffmpeg in
test_g726.py rather than assumed.
"""

_POWER2 = (1, 2, 4, 8, 0x10, 0x20, 0x40, 0x80, 0x100, 0x200,
           0x400, 0x800, 0x1000, 0x2000, 0x4000)

# Table 8/G.726 decision levels for 32 kbit/s
QTAB_32 = (-124, 80, 178, 246, 300, 349, 400)
# Table 12/G.726 output levels (indexed by the 4-bit codeword)
DQLN_32 = (-2048, 4, 135, 213, 273, 323, 373, 425,
           425, 373, 323, 273, 213, 135, 4, -2048)
# 4.2.4 W(I)
WI_32 = (-12, 18, 41, 64, 112, 198, 355, 1122,
         1122, 355, 198, 112, 64, 41, 18, -12)
# FUNCTF F(I)
FI_32 = (0, 0, 0, 1, 1, 1, 3, 7, 7, 3, 1, 1, 1, 0, 0, 0)


def _quan(val, table):
    """Index of the first table entry greater than val."""
    for i, t in enumerate(table):
        if val < t:
            return i
    return len(table)


def _fmult(an, srn):
    """Floating-point style multiply used by the predictor (4.2.6)."""
    anmag = an if an > 0 else ((-an) & 0x1FFF)
    anexp = _quan(anmag, _POWER2) - 6
    if anmag == 0:
        anmant = 32
    elif anexp >= 0:
        anmant = anmag >> anexp
    else:
        anmant = anmag << -anexp
    wanexp = anexp + ((srn >> 6) & 0xF) - 13
    wanmant = (anmant * (srn & 0o77) + 0x30) >> 4
    if wanexp >= 0:
        retval = (wanmant << wanexp) & 0x7FFF
    else:
        retval = wanmant >> -wanexp
    return -retval if ((an ^ srn) < 0) else retval


class G726_32:
    """One direction of a 32 kbit/s ADPCM codec. Encoder and decoder share
    exactly this state machine, which is why they track each other."""

    def __init__(self):
        # Reset values per Table 6/G.726. yl is held scaled by 2^6 relative to
        # yu, so yl = 544 << 6 makes the first step size 544 (yu's own reset
        # value is irrelevant until ap reaches 256, and is overwritten on the
        # first update).
        self.yl = 34816
        self.yu = -24576
        self.dms = 0
        self.dml = 0
        self.ap = 0
        self.a = [0, 0]
        self.b = [0] * 6
        self.pk = [0, 0]
        self.dq = [32] * 6
        self.sr = [32, 32]
        self.td = 0

    # ---- predictor ----
    def _predictor_zero(self):
        s = _fmult(self.b[0] >> 2, self.dq[0])
        for i in range(1, 6):
            s += _fmult(self.b[i] >> 2, self.dq[i])
        return s

    def _predictor_pole(self):
        return (_fmult(self.a[1] >> 2, self.sr[1]) +
                _fmult(self.a[0] >> 2, self.sr[0]))

    def _step_size(self):
        if self.ap >= 256:
            return self.yu
        y = self.yl >> 6
        dif = self.yu - y
        al = self.ap >> 2
        if dif > 0:
            y += (dif * al) >> 6
        elif dif < 0:
            y += (dif * al + 0x3F) >> 6
        return y

    # ---- quantizer ----
    @staticmethod
    def _quantize(d, y):
        dqm = abs(d)
        exp = _quan(dqm >> 1, _POWER2)
        mant = ((dqm << 7) >> exp) & 0x7F
        dl = (exp << 7) + mant
        # SUBTB: DLN = (DL + 4096 - (Y >> 2)) & 4095, i.e. a 12-bit two's
        # complement wrap. This matters: a difference signal large enough to
        # overflow wraps sign and selects the *opposite* codeword, so leaving
        # the value unmasked picks maximum magnitude where the spec picks
        # minimum.
        dln = (dl - (y >> 2)) & 4095
        if dln >= 2048:
            dln -= 4096
        i = _quan(dln, QTAB_32)
        if d < 0:
            return 15 - i
        if i == 0:
            return 15
        return i

    @staticmethod
    def _reconstruct(sign, dqln, y):
        # ADDA: DQL = (DQLN + (Y >> 2)) & 4095, again a 12-bit wrap. A DQL that
        # wraps into the top half means the magnitude underflowed (this is how
        # the illegal all-zero codeword, DQLN = -2048, is handled).
        dql = (dqln + (y >> 2)) & 4095
        if dql >= 2048:
            return -0x8000 if sign else 0
        dex = (dql >> 7) & 15
        dqt = 128 + (dql & 127)
        dq = (dqt << 7) >> (14 - dex)
        return (dq - 0x8000) if sign else dq

    # ---- state update (4.2.4 - 4.2.7) ----
    def _update(self, y, wi, fi, dq, sr, dqsez):
        pk0 = 1 if dqsez < 0 else 0
        mag = dq & 0x7FFF
        ylint = self.yl >> 15
        ylfrac = (self.yl >> 10) & 0x1F
        thr1 = (32 + ylfrac) << ylint
        thr2 = (31 << 10) if ylint > 9 else thr1
        dqthr = (thr2 + (thr2 >> 1)) >> 1
        tr = 1 if (self.td and mag > dqthr) else 0

        # quantizer scale factor adaptation.
        # FILTD: YUT = (Y + DIFSX) & 8191 -- masked to 13 bits *before* LIMB,
        # which matters when the sum overflows, since the wrapped value clamps
        # to the other rail. FILTE likewise masks YLP to 19 bits.
        self.yu = (y + ((wi - y) >> 5)) & 8191
        if self.yu < 544:
            self.yu = 544
        elif self.yu > 5120:
            self.yu = 5120
        self.yl = (self.yl + self.yu + ((-self.yl) >> 6)) & 524287

        # adaptive predictor
        a2p = 0
        if tr == 1:
            self.a = [0, 0]
            self.b = [0] * 6
        else:
            pks1 = self.pk[0] ^ pk0
            a2p = self.a[1] - (self.a[1] >> 7)
            if dqsez != 0:
                fa1 = self.a[0] if pks1 else -self.a[0]
                if fa1 < -8191:
                    a2p -= 0x100
                elif fa1 > 8191:
                    a2p += 0xFF
                else:
                    a2p += fa1 >> 5
                # UPA2: PKS2 = PK0 XOR PK2, i.e. the *new* sign against the
                # one from two samples back - not PK1 XOR PK2.
                if pk0 ^ self.pk[1]:
                    if a2p <= -12160:
                        a2p = -12288
                    elif a2p >= 12416:
                        a2p = 12288
                    else:
                        a2p -= 0x80
                else:
                    if a2p <= -12416:
                        a2p = -12288
                    elif a2p >= 12160:
                        a2p = 12288
                    else:
                        a2p += 0x80
            self.a[1] = a2p
            self.a[0] -= self.a[0] >> 8
            if dqsez != 0:
                if pks1 == 0:
                    self.a[0] += 192
                else:
                    self.a[0] -= 192
            a1ul = 15360 - a2p
            if self.a[0] < -a1ul:
                self.a[0] = -a1ul
            elif self.a[0] > a1ul:
                self.a[0] = a1ul
            for c in range(6):
                self.b[c] -= self.b[c] >> 8
                if mag:
                    if (dq ^ self.dq[c]) >= 0:
                        self.b[c] += 128
                    else:
                        self.b[c] -= 128

        # FLOAT A: dq history in 4-bit exponent / 6-bit mantissa form
        self.dq = [0] + self.dq[:5]
        if mag == 0:
            self.dq[0] = 0x20 if dq >= 0 else -0x3E0      # 0xFC20 as signed
        else:
            exp = _quan(mag, _POWER2)
            v = (exp << 6) + ((mag << 6) >> exp)
            self.dq[0] = v if dq >= 0 else v - 0x400

        # FLOAT B: sr history
        self.sr[1] = self.sr[0]
        if sr == 0:
            self.sr[0] = 0x20
        elif sr > 0:
            exp = _quan(sr, _POWER2)
            self.sr[0] = (exp << 6) + ((sr << 6) >> exp)
        elif sr > -32768:
            m = -sr
            exp = _quan(m, _POWER2)
            self.sr[0] = (exp << 6) + ((m << 6) >> exp) - 0x400
        else:
            self.sr[0] = -0x3E0

        self.pk[1] = self.pk[0]
        self.pk[0] = pk0

        # tone detect
        if tr == 1:
            self.td = 0
        else:
            self.td = 1 if a2p < -11776 else 0

        # adaptation speed control. FILTA/FILTB take FI scaled by 2^9, so the
        # caller passes F(I) << 9 and FILTB effectively uses F(I) << 11.
        self.dms += (fi - self.dms) >> 5
        self.dml += ((fi << 2) - self.dml) >> 7
        if tr == 1:
            self.ap = 256
        elif y < 1536:
            self.ap += (0x200 - self.ap) >> 4
        elif self.td == 1:
            self.ap += (0x200 - self.ap) >> 4
        elif abs((self.dms << 2) - self.dml) >= (self.dml >> 3):
            self.ap += (0x200 - self.ap) >> 4
        else:
            self.ap += (-self.ap) >> 4

    # ---- public ----
    def encode_sample(self, sl16):
        """16-bit linear sample -> 4-bit codeword."""
        sl = sl16 >> 2                      # to the 14-bit internal range
        sezi = self._predictor_zero()
        sez = sezi >> 1
        se = (sezi + self._predictor_pole()) >> 1
        d = sl - se
        y = self._step_size()
        i = self._quantize(d, y)
        dq = self._reconstruct(i & 8, DQLN_32[i], y)
        sr = (se - (dq & 0x3FFF)) if dq < 0 else (se + dq)
        dqsez = sr + sez - se
        self._update(y, WI_32[i] << 5, FI_32[i] << 9, dq, sr, dqsez)
        return i

    def decode_sample(self, i):
        """4-bit codeword -> 16-bit linear sample."""
        sezi = self._predictor_zero()
        sez = sezi >> 1
        se = (sezi + self._predictor_pole()) >> 1
        y = self._step_size()
        dq = self._reconstruct(i & 8, DQLN_32[i], y)
        sr = (se - (dq & 0x3FFF)) if dq < 0 else (se + dq)
        dqsez = sr + sez - se
        self._update(y, WI_32[i] << 5, FI_32[i] << 9, dq, sr, dqsez)
        v = sr << 2
        return max(-32768, min(32767, v))


def encode(samples):
    """16-bit linear samples -> list of 4-bit codewords."""
    st = G726_32()
    return [st.encode_sample(int(s)) for s in samples]


def decode(codes):
    """4-bit codewords -> 16-bit linear samples."""
    st = G726_32()
    return [st.decode_sample(int(c)) for c in codes]


def pack(codes):
    """Pack codewords for RTP (RFC 3551): first codeword in the four most
    significant bits of the first octet."""
    out = bytearray()
    for j in range(0, len(codes) - 1, 2):
        out.append(((codes[j] & 0xF) << 4) | (codes[j + 1] & 0xF))
    if len(codes) % 2:
        out.append((codes[-1] & 0xF) << 4)
    return bytes(out)


def unpack(data, n=None):
    codes = []
    for b in data:
        codes.append((b >> 4) & 0xF)
        codes.append(b & 0xF)
    return codes[:n] if n else codes


class Encoder:
    """Stateful encoder for streaming use.

    G.726 carries adaptation state across samples, so a call must use one
    encoder for its whole duration - encoding each 20 ms frame with a fresh
    state would resynchronise the predictor 50 times a second and the decoder
    would never track it.
    """

    def __init__(self):
        self.st = G726_32()

    def encode_frame(self, samples):
        return pack([self.st.encode_sample(int(v)) for v in samples])
