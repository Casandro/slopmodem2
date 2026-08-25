"""ITU-T V.32 §5.4 start-up, both sides.

Figure 4 is two round trips. The answering modem leads with the V.25 answer tone
and the 600/3000 Hz pair; the calling modem answers with a pure 1800 Hz tone and
a pair of phase reversals that let each side measure the round-trip delay; then
each in turn sends a receiver conditioning signal (S, S-bar, TRN) and a rate
signal, and the rates are negotiated R1 -> R2 -> R3 before signal E hands over to
data.

    answer   ANS  AC   CA  AC  |  S S TRN R1  |  ...  S S TRN R3 E B1  Data
    call          AA   CC      |              |  S S TRN R2 E B1       Data

Detectors, calibrated against generated signals rather than assumed. Normalised
Goertzel energies per 160-sample frame:

    signal    e600    e1800   e3000   level
    AA / CC   0.000   0.750   0.000   -37.5 dBFS
    AC / CA   0.375   0.000   0.375   -37.6
    S / S-bar 0.183   0.382   0.184   -37.5
    TRN       0.007   0.028   0.005   -37.5
    R         0.010   0.013   0.012   -37.5
    data      0.011   0.010   0.008   -30.4

Two things fall out of that table. The three start-up tones are cleanly
separable: a pure carrier, a carrier-suppressed pair, and S which has both. And
the whole handshake runs **7 dB below the data phase**, because §5.2 trains on
the A/B/C/D subset whose mean power is 2 against the 16-point set's 10 --
10*log10(10/2) = 7.0 dB, and the measurement agrees. The receiver has to step its
gain at that boundary rather than be surprised by it.

Known deviation, stated rather than buried: §5.4 wants the AA->CC and CA->AC turn
-round to appear on line 64 ± 2 symbol intervals after the phase reversal that
triggered it. A symbol is 0.417 ms and our detection granularity is a 40-sample
sub-window (5 ms, 12 symbols) -- the finest window that is a whole number of
1800 Hz cycles, so the finest whose phase is comparable with the next one's.
Transmission is scheduled at symbol resolution, so the turn-round lands within
about ±12T of the requirement rather than ±2T. Both sides here quantise the same
way, so the round-trip estimates agree with each other; against hardware the
estimate would carry that error.
"""
import cmath, math
import dsp, dte, echo as echomod, tracking, v32, v42

SR = 8000
FRAME = 160
T = 1.0 / v32.BAUD                  # one symbol interval
SYM_PER_FRAME = 48                  # exactly, at 2400 baud

# --- durations, in symbol intervals -----------------------------------------
S_LEN = 256                         # 5.2.1
SBAR_LEN = 16                       # 5.2.2
# 5.2.3 makes 1280 the minimum and says outright that "transmission of the TRN
# segment ... may be extended in order to ensure a satisfactory level of echo
# cancellation". Measured off a real modem-to-modem call through the bridge, the
# answering modem's whole conditioning-plus-rate-signal phase ran 5.6 s against
# our 0.64 s, so the minimum is not what hardware actually sends. Settable
# because the far end's convergence is the thing being tested.
TRN_MIN = 1280                      # 5.2.3
B1_LEN = 128                        # 5.4.1 / 5.4.2
TURNROUND = 64                      # 5.4.1 / 5.4.2, +/- 2

# 5.4.1 and 5.4.2 pin the turnaround to 64 +/- 2 T, measured on the *line*: "the
# time delay between the reception of this phase reversal at the line terminals
# and the transmitted CA to AC transition appearing at the line terminals". Any
# latency between the line and the state machine therefore has to come out of
# the pad, not be added to it.
#
# The echo canceller holds one frame -- 160 samples, 48 T -- so with it enabled
# our reaction was 48 T late and the turnaround came out at about 112 T against a
# tolerance of 2. Measured: MT read 192 T with the canceller off and 240 T with
# it on, the difference being exactly its hold. The Conexant tolerated it; the
# Cirrus did not, and simply stopped transmitting after the reversal and waited
# for a handshake that never arrived on its schedule.
QUIET16 = 16
# 5.5: the far end asks for a retrain by going back to a carrier state and
# holding it. The trigger is "more than 128 symbol intervals" of the other side's
# tone -- 1800 Hz for the answerer, one of the 600/3000 pair for the caller.
# 5.4.2 gives no length for the answerer's R1, but Table 6's detection rule
# needs two identical 16-bit sequences, so 128 symbols is eight of them.
R1_MIN = 128

# Table 4/V.32's note defines MT and NT as "round-trip delays ... including
# 64T +/- 2T modem turn round delay". A round trip on this rig is tens of
# milliseconds, and every healthy call measured between 48T and 240T. A
# measurement far outside that is not a long round trip, it is a failed
# measurement -- and MT is then spent sitting silent, so honouring 14.8 s of it
# (measured, once) guarantees the call dies. Bounded, and the clamp is logged
# rather than silent.
MT_MIN = 16
MT_MAX = 1200               # 0.5 s, far beyond any real round trip here
MT_DEFAULT = 64             # the turn-round delay alone, when we have nothing

# 5.4.2 puts no limit on how long CA may run waiting for the caller's phase
# reversal. In practice the caller does not wait: it sends S, its conditioning
# signal and R2 on its own schedule, so a CA segment measured in seconds means
# the reversal was missed and the rest of the handshake has already gone past.
CA_MAX = 2400               # 1 s
# 5.3.2 makes signal E a *single* 16-bit sequence, which at 2 bits per symbol is
# eight symbols -- a 3.3 ms window, once, and then the caller is in B1 at a
# different rate and a different constellation. Miss it and there is nothing left
# to detect, so R3TX has to be able to give up and retrain rather than sit there.
# Measured, E arrives 75 to 100 ms after R3 begins; a second is not marginal.
E_MAX = 2400                # 1 s of R3 with no E

RETRAIN_TONE = 128
# 5.5 also permits a retrain on "unsatisfactory signal reception", which is
# optional and left to the implementation. Ours is the equaliser losing its
# decision-directed lock in the data phase for a sustained period.
RETRAIN_LOSS = 2400                 # symbols = 1 s of lost lock
RETRAIN_SETTLE = 4800               # symbols after entering DATA before judging
RETRAIN_MAX = 4                     # consecutive attempts before giving up
# Frames of a shut eye before the gain is measured again; 0 disables it. The
# mechanism exists because a one-shot correction cannot recover a 20% scale error
# on 128 points -- but a *noisy* channel also shuts the eye, and re-measuring
# then corrects nothing and disturbs a loop that was merely struggling. Kept
# settable so the two cases can be told apart by experiment rather than argument.
REGAIN_EVERY = 50                        # 5.4.2, after the amplitude drop
# A data phase this long means the rate is viable, so a later retrain is a
# transient rather than evidence against it. 24000 symbols is 10 s at 2400
# baud; measured, a rate that cannot hold collapses in three to four.
RATE_PROVEN_SYM = 24000
TONE_HOLD = 64                      # 5.4.2, 1800 Hz for 64 symbol periods

# --- states ----------------------------------------------------------------
(ANS, AC1, CA, AC2, QUIET, RC1, R1TX, WAITMT, HUNT2, RC2, R3TX, ETX, B1TX,
 DATA, FAILED) = ("ANS", "AC1", "CA", "AC2", "QUIET", "RC1", "R1TX", "WAITMT",
                  "HUNT2", "RC2", "R3TX", "ETX", "B1TX", "DATA", "FAILED")
(WAITANS, AA, CC, IDLE, WAITS, STX_NT, R2TX, WAITE) = (
    "WAITANS", "AA", "CC", "IDLE", "WAITS", "STX_NT", "R2TX", "WAITE")


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------

def energies(x):
    """(mean square, e600, e1800, e3000) with the three normalised."""
    ms = dsp.mean_square(x)
    if ms < 1.0:
        return 0.0, 0.0, 0.0, 0.0
    return (ms, dsp.goertzel(x, 600.0) / ms, dsp.goertzel(x, 1800.0) / ms,
            dsp.goertzel(x, 3000.0) / ms)


def is_tone1800(e):
    ms, e6, e18, e30 = e
    return e18 > 0.50 and (e6 + e30) < 0.10


def is_pair(e):
    ms, e6, e18, e30 = e
    return (e6 + e30) > 0.50 and e18 < 0.10


def is_S(e):
    """S and S-bar both: all three bands, with the carrier dominant."""
    ms, e6, e18, e30 = e
    return e18 > 0.25 and (e6 + e30) > 0.25


def sub_phase(x, hz, base=0, win=40, step=10):
    """Phase of `hz` in overlapping windows, referenced to the absolute sample
    index so that any two windows anywhere in the stream are comparable.

    The earlier version of this compensated for the stride within one frame and
    reset at each frame boundary, which put a step of its own at every boundary.
    Referencing to `base + k` instead needs no compensation at all, because a
    160-sample frame is a whole number of cycles at every frequency here: 36 at
    1800 Hz, 12 at 600, 60 at 3000. This is the same trap as before, one level
    up -- the fix is to make the reference absolute, not to pick a stride.
    """
    out = []
    w = 2 * math.pi * hz / SR
    for i in range(0, len(x) - win + 1, step):
        acc = 0j
        for k in range(win):
            acc += x[i + k] * cmath.exp(-1j * w * (base + i + k))
        out.append(cmath.phase(acc))
    return out


class Reversal:
    """Locate 180 degree phase reversals in a tone, at sub-frame resolution.

    Triggering on a phase *jump* does not work on a pulse-shaped signal. The
    carrier does turn through 180 degrees, but it does so gradually: measured
    across a real AC-to-CA transition with a 40-sample window stepped by 5, the
    largest change between consecutive windows was 0.8 rad, so a 2 rad threshold
    never fires and a threshold low enough to fire also fires on nothing.

    What is unmistakable is the amplitude null. A window straddling the reversal
    has its two halves cancelling, so the magnitude collapses -- 10002 to 1940 in
    that measurement, better than 5:1 -- and then recovers. So: watch for the
    dip, take the minimum inside it as the instant (symmetric, hence unbiased),
    and use the phase before against the phase after only to confirm that it was
    a reversal and not a fade.
    """

    def __init__(self, hz, win=40, step=5, dip=0.5, confirm=1.6):
        self.hz = hz
        self.win = win
        self.step = step
        self.dip = dip
        self.confirm = confirm      # radians; a reversal is pi apart
        self.ref = 0.0              # settled magnitude
        self.in_dip = False
        self.best = None            # (centre, mag) inside the current dip
        self.pre_phase = None
        self.armed = False
        self.count = 0
        self.at = []
        self.nsamp = 0
        self.buf = []
        self.buf_base = 0
        self.pos = 0

    def feed(self, x, base_sym=0.0):
        hits = []
        self.buf.extend(x)
        self.nsamp += len(x)
        w = 2 * math.pi * self.hz / SR
        while self.pos - self.buf_base + self.win <= len(self.buf):
            i = self.pos - self.buf_base
            acc = 0j
            for k in range(self.win):
                acc += self.buf[i + k] * cmath.exp(-1j * w * (self.pos + k))
            mag = abs(acc)
            ph = cmath.phase(acc)
            centre = self.pos + self.win / 2.0
            if not self.in_dip:
                if self.ref > 0 and mag < self.dip * self.ref and self.armed:
                    self.in_dip = True
                    self.best = (centre, mag)
                else:
                    # settled: track the reference, and remember the phase
                    self.ref = max(mag, 0.9 * self.ref)
                    if mag > 0.8 * self.ref:
                        self.pre_phase = ph
            else:
                if mag < self.best[1]:
                    self.best = (centre, mag)
                if mag > self.dip * self.ref:
                    self.in_dip = False
                    d = math.pi
                    if self.pre_phase is not None:
                        d = abs((ph - self.pre_phase + math.pi)
                                % (2 * math.pi) - math.pi)
                    if d > self.confirm:
                        self.count += 1
                        sym = self.best[0] / (SR * T)
                        self.at.append(sym)
                        hits.append(sym)
                    self.pre_phase = ph
            self.pos += self.step
        drop = self.pos - self.buf_base
        if drop > 0:
            del self.buf[:drop]
            self.buf_base += drop
        return hits


class RateScanner:
    """Find 16-bit rate sequences (5.3.1) in a descrambled bit stream.

    Once alignment is established it is kept. Re-searching bit by bit on every
    sequence lets the scanner slide across a boundary and lock onto a window that
    happens to satisfy the seven sync bits -- which is how signal E, sent
    immediately after the last rate sequence, came out reading [2400, 4800]
    instead of [9600].
    """

    def __init__(self):
        self.bits = []
        self.last = None
        self.aligned = False
        self.confirmed = None       # the parsed sequence, once accepted

    def feed(self, bits):
        self.bits.extend(bits)
        out = []
        while len(self.bits) >= 16:
            window = self.bits[:16]
            p = v32.parse_any(window)
            if p is None:
                if self.aligned:
                    # keep the alignment; this sequence is simply not a rate
                    # signal (B1, data, or a hit of noise)
                    self.bits = self.bits[16:]
                    self.last = None
                    continue
                self.bits.pop(0)            # still hunting
                continue
            self.bits = self.bits[16:]
            # 5.3.1: two consecutive identical sequences is the minimum for
            # detecting a rate signal, and that is also what establishes
            # alignment. Signal E is a single 16-bit sequence (5.3.2) so it must
            # be accepted on one occurrence -- but only once aligned, because E
            # always follows a rate signal. Accepting a lone "end" window while
            # still hunting let a chance pattern inside TRN lock the scanner to
            # the wrong phase for the rest of the call.
            if self.last == window and not p["end"]:
                self.aligned = True
                self.confirmed = p
                out.append(p)
            elif p["end"] and self.aligned:
                self.confirmed = p
                out.append(p)
            self.last = window
        return out


# ---------------------------------------------------------------------------
# a transmitter that can switch signal at symbol resolution
# ---------------------------------------------------------------------------

class _Tx:
    """Symbol-level transmit scheduling on top of v32.Mod.

    The state machines need to change signal partway through a frame -- §5.4's
    turn-round is 64 symbol intervals, which is 1.33 frames -- so symbols are
    generated one at a time and the frame boundary is only where 160 samples get
    handed over.
    """

    def __init__(self, mod):
        self.mod = mod
        self.outq = []
        self.nsym = 0.0             # symbols emitted, absolute
        self.mode = "quiet"
        self.left = None            # symbols remaining in this mode, or None
        self.bits = []              # pending bits for the bit-driven modes
        self.bps = 2
        self.trellis = False
        self.ts = None              # the trellis set, when one is in use
        self.states = []            # pending raw states
        self.on_done = None

    def set(self, mode, count=None, states=None, bits=None, bps=2, on_done=None,
            trellis=False, ts=None):
        self.mode = mode
        self.left = count
        self.states = list(states) if states else []
        self.bits = list(bits) if bits else []
        self.bps = bps
        self.trellis = trellis
        self.ts = ts
        self.on_done = on_done

    def _one(self):
        """One symbol, as a complex point, or None for silence."""
        if self.mode == "quiet":
            return None
        if self.states:
            return complex(*v32.ABCD[self.states.pop(0)]) * v32.Mod.SCALE_4
        if self.bits:
            need = self.bps
            if len(self.bits) < need:
                return None
            take, self.bits = self.bits[:need], self.bits[need:]
            pts = self.mod.symbols(take, bps=self.bps, scramble=True,
                                   trellis=self.trellis, ts=self.ts)
            return pts[0] if pts else None
        return None

    def fill(self, n, machine):
        while len(self.outq) < n:
            if (self.left is not None and self.left <= 0) or \
                    (self.mode != "quiet" and not self.states and not self.bits):
                cb, self.on_done = self.on_done, None
                self.mode, self.left = "quiet", None
                if cb:
                    cb(machine)
                continue
            s = self._one()
            if self.left is not None:
                self.left -= 1
            self.nsym += 1
            if s is None:
                self.outq.extend([0] * int(round(v32.SPS)))
            else:
                self.outq.extend(self.mod.shape([s]))

    def take(self, n):
        out, self.outq = self.outq[:n], self.outq[n:]
        while len(out) < n:
            out.append(0)
        return out


class _Rx:
    """Receive side of the handshake: symbols -> dibits -> rate sequences.

    Runs in the four-point mode throughout the conditioning and rate-signal
    phases, which is what TRN and the rate signals actually are, and switches to
    the negotiated data constellation at signal E. The taps are rescaled at that
    switch because the handshake runs 7 dB below the data phase -- §5.2 trains on
    the A/B/C/D subset whose mean power is 2 against the 16-point set's 10.
    """

    def __init__(self, far_taps, level_hint=-24.0):
        self.rx = tracking.StreamRx(carrier=v32.CARRIER, mode=v32.QPSK4800,
                                    sps=v32.SPS, baud=v32.BAUD,
                                    beta=v32.ROLLOFF, span=10, acq_min=400,
                                    acq_win=400, settle=200)
        self.descr = v32.Scrambler(far_taps)
        self.scan = RateScanner()
        self.prev_y = None
        self.mode = v32.QPSK4800
        self.data_syms = []
        self.data_bits = []
        self.on_data = False
        self.vit = None             # the trellis decoder, when one is in use
        # 7.2: "...then passed to the converter in conformity with
        # Recommendation V.14 for regaining the data stream of start-stop
        # characters." This is that converter.
        self.framer = tracking.AsyncFramer()
        self.chars = bytearray()
        self.ec_bits = None         # set to a list when V.42 wants the bits
        self.gate = False           # is the eye open enough to believe framing?
        self.gates = 0
        self.shut = 0
        self.regains = 0

    def feed(self, samples):
        syms = self.rx.feed(samples)
        if not syms:
            return []
        if self.on_data:
            self.data_syms.extend(syms)
            got = self.descr.descramble(self._data_bits(syms))
            self.data_bits.extend(got)
            # Only frame while the eye is actually open. StreamRx already keeps a
            # fast decision-error estimate and gates its own output on it, which
            # is the same question asked one layer down.
            #
            # Without this the framer runs through the settling period after the
            # constellation switch, and its acquisition sweep dumps up to `cap`
            # buffered characters every time it re-acquires. At 9600 that cost a
            # few dozen junk bytes; at 14 400, where the eye takes longer to open
            # on 128 points and there are six bits a symbol to get wrong, it
            # re-acquired 388 times and emitted 14 186 characters where 640 had
            # been sent. The bits were right the whole time -- 0 trellis
            # mismatches, 100% ones once settled -- so this was the deframer
            # reporting its own confusion as data.
            # Feed it always and gate the *output*. Gating the input splices the
            # bit stream, and a deframer handles a splice exactly as badly as an
            # equaliser does -- which is written up above for the sample stream
            # and was rediscovered here: gating the feed left 12 000 and 14 400
            # corrupt after the eye had opened. On the transition to a good eye
            # the framer is rebuilt, so it acquires on clean bits instead of
            # carrying its confusion across.
            open_ = self.rx.fast_err < self.rx.out_thresh
            # A one-shot gain correction at the constellation switch is enough
            # for 32 points and not for 128: measured, the output sat 17 to 27%
            # under scale and the decision-directed loop cannot pull in from
            # there -- every decision is wrong, so the error it adapts on is
            # noise. If the eye stays shut, measure the gain again rather than
            # waiting for a loop that has nothing to work with.
            if not open_:
                self.shut += 1
                if REGAIN_EVERY and self.shut % REGAIN_EVERY == 0:
                    self.rx.rescale_to(self.mode)
                    self.regains += 1
            else:
                self.shut = 0
            out = self.framer.feed(got)
            if open_ and not self.gate:
                self.framer = tracking.AsyncFramer()
                self.gate = True
                self.gates += 1
                out = b""
            elif not open_:
                self.gate = False
            if self.gate:
                self.chars.extend(out)
                # V.42 rides the same descrambled stream. It is only fed once the
                # eye is open: HDLC would discard junk frames on the FCS anyway,
                # but the 7.2.1 detection phase is a pattern match with a 750 ms
                # timer and garbage bits can decide it the wrong way.
                if self.ec_bits is not None:
                    self.ec_bits.extend(got)
            return []
        bits, self.prev_y = self.mode.decode(syms, self.prev_y)
        # Descramble always -- the descrambler is self-synchronising and must
        # keep consuming -- but only offer bits to the rate scanner once the
        # equaliser has actually converged.
        #
        # 5.3.1's "two consecutive identical sequences" is protection against
        # noise, not against a receiver that is wrong the same way twice. The
        # Cirrus's R2 was accepted 0.13 s after the receiver opened, reading
        # [9600] trellis=False; decoding the same signal offline once locked
        # gives [9600] trellis=True, 10747 times over. B8 is not one of the seven
        # sync bits, so nothing in the sequence itself objected -- exactly the
        # failure mode that made a caller offering only 4800 come out at 9600.
        # 5.2.3 calls TRN "intended for training the adaptive equalizer", so
        # waiting for it to have done its job is the Recommendation's own order
        # of operations.
        out = self.descr.descramble(bits)
        if not self.rx.dd:
            return []
        return self.scan.feed(out)

    def _data_bits(self, syms):
        """Symbols -> the far end's scrambled bit stream, in whichever coding
        was negotiated. The trellis path is stateful across calls -- the Viterbi
        traceback spans symbols and the differential decode spans a pair -- so
        the decoder is an object rather than a function."""
        if self.vit is not None:
            # StreamRx already delivers symbols at constellation scale -- its
            # equaliser adapts against mode.points -- so nothing is rescaled
            # here. Dividing by Mod.SCALE was wrong and put every symbol a
            # factor of 3.16 off the lattice.
            out = []
            for z in syms:
                for q in self.vit.feed(z):
                    out.extend(q)
            return out
        bits, self.prev_y = self.mode.decode(syms, self.prev_y)
        return bits

    def to_handshake(self):
        """5.5: back to the four-point mode for a fresh conditioning signal.

        Timing and carrier lock are kept -- the retrain is about the equaliser
        and the negotiation, not about finding the signal again -- but the
        constellation, the differential state, the trellis decoder and the rate
        scanner all have to go back to where 5.4 expects them.
        """
        self.rx.rescale_to(v32.QPSK4800)
        self.mode = v32.QPSK4800
        self.on_data = False
        self.vit = None
        self.prev_y = None
        self.scan = RateScanner()
        self.framer = tracking.AsyncFramer()
        self.data_syms = []
        self.data_bits = []

    def to_data(self, mode, trellis=False, ts=None):
        """Switch to the data constellation, keeping timing and equaliser.

        The taps are *not* rescaled, and that is not an oversight. The handshake
        does run 7 dB below the data phase -- 5.2 trains on the A/B/C/D subset
        whose mean power is 2 against the data set's 10 -- but the transmitter
        scales both by the same 1/sqrt(10), so the wire amplitude rises by
        sqrt(5) at the switch and the target constellation is sqrt(5) larger too.
        The same taps therefore already produce the right output scale.

        This code used to divide the taps by sqrt(5), which is the error the two
        cancelling factors invite. It survived because the answerer's
        decision-directed loop reconverged from it; the caller's did not, and sat
        at 0.519 of the correct scale with a median distance of 0.496 to the
        lattice, decoding to noise. It only became visible once the data phase
        started producing bits to check.
        """
        # Switch the constellation and let the receiver measure the gain the
        # new one needs. See StreamRx.rescale_to: whether the far end's level
        # steps at this moment is its business, not ours to assume.
        self.rx.rescale_to(mode)
        # No re-acquisition here. It was added when the caller could enter the
        # data phase with a wrecked equaliser, but that turned out to be the
        # frozen-frame defect in step(), and once that was fixed a blind restart
        # became measurably pointless offline -- identical results with and
        # without, on every negotiated mode.
        #
        # Against real hardware it is worse than pointless. The carrier loop only
        # runs in decision-directed mode, so dropping to CMA stops tracking the
        # carrier: the first live V.32 call held its radius (median distance
        # 0.185) while the constellation rotated underneath it (90th percentile
        # 0.849), and assess() could never get a 32-point eye back under its
        # threshold to hand over again. The taps trained on TRN are good and
        # correctly scaled; only the decisions change. So dd carries straight
        # through the switch.
        self.mode = mode
        self.on_data = True
        self.vit = v32.TrellisDecoder(ts=ts) if trellis else None

    def locked(self):
        return self.rx.dd


class _Base:
    """Shared plumbing: one transmitter, one receiver, an event log."""

    def __init__(self, level_dbfs, my_taps, far_taps, log=None):
        self.mod = v32.Mod(level_dbfs=level_dbfs, scrambler_taps=my_taps)
        self.tx = _Tx(self.mod)
        # The receiver is built on the first frame that actually carries signal.
        # Built eagerly, its 600-half-symbol gain prologue runs on the silence
        # before the far end starts its conditioning signal, and it then spends
        # the rest of the call with a gain measured from nothing.
        self.rx = None
        self.far_taps = far_taps
        self.saw_S = False          # see step(): the receiver opens at TRN
        # 5.4.2: the far end's S, latched from the start of our own conditioning
        # signal. R1TX used to test is_S() against the current frame only, and
        # the caller's S is a few hundred milliseconds long: when it arrived
        # while we were still in RC1 -- which is pure luck of the relative
        # timing -- the level test was false by the time R1TX looked, we never
        # ceased transmission, never listened for R2, and the call died with the
        # far end reporting NO CARRIER. The evidence was in the same log: the
        # receiver opened "at TRN", which requires having seen S and then seen
        # it stop, so S had plainly been observed by the frame latch that R1TX
        # was not using.
        self.far_S = False
        self.r1_sym = None          # symbols transmitted when R1 began
        self.sig_ref = None         # tracked in-band level; see step()
        self.lvl_ref = None         # tracked far-end level; see _far_quiet()
        self.lvl_peak = 0.0         # peak far-end level; see _on_frame()
        self.trn_len = TRN_MIN      # 5.2.3 permits extending this
        # 7.1.2: the V.14 transmit converter. idle=0 gives the 10-bit character
        # 7 calls for; the extra fill bits v22bis uses for margin would put it
        # outside "8, 9, 10 or 11 bits per character".
        self.enc = dte.AsyncEncoder(idle=0, delete_stops=True)
        # V.42, when asked for. `ec` is the live session; `want_ec` survives the
        # fallback so a retrain does not try the detection phase a second time
        # against an end that has already declined it.
        self.ec = None
        self.want_ec = False
        self.ec_fell_back = False
        # The echo canceller, when asked for. Off by default: it is a no-op on a
        # line without echo, but "no-op" should still be an explicit choice.
        self.echo = None
        # Data handed over before the session exists. Without this it lands in
        # the V.14 encoder, which the V.42 path never drains, and the link comes
        # up perfectly and carries nothing.
        self.ecq = bytearray()
        self.level = level_dbfs
        self.events = [] if log is None else log
        self.t = 0.0
        self.state = None
        self.rate = None
        self.can_trellis = False    # set from the constructor by the subclasses
        self.can_bis = False
        self.bis = False            # what the far end's B4/B8 said
        self.trellis = False        # what was actually negotiated
        self.c107 = False
        self.c109 = False
        self.c106 = False           # ready for sending
        self.clamp104 = True        # 5.5: received data clamped to binary one
        self.retrains = 0
        self.retrain_run = 0        # symbols of the far end's retrain tone
        self.loss_run = 0           # symbols with no decision-directed lock
        self.data_sym = None        # tx.nsym when the data phase began
        self.seg_base = 0.0         # tx.nsym at the start of the current segment
        self.listening = False

    def _ev(self, msg):
        self.events.append((round(self.t, 3), self.state, msg))

    def _goto(self, s, msg=""):
        self._ev("-> %s %s" % (s, msg))
        self.state = s

    # -- signal helpers --------------------------------------------------

    def _send_states(self, names, on_done=None):
        self.tx.set("states", states=names, on_done=on_done)

    def _send_bits(self, bits, bps, on_done=None, trellis=False, ts=None):
        self.tx.set("bits", bits=bits, bps=bps, on_done=on_done,
                    trellis=trellis, ts=ts)

    def _send_e(self, bits, on_done=None):
        """5.3.2 / 5.4: finish the rate sequence in flight, *then* send E.

        Replacing the queue outright cuts the current 16-bit sequence short, and
        everything after it lands off by however many bits were left. Measured:
        E arrived two bits shifted and decoded as rates [2400, 4800] instead of
        [9600] -- one dibit out.
        """
        rem = len(self.tx.bits) % 16
        head = self.tx.bits[:rem]
        self.tx.set("bits", bits=head + list(bits), bps=2, on_done=on_done)

    def _conditioning(self, on_done):
        """5.2: S for 256T, S-bar for 16T, then TRN for at least 1280T.

        The scrambler restarts at all zeros for each TRN (5.2.3), and the
        differential encoder is initialised from TRN's final symbol (5.3) so the
        rate signal that follows carries on from it.
        """
        self.mod.scr.reg = 0
        trn = v32.trn_states(self.trn_len, self.mod.scr.taps)
        seq = v32.s_states(S_LEN) + v32.sbar_states(SBAR_LEN) + trn
        last = trn[-1]
        yy = {"A": (0, 0), "B": (0, 1), "C": (1, 1), "D": (1, 0)}[last]

        def done(m):
            m.mod.y = yy               # 5.3: initialise from TRN's last symbol
            on_done(m)
        self._send_states(seq, on_done=done)

    QUIET_FRAC = 0.1

    def _far_quiet(self, e, present):
        """Has the far end stopped transmitting? Relative, not absolute.

        5.4 turns on this question three times -- the answerer's amplitude-drop
        test in 5.4.2, and the arming of both sides' reversal detectors -- and it
        used to be asked as `e[0] < 1.0`, meaning near-perfect silence. That is
        true only of a loopback, where a quiet transmitter emits exact zeros.
        Against a real modem through the FRITZ!Box, the caller's cessation
        measured a mean square of **5926** where its tone had been 3 700 000: a
        drop of 28 dB that the absolute test called "still transmitting". The
        answerer therefore never left AC2, never sent the conditioning signal,
        and the modem gave up and retried six seconds later.

        Measured on that capture, a frame during the steady tone stays within
        0.990 to 1.007 of the tracked reference, and the cessation sits at
        0.0016. A tenth is 10x clear of the worst legitimate frame and 60x above
        the cessation. The frame in which the tone stops part way reads 0.806 and
        is deliberately *not* caught -- the next one is, one frame later, which
        5.4.2 has ample tolerance for.

        This is a different question from step()'s freeze gate and deliberately
        has a different threshold. The freeze gate asks "might this frame be
        untrustworthy to adapt on?" and errs towards yes at half the reference.
        This asks "has the far end definitely stopped?" and must not fire early.

        `present` is the caller's own spectral test for the signal it expects --
        is_tone1800 for the answerer watching the caller's carrier, is_pair for
        the caller watching the answerer's AC. The reference is tracked *only*
        while that signal is present, and this is not a refinement: a plain
        running level tracker latches onto the wrong thing. The V.25 answer tone
        is a full-amplitude sine, while V.32's AC is the four-point constellation
        at mean power 2 scaled by 1/sqrt(10) -- 12.3 dB lower for the same
        level_dbfs label, measured 4 272 571 against 248 779. A tracker that
        spans the ANS-to-AC step reads that legitimate step as a cessation, and
        because it then stops updating it never recovers. Anchoring it to the
        expected signal keeps the two apart: on the answer side the reference is
        the caller's 1800 Hz tone and nothing else.
        """
        ms = e[0]
        if present:
            self.lvl_ref = (ms if self.lvl_ref is None
                            else self.lvl_ref + 0.25 * (ms - self.lvl_ref))
            return False
        ref = self.lvl_ref
        return ref is not None and ms < self.QUIET_FRAC * ref

    def _retrain_trigger(self, e, tone):
        """5.5: has the far end asked for a retrain, or have we lost the signal?

        Returns a reason string, or None. `tone` is the state-appropriate tone
        test -- 1800 Hz for the answerer (5.5.2), the 600/3000 pair for the
        caller (5.5.1). Both are counted in symbol intervals because that is how
        5.5 words the threshold.
        """
        if self.state != DATA or self.retrains >= RETRAIN_MAX:
            return None
        if tone:
            self.retrain_run += SYM_PER_FRAME
            if self.retrain_run > RETRAIN_TONE:
                return "the far end is holding a carrier state"
        else:
            self.retrain_run = 0
        # ... and our own "means of detecting unsatisfactory signal reception",
        # which 5.5 leaves to the implementation. Not judged until the equaliser
        # has had time to settle on the data constellation.
        if self.rx is not None and self.data_sym is not None \
                and self.tx.nsym - self.data_sym > RETRAIN_SETTLE:
            if self.rx.rx.dd:
                self.loss_run = 0
            else:
                self.loss_run += SYM_PER_FRAME
                if self.loss_run > RETRAIN_LOSS:
                    return "our own receiver has lost lock"
        return None

    def _retrain_begin(self, reason):
        """The part 5.5.1 and 5.5.2 share, before they diverge."""
        self.retrains += 1
        self._ev("5.5 retrain %d: %s - 106 off, 104 clamped, 107 stays on"
                 % (self.retrains, reason))
        self.c106 = False
        self.clamp104 = True
        # 5.5.3: 109 is maintained ON through a retrain. The optional OFF after
        # 45 s of the first segment is not implemented; the segments here are
        # bounded well inside that.
        self.retrain_run = 0
        self.loss_run = 0
        self.data_sym = None
        self.rate = None
        self.trellis = False
        self.saw_far_S = False
        self.mt = None
        self.nt = None
        self.t0_sym = None
        self.quiet_run = 0
        self.tone_run = 0
        self.r1 = None
        self.r2 = None
        self.r3 = None
        self.r3_sym = None
        self.e_seen = False
        self.e_wait = 0
        self.seg_base = self.tx.nsym
        if self.rx is not None:
            self.rx.to_handshake()

    def rx_delay_T(self):
        """Symbol intervals of latency the receive path adds before the state
        machine sees a signal. Only the echo canceller contributes, and only when
        it is enabled; see the note on TURNROUND for why it has to be subtracted
        from a turnaround the spec measures at the line."""
        if self.echo is None:
            return 0
        return int(round(self.echo.hold * SYM_PER_FRAME / float(FRAME)))

    def _rate_bits(self, rates=(), end=False, trellis=False):
        """A rate sequence in whichever table applies.

        Note 1 to Table 6/V.32 makes the choice: B4 and B8 both set means V.32bis,
        so a modem with the extra rates simply emits Table 5's sequence and one
        without emits Table 6's. Nothing negotiates the *table* -- the two bits
        are the negotiation.
        """
        if self.can_bis:
            return v32.bis_rate_sequence(rates, end=end)
        return v32.rate_sequence(can2400=False, can4800=4800 in rates,
                                 can9600=9600 in rates, trellis=trellis,
                                 end=end)

    def _offer(self):
        """The rates we can offer, as a set."""
        return set(self.rates)

    def _offer_trellis(self, rates):
        """B8: "availability of trellis coding/decoding at the highest data rate
        indicated in B4-6". We only have it at 9600, so it is only offered when
        9600 is the top rate on offer."""
        return bool(self.can_trellis and rates and max(rates) == 9600)

    def _data_mode(self):
        """The constellation the negotiated rate and coding call for."""
        if self.rate == 4800:
            return v32.QPSK4800
        if self.trellis:
            return v32.TRELLIS_MODES[self.rate]
        return v32.QAM9600

    def _ts(self):
        """The trellis set in use, or None."""
        if self.trellis and self.rate in v32.TRELLIS_SETS:
            return v32.TRELLIS_SETS[self.rate]
        return None

    def _data_bps(self):
        """Data bits per symbol: 2 at 4800, then one more for each rate step.
        V.32bis's rates are trellis-coded by definition, so the count comes from
        the set rather than from the rate."""
        if self.rate == 4800:
            return 2
        ts = self._ts()
        return (ts.nbits - 1) if ts is not None else 4

    def _start_data_tx(self):
        """5.4.1/5.4.2: zero the encoder's delay elements where the trellis-coded
        transmission begins."""
        if self.trellis:
            # 5.3 initialises the differential encoder from TRN's final symbol
            # and nothing resets it again, so it carries into the data phase;
            # only Figure 2's delay elements are zeroed.
            ts = self._ts()
            self.mod.tre = v32.TrellisEncoder(ts)
            self.mod.tre.prev = self.mod.y
            self.mod.reset_trellis(keep_diff=True, ts=ts)

    IS_CALLER = None            # set by the subclasses; also the V.42 role

    def _start_ec(self):
        """7.2.1.1: the V.42 originator is "the role assumed during carrier
        handshake as assigned in the particular modulation Recommendations", so
        the V.32 caller is the V.42 originator and there is nothing to agree."""
        if not self.want_ec or self.ec_fell_back:
            return
        if self.ec is not None:
            # A 5.5 retrain is a physical-layer event. The far end keeps its
            # V(S), V(A) and V(R) across one, so throwing ours away and running
            # a fresh detection phase desynchronises the link: measured, a
            # retrain mid-call left us at V(R) = 2 after 181 I frames, with the
            # window jammed and 56 retransmissions. The receiver object is
            # rebuilt, so only the bit tap has to be re-attached.
            if self.rx is not None:
                self.rx.ec_bits = []
            self._ev("V.42 link kept across the retrain")
            return
        self.ec = v42.Session(originator=bool(self.IS_CALLER))
        self.ec.link.lapm.xid_reps = getattr(self, "xid_reps", 1)
        self.ec.link.lapm.xid_variants = getattr(self, "xid_variants", None)
        self.ec.link.lapm.xid_opt_pi = getattr(self, "xid_opt_pi", True)
        if self.ecq:
            self.ec.put(bytes(self.ecq))
            del self.ecq[:]
        if self.rx is not None:
            self.rx.ec_bits = []
        self._ev("V.42 detection phase begins (%s)"
                 % ("originator" if self.IS_CALLER else "answerer"))

    def _ec_fallback(self):
        """7.2.1.3: no answer to the detection phase means no error correction.
        The data that was queued has not been sent, so it moves to the V.14
        converter rather than being dropped."""
        left = bytes(self.ec.outq) + bytes(self.ecq) if self.ec is not None else bytes(self.ecq)
        del self.ecq[:]
        self.ec = None
        self.ec_fell_back = True
        if self.rx is not None:
            self.rx.ec_bits = None
        if left:
            self.enc.put(bytes(left))
        self._ev("V.42 detection found no far end - falling back to V.14")

    def _send_data(self, nsym, on_done=None):
        bps = self._data_bps()
        self._send_bits([1] * (bps * nsym), bps, on_done=on_done,
                        trellis=self.trellis, ts=self._ts())

    # 5.4: "continuous scrambled binary ones". Queueing a fixed block and
    # letting it run out is not continuous -- the transmitter falls silent and
    # the far receiver spends the rest of the call decoding nothing, which is
    # exactly what it looked like the first time this was measured. So the data
    # phase re-arms itself.
    # One frame's worth, so a character handed to put() waits at most 20 ms.
    DATA_CHUNK = SYM_PER_FRAME

    def _data_run(self):
        """7.1.2: take start-stop characters from the DTE, convert per V.14, and
        hand the resulting synchronous stream to the modulator. With nothing to
        send the converter returns mark, which is exactly the "continuous
        scrambled binary ones" 5.4 calls for, so an idle link is unchanged."""
        bps = self._data_bps()
        n = bps * self.DATA_CHUNK
        if self.ec is not None:
            inb = self.rx.ec_bits if self.rx is not None else []
            got, inb[:] = list(inb), []
            bits = self.ec.step(got, n, self.t)
            if self.ec.fell_back:
                self._ec_fallback()
                bits = self.enc.take(n)
        else:
            bits = self.enc.take(n)
        self._send_bits(bits, bps, on_done=lambda m: m._data_run(),
                        trellis=self.trellis, ts=self._ts())

    def put(self, data):
        """Queue DTE characters for the data phase."""
        if self.ec is not None:
            self.ec.put(data)
        elif self.want_ec and not self.ec_fell_back:
            self.ecq.extend(data)
        else:
            self.enc.put(data)

    def received(self):
        """Characters recovered from the far end, and taken out of the buffer."""
        if self.ec is not None:
            return self.ec.received()
        if self.rx is None:
            return b""
        out = bytes(self.rx.chars)
        del self.rx.chars[:]
        return out

    def _rescan(self):
        """Start a fresh rate-signal hunt.

        Alignment is sticky within a rate signal, which is what stops the scanner
        sliding into signal E and misreading it. But it must not survive the
        silence and TRN between one rate signal and the next: held across that
        gap, the phase established on R1 was stale by the time R3 arrived and R3
        was never seen at all.
        """
        if self.rx is not None:
            self.rx.scan = RateScanner()

    def step(self, inbound):
        self.t += FRAME / float(SR)
        if self.echo is not None:
            # Search only in the data phase. The bulk-delay scan costs about
            # 1.4 ms of every 20 ms frame, and during start-up it cannot possibly
            # earn that back: 640 lags at one lag per frame is 12.8 s, longer
            # than the whole handshake, so it never reaches a verdict before the
            # phase it is running in has ended. What it does reach is the timing.
            # Measured on the Cirrus dial-in, 9 of 9 calls made the data phase
            # with no canceller and 4 of 4 with the canceller present but not
            # scanning, against 1 of 9 with it scanning -- and the samples were
            # provably identical in all three, because the filter never locked.
            #
            # Correlating start-up would be a poor bargain even if it were free.
            # A delay estimate wants one long stationary stretch; 5.4 is a dozen
            # short segments with different spectra. The data signal is scrambled
            # and stationary, which is what the estimator was designed for.
            if self.state == DATA:
                self.echo.budget = self.echo_budget
            elif self.echo.budget:
                self.echo.budget = 0
                self.echo.defer_search()
            # Cancel before anything looks at the samples: the echo pollutes the
            # handshake's tone detectors as much as it pollutes the eye. Nothing
            # is held back -- the echo cannot return in less than one frame, so
            # the newest transmit sample the filter ever needs is the one just
            # pushed, and the canceller adds no delay of its own.
            #
            # Pad to a whole frame first. The canceller lines its two streams up
            # by sample index, and the 1:1 RTP pacing normally keeps them level
            # -- but a frame with no inbound audio, which happens at start-up
            # and on a dropout, would advance transmit without advancing receive
            # and put a permanent offset between them. That is silent: the
            # search simply stops finding the echo. Padding with silence keeps
            # the two clocks locked, and silence is what was actually received.
            #
            # The padding is for the canceller alone, though, and must not reach
            # the receiver. rtp.pump primes two frames before any inbound
            # exists, so padding those and passing them on fed the receiver 320
            # samples of silence that the canceller-off path never sees, and put
            # every received symbol index 96T out of step with our own transmit
            # clock -- across 5.4's turn-round, which is 64T. So slice the
            # invented samples back off before anyone looks at them.
            # At least one whole frame, and a whole number of them: an empty
            # inbound frame still has to advance the canceller's receive clock,
            # or transmit runs ahead of it for the rest of the call.
            n = len(inbound)
            pad = FRAME * max(1, (n + FRAME - 1) // FRAME) - n
            if pad:
                inbound = list(inbound) + [0.0] * pad
            inbound = self.echo.feed(inbound)
            if pad:
                inbound = inbound[:len(inbound) - pad]
        self._on_frame(inbound)
        # The receiver is fed continuously, but it only *adapts* on TRN and on
        # what follows it. 5.2.3 says outright that "segment 3 is intended for
        # training the adaptive equalizer in the receiving modem", and the
        # signals before it are actively hostile to a blind one: AC is an
        # antipodal pair, so it has constant modulus and tells CMA nothing --
        # any rotation satisfies the cost function, including ones that never
        # open a four-point eye -- and S and S-bar are two-point too. Adapting on
        # AC, the caller's receiver never reached decision-directed mode at all:
        # assess() reported medians of 0.39 to 0.77 against a 0.30 threshold.
        #
        # Freezing rather than withholding the input matters just as much. Gating
        # the feed splices the sample stream, and the equaliser sees a
        # discontinuity: with that, the caller handed over correctly at 1.06 s,
        # lost lock, and could never re-acquire -- its medians sat at 0.75 to
        # 0.84 for the rest of the call and R3 took 11.6 s to be admitted.
        #
        # 5.2.2 offers the S-to-S-bar step as "a well-defined event ... that may
        # be used for generating a time reference"; S and S-bar share a magnitude
        # spectrum, so what is detectable is the end of them. When S has been
        # seen and then stops while the signal continues, TRN has begun.
        if self.listening and inbound:
            e = energies(inbound)
            # The level test comes first, and the S latch is gated on it.
            #
            # is_S() normalises by mean square, so it says nothing about how loud
            # the signal is -- and during RC1 the far end is silent while we
            # transmit S ourselves. What comes back is our own echo, 19 dB down
            # on this rig, and it has exactly the spectrum of S because it *is*
            # S. Ungated, the latch fired on our own reflection: the log claimed
            # "incoming S (seen earlier, during RC1)" on a call where the caller
            # had not sent anything at all.
            #
            # The far end's own signals arrive within 10 dB of lvl_peak; an echo
            # at 19 dB down is 0.016 of it, well under QUIET_FRAC. So the same
            # test that already guards opening the receiver guards the latch.
            live = e[0] > self.QUIET_FRAC * self.lvl_peak
            if is_S(e) and live:
                self.saw_S = True
                self.far_S = True
            if self.rx is None and self.saw_S and not is_S(e) and live:
                self.rx = _Rx(self.far_taps)
                self._ev("receiver opened at TRN")
            if self.rx is not None:
                # ... and it must not adapt while the far end is *silent*. The
                # far end goes quiet several times in 5.4 -- the answerer stops
                # transmitting on entering its R2 hunt, for one -- and an
                # absolute floor cannot catch the transition. The frame in which
                # the signal stops is part signal and part nothing: measured, it
                # carried 2250 against the 255 471 of the frame before it, which
                # is 20 dB down and still 22x over a floor of 100. One frame of
                # decision-directed adaptation on that was enough to drive the
                # taps from |w| = 0.99 to 3.37, and CMA never climbed back out --
                # medians of 0.78 to 0.87 against a 0.30 threshold for the rest
                # of the call. That single frame is what made R3 arrive at
                # 11.3 s instead of 3.0 s and what put bit errors in it.
                #
                # So the gate is *relative*: freeze when the frame falls well
                # below the level we have been tracking. Measured over a whole
                # handshake, the smallest ratio on a legitimately live frame is
                # 0.757 and the collapse frame is 0.009, so 0.5 sits with a 1.5x
                # margin below the one and 55x above the other. Only drops
                # count -- the 7 dB rise into the data phase shows up as a ratio
                # of 4.2 and must be allowed through.
                ref = self.sig_ref
                quiet = not live or (ref is not None and e[0] < 0.5 * ref)
                self.rx.rx.frozen = is_S(e) or is_pair(e) or quiet
                if not self.rx.rx.frozen:
                    self.sig_ref = (e[0] if ref is None
                                    else ref + 0.25 * (e[0] - ref))
                for p in self.rx.feed(inbound):
                    self._on_rate(p)
        self.tx.fill(FRAME, self)
        out = self.tx.take(FRAME)
        if self.echo is not None:
            # The reference has to be what we actually emitted. These are the
            # pre-G.711 samples, so quantisation puts a floor near 38 dB on how
            # much echo can be removed -- far below anything we need.
            self.echo.push_tx(out)
        return out


class AnswerStartup(_Base):
    """Answer mode, §5.4.2."""

    IS_CALLER = False

    def __init__(self, level_dbfs=-24.0, ans_s=1.0, rates=(4800, 9600),
                 log=None, trellis=False, trn=TRN_MIN, bis=False, ec=False, cancel_echo=False,
                 echo_budget=echomod.SEARCH_BUDGET):
        _Base.__init__(self, level_dbfs, v32.Scrambler.GPA,
                       v32.Scrambler.GPC, log)
        self.can_trellis = bool(trellis)
        self.can_bis = bool(bis)
        self.want_ec = bool(ec)
        if cancel_echo:
            self.echo = echomod.EchoCanceller(budget=echo_budget)
            self.echo_budget = echo_budget
            self.echo.budget = 0        # 5.4 first; see the gate in step()
        if bis:
            # every V.32bis rate is trellis coded, so offering
            # them is offering the coding
            self.can_trellis = True
        self.trn_len = int(trn)
        # 6.2: "It is recommended that R3 take also account of the likely
        # performance of the answer modem receiver with the particular GSTN
        # connection established." _start_r3 picked max(offered & ours) and
        # looked at nothing, so with every rate enabled it chose the top one and,
        # if that rate would not hold, chose it again after every retrain --
        # measured, 14400 selected five times in one call, five collapses, no
        # data. A real modem falls back. This counts collapses and steps the
        # choice down one offered rate each time, which is 6.2's recommendation
        # made from evidence rather than from a prediction: the link has already
        # told us the rate is too high.
        self.rate_demotions = 0
        import ansam
        self.ans = list(ansam.ans_samples(ans_s, level_dbfs=level_dbfs))
        self.ans_pos = 0
        self.rates = tuple(rates)
        self.state = ANS
        self.rev = Reversal(1800.0)
        self.tone_run = 0
        self.mt = None
        self.t0_sym = None
        self.ac_sent = 0.0
        self.quiet_run = 0
        self.r2 = None
        self.r3_sym = None
        self.saw_far_S = False
        self.wait_until = None

    def _on_rate(self, p):
        if self.state == HUNT2 and not p["end"]:
            self.r2 = p
            self.bis = self.can_bis and bool(p.get("bis"))
            self._ev("R2 received: rates %s%s"
                     % (p["rates"], " (V.32bis)" if self.bis else
                        (" trellis %s" % p.get("trellis"))))
            self.c107 = True
            self._goto(RC2, "(107 on; second conditioning signal)")
            self._conditioning(lambda m: m._start_r3())
        elif self.state == R3TX and p["end"]:
            self._ev("incoming E: %s bit/s%s"
                     % (p["rates"][0] if p["rates"] else 0,
                        (" (V.32bis)" if p.get("bis") else
                      (" trellis" if p.get("trellis") else ""))))
            self.rate = p["rates"][0] if p["rates"] else None
            # 5.4.2: take the coding from the incoming E, which is the caller
            # confirming what R3 asked for -- and never more than we have.
            if p.get("bis"):
                self.bis = self.can_bis
                self.trellis = self.bis and self.rate != 4800
            else:
                self.trellis = bool(p.get("trellis")) and self.can_trellis
            self._goto(ETX, "(complete the sequence, then one E)")
            self._send_e(self._rate_bits(rates=(self.rate,) if self.rate else (),
                                         trellis=self.trellis, end=True),
                         on_done=lambda m: m._start_b1())

    def _start_r3(self):
        # 5.4.2: R3 selects within what R2 offered
        offered = set(self.r2["rates"]) & set(self.rates)
        ranked = sorted(offered, reverse=True)
        pick = (ranked[min(self.rate_demotions, len(ranked) - 1)]
                if ranked else None)
        self.r3_rate = pick
        if self.state != R3TX:
            self._ev("R3: selecting %s bit/s from %s%s"
                     % (pick, sorted(self.r2["rates"]),
                        "" if not self.rate_demotions else
                        " (down %d after %d collapse%s)"
                        % (self.rate_demotions, self.rate_demotions,
                           "" if self.rate_demotions == 1 else "s")))
            # No rescan here, unlike the caller's _start_r2. 5.4.1's timing
            # diagram has the caller sending one S S-bar TRN R2, and then E and
            # B1 straight on the end of it -- no silence, no retrain, no gap. The
            # alignment established while detecting R2 is therefore exactly the
            # alignment E arrives on, and wiping it started a race we sometimes
            # lost: E is a *single* 16-bit sequence, accepted only when aligned,
            # so re-aligning first needs two more R2 sequences before the caller
            # stops sending them. When the caller won that race the E was never
            # seen and the call sat in R3TX until the far end gave up. The
            # caller's own rescan stays, because there the answerer really does
            # retrain between R1 and R3.
            self._goto(R3TX, "(rate signal R3)")
            self.r3_sym = self.tx.nsym
        # 5.4.2: R3 calls for the coding too. Trellis needs both ends to have
        # it, so R2's B8 has to agree with our own capability.
        # V.32bis: every rate above 4800 is trellis coded, so the coding follows
        # from the rate. V.32: only 9600 has an alternative, chosen by B8.
        if self.bis:
            self.r3_trellis = pick != 4800
        else:
            self.r3_trellis = (pick == 9600 and self.can_trellis
                               and bool(self.r2.get("trellis")))
        seq = self._rate_bits(rates=(pick,) if pick else (),
                              trellis=self.r3_trellis)
        self._send_bits(seq * 40, 2, on_done=lambda m: m._start_r3())

    def _start_b1(self):
        self._goto(B1TX, "(scrambled ones at %s bit/s%s for 128T)"
                   % (self.rate, ", trellis coded" if self.trellis else ""))
        if self.rx is not None:
            self.rx.to_data(self._data_mode(), self.trellis, self._ts())
        self._start_data_tx()
        self._send_data(B1_LEN, on_done=lambda m: m._to_data())

    def _to_data(self):
        self.c109 = True
        self.c106 = True
        self.clamp104 = False
        self.data_sym = self.tx.nsym
        self._ev("128T of scrambled ones sent - 106 enabled, 109 on")
        self._goto(DATA)
        self._start_ec()
        self._data_run()

    def _on_frame(self, x):
        e = energies(x) if x else (0.0, 0.0, 0.0, 0.0)
        if x:
            # A peak hold on the far end's level, with a slow release.
            # Absolute thresholds do not survive a real line: idle here
            # measures a mean square of 5926, so a floor of 100 called
            # silence "signal", opened the receiver on it, and let the
            # input gain be measured from nothing.
            self.lvl_peak = max(e[0], self.lvl_peak * 0.999)
        tone = is_tone1800(e)
        quiet = self._far_quiet(e, tone) if x else True
        if x:
            # Arming is sticky. It exists to stop the tone's *onset* from
            # reading as a reversal, so it must latch on once the tone is
            # established: a reversal's own null disturbs the tone test, and
            # gating frame by frame threw away exactly the frame that mattered
            # -- the second turn-round null was missed every time.
            if tone:
                self.rev.armed = True
            elif quiet:
                self.rev.armed = False
            self.rev.feed(x)
        st = self.state
        if st == ANS:
            take = self.ans[self.ans_pos:self.ans_pos + FRAME]
            self.ans_pos += len(take)
            self.tx.outq.extend(take)
            if self.ans_pos >= len(self.ans):
                self._ev("V.25 answer sequence done")
                self._goto(AC1, "(alternate A and C)")
                self._send_states(["AC"[i % 2] for i in range(60000)])
        elif st == AC1:
            # symbols of AC sent *in this segment* -- on a 5.5 retrain the
            # absolute counter is already in the tens of thousands and 5.4.2's
            # "not less than 128" would be satisfied before a single symbol
            self.ac_sent = self.tx.nsym - self.seg_base
            if is_tone1800(e):
                self.tone_run += SYM_PER_FRAME
                if self.tone_run >= TONE_HOLD and self.ac_sent >= 128:
                    # 5.4.2 wants AC for an *even* number of symbol intervals,
                    # and that is not decoration: switch on an odd boundary and
                    # AC..CA concatenates seamlessly, leaving the far end no
                    # reversal to find. Measured, 90/90 found on even boundaries
                    # and nothing to find on odd ones.
                    # The parity that matters is the *segment's*, not the
                    # absolute counter's. On a 5.5 retrain the two differ, and
                    # padding on the wrong one made the AC segment odd: AC..CA
                    # then concatenates seamlessly and the caller has no reversal
                    # to find. It sat in AA forever.
                    n = int(self.tx.nsym - self.seg_base)
                    pad = [] if n % 2 == 0 else ["AC"[n % 2]]
                    self._ev("1800 Hz held for %dT after %dT of AC%s - timer on, "
                             "switching to CA"
                             % (self.tone_run, self.ac_sent,
                                " (+1 for even parity)" if pad else ""))
                    self.t0_sym = self.tx.nsym
                    self._goto(CA, "(alternate C and A)")
                    self._send_states(pad + ["CA"[i % 2] for i in range(60000)])
            else:
                self.tone_run = 0
        elif st == CA:
            waited = self.tx.nsym - (self.t0_sym or self.tx.nsym)
            late = not self.rev.at and waited >= CA_MAX
            if self.rev.at or late:
                r = self.rev.at[-1] if self.rev.at else -1
                raw = self.tx.nsym - self.t0_sym
                self.mt = min(max(raw, MT_MIN), MT_MAX)
                if late:
                    self.mt = MT_DEFAULT
                    self._ev("no phase reversal after %.0fT of CA - the caller "
                             "has moved on; taking MT = %dT and continuing"
                             % (waited, self.mt))
                elif self.mt != raw:
                    self._ev("phase reversal at %.0fT - MT measured %.0fT, "
                             "outside %d..%dT for a round trip, clamped to %.0fT"
                             % (r, raw, MT_MIN, MT_MAX, self.mt))
                else:
                    self._ev("phase reversal at %.0fT - timer stopped, "
                             "MT = %.0fT; state A then back to AC in %dT"
                             % (r, self.mt, int(TURNROUND) - self.rx_delay_T()))
                # 5.4.2 asks for CA "for an even number of symbol intervals"
                # and then, "after transmitting a state A, revert to alternate A
                # and C". An even CA segment already ends on A, and AC begins on
                # A, so the two together give exactly one doubled A -- which is
                # the discontinuity the far end detects. Inserting a further
                # explicit A gives three in a row and no clean reversal.
                pad = int(TURNROUND) - self.rx_delay_T()
                if pad < 2:
                    pad = 2
                if pad % 2:
                    pad += 1
                self._send_states(["CA"[i % 2] for i in range(pad)]
                                  + ["AC"[i % 2] for i in range(8192)])
                self._goto(AC2, "(reverted to AC; waiting for the amplitude drop)")
                self.rev.at = []
        elif st == AC2:
            if quiet:
                self.quiet_run += 1
                if self.quiet_run >= 2:
                    self._ev("amplitude drop - silence for 16T, then the "
                             "receiver conditioning signal")
                    self._goto(QUIET)
                    self.tx.set("quiet", count=QUIET16,
                                on_done=lambda m: m._start_rc1())
            else:
                self.quiet_run = 0
        elif st == R1TX:
            # The latch, not the instantaneous test -- but not before the caller
            # has had a fair chance to read R1. Detection needs two identical
            # sequences, so ceasing the moment we arrive here with S already
            # latched could leave the far end with nothing to lock onto, and it
            # is the far end's R2 we are about to go looking for.
            sent = self.tx.nsym - (self.r1_sym if self.r1_sym is not None
                                   else self.tx.nsym)
            # The latch only -- it is set by the frame loop, one frame behind at
            # worst, and unlike a bare is_S() here it carries the level test that
            # keeps our own echo out.
            if self.far_S and sent >= R1_MIN:
                self.saw_far_S = True
                self._ev("incoming S%s - ceasing transmission after %.0fT of "
                         "R1, waiting MT = %.0fT"
                         % ("" if is_S(e) else " (seen earlier)",
                            sent, self.mt or 0))
                self._goto(WAITMT)
                self.tx.set("quiet", count=max(1, int(self.mt or 64)),
                            on_done=lambda m: m._after_mt())
        elif st == R3TX:
            waited = self.tx.nsym - (self.r3_sym or self.tx.nsym)
            if waited >= E_MAX and self.retrains < RETRAIN_MAX:
                # The caller has either not read our R3 or has read it, sent its
                # eight symbols of E, and moved on without us. Either way there
                # is no signal left to wait for, and 5.5.2 is the way back:
                # transmit AC and pick 5.4.2 up at its third paragraph. The
                # caller is in its own data phase by now, so our carrier state
                # is exactly what its 5.5.1 trigger is watching for.
                self._retrain_answer("no E after %.0fT of R3" % waited)
        elif st == DATA:
            why = self._retrain_trigger(e, tone)
            if why:
                self._retrain_answer(why)

    def _retrain_answer(self, why):
        """5.5.2: transmit AC for an even number of symbol intervals not less
        than 128, then pick up 5.4.2 at its third paragraph -- which is what our
        AC1 state already is."""
        # Whatever rate we chose did not survive, so the next R3 may ask for
        # less -- but not on the first retrain. A single one is a transient, and
        # 5.5 exists precisely so a link can recover at the rate it had; the
        # soft-to-soft tests induce exactly that and expect 9600 back. It is the
        # *repeated* collapse that says the rate is wrong, so the demotion
        # starts from the second. A data phase that actually held resets it,
        # because a rate that ran for ten seconds has proved itself and should
        # not be given away to one later glitch.
        held = 0
        if self.data_sym is not None:
            held = self.tx.nsym - self.data_sym
        if held >= RATE_PROVEN_SYM:
            self.rate_demotions = 0
        elif self.retrains >= 1:
            self.rate_demotions += 1
        self._retrain_begin(why)
        self.rev = Reversal(1800.0)
        self.ac_sent = 0.0
        self._goto(AC1, "(5.5.2: alternate A and C, then 5.4.2 from para 3)")
        self._send_states(["AC"[i % 2] for i in range(60000)])

    def _start_rc1(self):
        self._goto(RC1, "(S, S-bar, TRN)")
        self.listening = True
        # Scoped to this conditioning phase, so a retrain starts the hunt afresh.
        self.far_S = False
        self.r1_sym = None
        self._conditioning(lambda m: m._start_r1())

    def _start_r1(self):
        if self.state != R1TX:
            self._goto(R1TX, "(rate signal R1)")
            self.r1_sym = self.tx.nsym
        seq = self._rate_bits(rates=self.rates,
                              trellis=self._offer_trellis(self.rates))
        self._send_bits(seq * 60, 2, on_done=lambda m: m._start_r1())

    def _after_mt(self):
        self._ev("MT elapsed - training the receiver, hunting for R2")
        self._rescan()
        self._goto(HUNT2)


class OriginateStartup(_Base):
    """Call mode, §5.4.1."""

    IS_CALLER = True

    def __init__(self, level_dbfs=-24.0, rates=(4800, 9600), log=None,
                 ans_hold=1.0, trellis=False, trn=TRN_MIN,
                 bis=False, ec=False, cancel_echo=False,
                 echo_budget=echomod.SEARCH_BUDGET):
        _Base.__init__(self, level_dbfs, v32.Scrambler.GPC,
                       v32.Scrambler.GPA, log)
        self.can_trellis = bool(trellis)
        self.can_bis = bool(bis)
        self.want_ec = bool(ec)
        if cancel_echo:
            self.echo = echomod.EchoCanceller(budget=echo_budget)
            self.echo_budget = echo_budget
            self.echo.budget = 0        # 5.4 first; see the gate in step()
        if bis:
            # every V.32bis rate is trellis coded, so offering
            # them is offering the coding
            self.can_trellis = True
        self.trn_len = int(trn)
        self.rates = tuple(rates)
        self.state = WAITANS
        self.rev600 = Reversal(600.0)
        self.rev3000 = Reversal(3000.0)
        self.ans_hold = ans_hold
        self.ans_run = 0.0
        self.s_hold = 0             # floor on the S period, in symbols
        self.nt = None
        self.t0_sym = None
        self.r1 = None
        self.r3 = None
        self.e_seen = False
        self.e_wait = 0

    def _rev(self):
        """Reversals in whichever of the two tones is present (5.4.1)."""
        a = self.rev600.at
        b = self.rev3000.at
        return a if len(a) >= len(b) else b

    def _on_rate(self, p):
        if self.state == WAITS and not p["end"] and self.r1 is None:
            self.r1 = p
            self.bis = self.can_bis and bool(p.get("bis"))
            self._ev("R1 received: rates %s%s"
                     % (p["rates"], " (V.32bis)" if self.bis else
                        (" trellis %s" % p.get("trellis"))))
            # 6.1 says S "for a period NT already estimated by the
            # counter/timer", and NT is short on this rig -- 384T, 160 ms. 6.2
            # has the answerer detect our S, cease transmitting, wait its own MT
            # and then require that S "persists ... or reappears". If its MT
            # outlasts our S, it goes looking for something that has already
            # stopped. s_hold sets a floor on the period so that can be tested
            # rather than argued about; 0 keeps the Recommendation's NT.
            n = max(1, int(self.nt or 256), int(self.s_hold))
            self._goto(STX_NT, "(S for %dT; NT = %.0fT)" % (n, self.nt or 0))
            self._send_states(v32.s_states(n),
                              on_done=lambda m: m._start_rc())
        elif self.state == R2TX and not p["end"]:
            self.r3 = p
            self._ev("R3 received: %s bit/s%s"
                     % (p["rates"], " (V.32bis)" if p.get("bis") else
                        (" trellis" if p.get("trellis") else "")))
            self.rate = max(p["rates"]) if p["rates"] else None
            # 5.4.1: adopt "the rate, coding and any special operational modes
            # called for in R3" -- but only if we actually have the coding.
            if p.get("bis"):
                self.bis = self.can_bis
                self.trellis = self.bis and self.rate != 4800
            else:
                self.trellis = bool(p.get("trellis")) and self.can_trellis
            if p.get("trellis") and not self.can_trellis:
                self._ev("R3 called for trellis coding, which we do not have - "
                         "falling back to the nonredundant alternative")
            self._goto(ETX, "(one E, per R3)")
            self._send_e(self._rate_bits(rates=(self.rate,) if self.rate else (),
                                         trellis=self.trellis, end=True),
                         on_done=lambda m: m._start_b1())
        elif self.state == B1TX and p["end"]:
            self.e_seen = True
            self._ev("incoming E: %s bit/s%s - switching the receiver, 109 on "
                     "after 128T"
                     % (p["rates"], (" (V.32bis)" if p.get("bis") else
                      (" trellis" if p.get("trellis") else ""))))
            if self.rx is not None:
                self.rx.to_data(self._data_mode(), self.trellis, self._ts())

    def _retrain_call(self, why):
        """5.5.1: repetitively transmit carrier state A, then pick up 5.4.1 at
        its third paragraph -- "conditioned to detect one of two incoming tones
        at 600 and 3000 Hz, and subsequently a phase reversal in that tone",
        which is what our AA state already is. WAITANS is skipped: 5.5.1 has us
        transmitting A already, so there is no answer tone to wait for."""
        self._retrain_begin(why)
        self.rev600 = Reversal(600.0)
        self.rev3000 = Reversal(3000.0)
        self._goto(AA, "(5.5.1: state A, then 5.4.1 from para 3)")
        self._send_states(["A"] * 60000)

    def _start_rc(self):
        self._goto(RC1, "(S, S-bar, TRN)")
        self._conditioning(lambda m: m._start_r2())

    def _start_r2(self):
        offered = set(self.r1["rates"]) & set(self.rates)   # 5.4.1: R2 <= R1
        if not self.c107:
            self.c107 = True
            self._ev("107 on; R2 offering %s (R1 offered %s)"
                     % (sorted(offered), sorted(self.r1["rates"])))
        if self.state != R2TX:
            self._rescan()              # now hunting for R3
            self._goto(R2TX, "(rate signal R2)")
        seq = self._rate_bits(rates=sorted(offered),
                              trellis=self._offer_trellis(offered))
        self._send_bits(seq * 60, 2, on_done=lambda m: m._start_r2())

    def _start_b1(self):
        # 5.4.1: transmit scrambled ones at the R3 rate, but keep *receiving* in
        # the four-point mode -- the answerer's own E is still to come, and it is
        # sent at 4800 bit/s like every other rate sequence. Switching the
        # receiver here instead of on that E leaves it unable to read it.
        self._goto(B1TX, "(scrambled ones at %s bit/s%s; still receiving at "
                   "4800)" % (self.rate,
                              ", trellis coded" if self.trellis else ""))
        self._start_data_tx()
        self._start_ec()
        self._data_run()

    def _on_frame(self, x):
        e = energies(x) if x else (0.0, 0.0, 0.0, 0.0)
        if x:
            # A peak hold on the far end's level, with a slow release.
            # Absolute thresholds do not survive a real line: idle here
            # measures a mean square of 5926, so a floor of 100 called
            # silence "signal", opened the receiver on it, and let the
            # input gain be measured from nothing.
            self.lvl_peak = max(e[0], self.lvl_peak * 0.999)
        pair = is_pair(e)
        quiet = self._far_quiet(e, pair) if x else True
        if x:
            # Sticky, for the same reason as on the answer side.
            if pair:
                self.rev600.armed = self.rev3000.armed = True
            elif quiet:
                self.rev600.armed = self.rev3000.armed = False
            self.rev600.feed(x)
            self.rev3000.feed(x)
        st = self.state
        if st == WAITANS:
            # 6.1: "After receiving the answer tone for a period of at least 1 s
            # as specified in Recommendation V.25, the modem shall be connected
            # to line ... and shall repetively transmit carrier state A."
            #
            # ans_hold and ans_run were here for that and were never wired to
            # anything, so the only way out of WAITANS was Note 1's alternative
            # -- proceed on the 600/3000 pair without waiting for 2100 Hz. That
            # is legal and it is not what a real caller does. Measured against
            # the modem in the calling role, it holds its 1800 Hz for 1.5 to 2 s
            # before anything else happens; ours managed 0.35 s before the
            # reversal and 0.26 s after it, because it could not start until the
            # answerer's AC had already begun. 6.2 wants our tone present for 64
            # symbol periods, on top of 128 symbol intervals of its own AC,
            # before it will arm the reversal detector -- and that reversal is
            # what defines NT and MT for both ends. Starting late hands the
            # answerer a time reference from a schedule it is not on, and
            # measured, it then never ceased transmitting, never read our R2 and
            # never looked for our E.
            if x and e[0] >= 1.0 and dsp.goertzel(x, 2100.0) / e[0] > 0.5:
                self.ans_run += len(x) / float(SR)
            if is_pair(e) or self.ans_run >= self.ans_hold:
                self._ev("%s - transmitting state A"
                         % ("600/3000 Hz pair detected" if is_pair(e)
                            else "answer tone held for %.1f s" % self.ans_run))
                # The far end's level starts here, and not before. Everything
                # earlier is pre-answer: ringback, network tones, whatever the
                # box plays while the extension is still ringing. On this rig
                # ringback arrives at -10.4 dBFS against the modem's -24, so a
                # peak tracker that has seen it sits 26x above every signal that
                # matters, and `live` -- which gates the S latch and therefore
                # opening the receiver at all -- stays shut while lvl_peak decays
                # at 0.999 a frame. That is 65 s, and the far end gives up at 60:
                # the first originated call ever made from this code sat in WAITS
                # for ninety seconds with a perfectly good S, TRN and R1 going
                # past it. The answer side never had the problem because it never
                # hears ringback.
                self.lvl_peak = e[0]
                self.sig_ref = None
                self._goto(AA)
                self._send_states(["A"] * 60000)
                self.rev600.at = []
                self.rev3000.at = []
                self.rev600.prev = None
                self.rev3000.prev = None
        elif st == AA:
            hits = self._rev()
            if hits:
                self.t0_sym = self.tx.nsym
                turn = max(2, int(TURNROUND) - self.rx_delay_T())
                self._ev("first phase reversal at %.0fT - timer on, AA->CC in "
                         "%dT" % (hits[-1], turn))
                self._send_states(["A"] * turn + ["C"] * 60000)
                self._goto(CC)
                self.rev600.at = []
                self.rev3000.at = []
        elif st == DATA:
            why = self._retrain_trigger(e, pair)
            if why:
                self._retrain_call(why)
        elif st == CC:
            hits = self._rev()
            if hits:
                self.nt = self.tx.nsym - self.t0_sym
                self._ev("second phase reversal at %.0fT - timer stopped, "
                         "NT = %.0fT; ceasing transmission" % (hits[-1], self.nt))
                self.tx.set("quiet")
                self._goto(WAITS, "(silent, waiting for an incoming S)")
                self.listening = True
        elif st == B1TX and self.e_seen:
            self.e_wait += SYM_PER_FRAME
            if self.e_wait >= B1_LEN:
                self.c109 = True
                self._ev("128T after the incoming E - 109 on, 106 enabled")
                self._goto(DATA)
                self.c106 = True
                self.clamp104 = False
                self.data_sym = self.tx.nsym
                self._data_run()
