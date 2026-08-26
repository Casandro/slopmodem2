"""Sample-by-sample tracking receiver for V.22bis.

The block receiver in `equalise.py` decodes a window at a time: one brute-forced
timing phase, one linear-fit frequency offset, one CMA tap set and one DD-LMS tap
set, each computed over the whole window and then applied to all of it. That
works, but it has two costs measured on the real captures (see
`testrig/v22-modem.md`): every window pays its own blind-acquisition transient,
and nothing tracks channel drift *within* a window, so widening the window from
3 s to 5 s pushed median distance to the lattice from 0.07 to 0.22-0.34.

This module replaces all four block stages with loops that run once per symbol,
so a stream of any length is decoded in a single pass with one acquisition at the
start:

  timing     interpolating polyphase matched filter at 2 samples/symbol, with a
             Gardner detector driving a PI loop that steers the sample instants
  equaliser  T/2-spaced fractionally-spaced FIR, NLMS update every symbol,
             CMA while acquiring then decision-directed
  carrier    rotator after the equaliser, decision-directed PI loop, which
             absorbs residual frequency offset and phase drift

Why fractionally spaced: a T/2 equaliser can synthesise a fractional delay, so
it absorbs whatever timing error the loop leaves behind, and it does not need
the timing phase to be right before it can converge. That is what lets timing
and equalisation acquire together from a cold start.

The loop lives in `StreamRx`, which takes samples a frame at a time and is what
runs inside the RTP callback; `TrackingRx` is a thin wrapper that feeds a whole
array through it in one call, so the offline tests exercise exactly the live
code. `LiveRx` adds the character chain on top -- differential decode,
descrambler, async framer -- and is the class `run_answer.py` hands RTP frames
to.

Why Gardner before the equaliser: its error term is
Re{conj(mid) * (cur - prev)}, and multiplying every sample by a common e^{j0}
leaves it unchanged -- conj(mid) picks up e^{-j0} and the difference picks up
e^{+j0}. So it is exactly carrier-phase invariant and can run before the carrier
loop has any idea where it is.
"""
import cmath, math, time
import v22, v22bis

SR = v22.SR
# Defaults are V.22's. Everything that depends on the symbol rate now takes it
# as a parameter, because V.32 runs at 2400 baud (3.333 samples per symbol
# against V.22's 13.333) through the same receiver.
SPS = v22.SPS                      # 13.333... samples per symbol
HALF = SPS / 2.0
BAUD = v22.BAUD

LATTICE = [complex(i, q) for i in (-3, -1, 1, 3) for q in (-3, -1, 1, 3)]
LATTICE_POWER = 10.0               # E[|a|^2] for both V.22bis constellations;
                                   # a per-mode figure now (Mode.power), because
                                   # V.32's 4800 bit/s subset has power 2


class Mode:
    """A constellation and everything the receiver derives from it.

    V.22bis runs two: 16-QAM quadbits at 2400 bit/s (2.5.2.1) and, at 1200 bit/s
    (2.5.2.2), dibits carried on "the signalling elements corresponding to 01 in
    the signal constellation ... irrespective of the quadrant concerned" -- the
    four points of magnitude sqrt(10) at 18.43 + k*90 degrees. Both have mean
    power 10, so every scaling path in the receiver is shared; what differs is

      r2      E[|a|^4]/E[|a|^2], the constant CMA drives |y|^2 towards. 13.2 for
              16-QAM, 10 for the four-point set -- which is constant modulus, so
              its dispersion floor is 0 rather than 42.24 and CMA converges to a
              genuinely clean eye instead of a compromise.
      m4ref   arg(E[z^4])/4, a fixed property of the constellation and not an
              ambiguity: +45 degrees for 16-QAM, +18.43 for the four-point set.
              Subtract it or the estimate sits on the decision diagonals.
      bps     4 or 2, which is also how many bits each symbol contributes.
    """

    def __init__(self, name, points, bps):
        self.name = name
        self.points = list(points)
        self.bps = bps
        n = len(self.points)
        self.power = sum(abs(z) ** 2 for z in self.points) / n
        self.r2 = (sum(abs(z) ** 4 for z in self.points) / n) / self.power
        self.m4ref = cmath.phase(sum(z ** 4 for z in self.points) / n) / 4.0
        self.labels = [(z, v22bis.POINT_TO_LABEL[(int(round(z.real)),
                                                  int(round(z.imag)))])
                       for z in self.points]

    def slice(self, z):
        best = self.points[0]
        bd = abs(z - best)
        for p in self.points[1:]:
            d = abs(z - p)
            if d < bd:
                bd, best = d, p
        return best

    def label(self, z):
        best, lab = self.labels[0]
        bd = abs(z - best)
        for p, l in self.labels[1:]:
            d = abs(z - p)
            if d < bd:
                bd, best, lab = d, p, l
        return lab

    def decode(self, syms, prev_q=None):
        """Differential decode. Table 1/V.22bis gives the quadrant change from
        the first two bits; at 2400 the last two name the point within the new
        quadrant, and at 1200 there is only ever the one point."""
        bits = []
        for z in syms:
            q, last = self.label(z)
            if prev_q is not None:
                deg = 0
                for d in (0, 90, 180, 270):
                    if v22bis.rotate_quad(prev_q, d) == q:
                        deg = d
                        break
                bits.extend(v22bis.CHANGE_QUAD[deg])
                if self.bps == 4:
                    bits.extend(last)
            prev_q = q
        return bits, prev_q


QAM2400 = Mode("2400", LATTICE, 4)
QPSK1200 = Mode("1200", [complex(*v22bis.POINTS[(q, (0, 1))])
                         for q in (1, 2, 3, 4)], 2)
MODES = {2400: QAM2400, 1200: QPSK1200, 4: QAM2400, 2: QPSK1200}

R2 = QAM2400.r2                    # kept for callers that predate Mode
M4_REF = QAM2400.m4ref


def slice_to(z):
    return QAM2400.slice(z)


# ---------------------------------------------------------------------------
# Interpolating matched filter
# ---------------------------------------------------------------------------

def polyphase(nsub=128, span=6, sps=SPS, beta=v22.ROLLOFF):
    """SRRC matched filter as a polyphase bank indexed by fractional delay.

    To read the matched-filter output at an arbitrary continuous time tau, split
    tau into m = floor(tau) and f = tau - m. With the filter's half-span D and
    the input window starting at m - D, tap k multiplies input m - D + k and its
    argument is (f + D - k)/SPS symbols -- a function of f alone. So one table of
    `nsub` sub-phases covers every possible sampling instant, and reading the
    filter at any time costs one table lookup plus L multiply-accumulates.

    This is also why the filter is never evaluated on a sample grid: there is no
    separate "resample then filter" step to accumulate error, and no requirement
    that SPS be an integer (it is 13.333).
    """
    n = int(span * sps)
    if n % 2 == 0:
        n += 1
    d = (n - 1) // 2
    tab = []
    for p in range(nsub):
        f = p / float(nsub)
        tab.append([v22.srrc_at((f + d - k) / sps, beta) for k in range(n)])
    e = math.sqrt(sum(v * v for v in tab[0]))
    return [[v / e for v in row] for row in tab], d, n


def carrier_lut(fc):
    """exp(-j*2*pi*fc*n/SR) over its exact period.

    8000/1200 and 8000/2400 are 6.667 and 3.333 samples per cycle, so three
    cycles take exactly 20 and 10 samples. The downconversion is therefore a
    table lookup on n % period with no accumulated phase error at all.
    """
    per = 1
    while per < 10000:
        if abs(per * fc / SR - round(per * fc / SR)) < 1e-12:
            break
        per += 1
    w = 2 * math.pi * fc / SR
    return [cmath.exp(-1j * w * n) for n in range(per)], per


def assess(tail, mode=QAM2400, block=48, step=None, span=12.0,
           baud=BAUD):
    """Is the eye open, and if so at what scale, phase and frequency?

    Returns (median_dist, gain, phase, rad_per_symbol). All three corrections
    come from the same fourth-order moment: |E[z^4]| collects into one lobe only
    when the constellation is not spinning, so taking arg(E[z^4])/4 over
    successive short blocks and fitting a line gives the residual frequency in
    its slope and the current phase in its value at the end of the tail. Doing
    this once at the handover is what lets the decision-directed loop start from
    a standing constellation -- seeding phase alone leaves a 7 Hz offset
    (4.2 deg/symbol) that the loop cannot pull in from a cold integrator, which
    is exactly what was measured.

    The m4ref subtraction matters: without it the estimate sits 45 degrees out,
    i.e. exactly on the decision diagonals.
    """
    n = len(tail)
    if n < 4 * block:
        return 9.9, 1.0, 0.0, 0.0
    p = sum(abs(v) ** 2 for v in tail) / n
    if p <= 0:
        return 9.9, 1.0, 0.0, 0.0
    gain = math.sqrt(mode.power / p)
    z = [v * gain for v in tail]

    # Coarse frequency first, by direct search. The block-fit below unwraps
    # modulo 90 degrees, so it is only valid while the phase advances well under
    # 45 degrees within one block -- at 600 baud a 48-symbol block allows about
    # 1.5 Hz. V.22bis 2.6 permits +/-7 Hz, which advances 201 degrees per block
    # and aliases: measured, the fit reported 0.96 Hz for a true 7 Hz offset and
    # the loop never pulled in.
    #
    # Searching is cheap because de-spinning by f and maximising the fourth-order
    # concentration |sum z^4| is just a periodogram of z^4 evaluated at 4f: the
    # trial exponent is 4*(2*pi*f/BAUD)*k.
    #
    # The grid step defaults to this periodogram's own resolution, BAUD/(4N):
    # 0.25 Hz for a 600-symbol tail. Searching finer is wasted work -- this is
    # the one expensive thing in the receiver and it runs inside a 20 ms RTP
    # callback -- and searching coarser silently misses the peak. Measured, and
    # worth recording: at a 0.5 Hz step, a 200 ppm sample-clock error (which
    # drags the 1200 Hz carrier by 0.246 Hz, i.e. exactly half a grid step)
    # pushed acquisition from symbol 600 out to symbol 4800 and left the eye
    # half shut, while 0.25 and 0.1 Hz both acquired at 600 with zero errors.
    z4 = [v ** 4 for v in z]
    p4 = sum(abs(v) for v in z4) + 1e-12
    coarse, best = 0.0, -1.0
    if step is None:
        step = baud / (4.0 * n)
    f = -span
    while f <= span + 1e-9:
        # running rotor rather than an exp() per sample: same sum, ~50x less
        # work, and 600 recursive multiplies drift far below the resolution
        r = cmath.exp(-1j * 4.0 * 2.0 * math.pi * f / baud)
        rot = 1.0 + 0j
        acc = 0j
        for v in z4:
            acc += v * rot
            rot *= r
        sc = abs(acc) / p4
        if sc > best:
            best, coarse = sc, f
        f += step
    wc = 2.0 * math.pi * coarse / baud
    z = [z[k] * cmath.exp(-1j * wc * k) for k in range(n)]

    ph, ctr = [], []
    for i in range(0, n - block + 1, block):
        b = z[i:i + block]
        s4 = sum(v ** 4 for v in b) / len(b)
        ph.append(cmath.phase(s4) / 4.0 - mode.m4ref)
        ctr.append(i + (len(b) - 1) / 2.0)
    # unwrap modulo 90 degrees, which is all this lattice determines
    unw = [ph[0]]
    for v in ph[1:]:
        k = round((unw[-1] - v) / (math.pi / 2))
        unw.append(v + k * math.pi / 2)
    m = len(unw)
    mx = sum(ctr) / m
    my = sum(unw) / m
    den = sum((c - mx) ** 2 for c in ctr)
    slope = (sum((ctr[i] - mx) * (unw[i] - my) for i in range(m)) / den
             if den else 0.0)
    end = wc * (n - 1) + my + slope * ((n - 1) - mx)
    # de-spin the tail with the fitted line and see whether it lands
    dz = [z[i] * cmath.exp(-1j * (my + slope * (i - mx))) for i in range(n)]
    dl = sorted(abs(mode.slice(v) - v) for v in dz)
    return dl[len(dl) // 2], gain, end, wc + slope


class StreamRx:
    """Single-pass V.22bis receiver, fed a stream: timing, equaliser and carrier
    all tracking.

    This is the class that runs inside the RTP callback. `TrackingRx` below is a
    thin batch wrapper that feeds a whole array through it in one call, so the
    offline tests exercise exactly the code the live path runs -- there is one
    implementation of the loop, not two.

    Gains are deliberately modest. This has to hold lock for a minute without
    supervision, and a loop fast enough to look impressive during acquisition is
    also a loop that walks off on a noise burst.

    There is no fixed schedule. The receiver is in one of two modes and moves
    between them on measurements:

      acq   CMA adaptation, timing loop at `acq_t_gain` times its tracking
            gains, no carrier rotator. Every `acq_check` symbols it asks
            assess() whether the eye is open; when the answer is yes it takes
            the scale, phase and frequency from that same measurement and
            switches to dd.
      dd    decision-directed equaliser and carrier loop. If the running mean
            square decision error passes `lose_thresh`, it drops back to acq.

    So a clean signal reaches dd in `acq_min` symbols, a badly mistimed one takes
    as long as it needs, and a channel that changes under the receiver gets a
    fresh acquisition rather than a stuck one.
    """

    def __init__(self, carrier=v22.LOW, taps=21, acq_min=600, acq_win=600,
                 acq_check=200, acq_thresh=0.30, lose_thresh=0.55,
                 lose_hold=400,
                 # mu_dd measured: at 0.0025 the tap time constant is
                 # ntaps/mu = 8400 symbols (14 s), too slow to follow a channel
                 # that moves in 4 s, and the loop lost lock twice. Raising it
                 # cost nothing measurable at 24/20/17 dB SNR, where the median
                 # lattice distance is set by the additive noise rather than by
                 # gradient noise.
                 mu_cma=0.004, mu_dd=0.02, leak=1e-6,
                 kp_t=0.008, ki_t=4e-5, acq_t_gain=3.0,
                 kp_c=0.010, ki_c=2.5e-4,
                 nsub=128, pro_half=600, settle=1500, out_thresh=0.25,
                 mode=QAM2400, sps=SPS, baud=BAUD, beta=v22.ROLLOFF,
                 span=6, log_every=0):
        self.ntaps = taps
        self.mode = mode
        # Acquisition ends when the eye is measurably open, not after a fixed
        # number of symbols. Pulling in a half-symbol timing offset took ~2500
        # symbols in measurement, so any fixed schedule is either too short for
        # the bad cases or wasteful for the good ones.
        # 5.2.3: "Segment 3 is intended for training the adaptive equaliser in
        # the receiving modem". TRN is scrambled ones through a known polynomial
        # with differential coding off, so the receiver can generate it and adapt
        # against a *known* reference instead of against its own decisions or a
        # blind cost function. train_ref() supplies it.
        self.ref = None
        self.ref_i = 0
        self.ref_used = 0
        self.ref_err = 0.0
        self.acq_min, self.acq_win = acq_min, acq_win
        self.acq_check, self.acq_thresh = acq_check, acq_thresh
        self.lose_thresh, self.lose_hold = lose_thresh, lose_hold
        self.mu_cma, self.mu_dd, self.leak = mu_cma, mu_dd, leak
        self.kp_t, self.ki_t, self.acq_t_gain = kp_t, ki_t, acq_t_gain
        self.kp_c, self.ki_c = kp_c, ki_c
        self.log_every = log_every
        self.settle = settle

        self.sps = sps
        self.sps_half = sps / 2.0
        self.baud = baud
        self.tab, self.D, self.L = polyphase(nsub, span, sps, beta)
        self.nsub = nsub
        self.lut, self.per = carrier_lut(carrier)

        # --- sample buffer, absolutely indexed ---------------------------
        # buf[j] is the baseband sample at absolute stream index abs0 + j.
        # Absolute indexing is what makes the carrier exact across frames: the
        # lookup is lut[i % per] on the absolute index, and since the period is
        # 20 samples for the low channel and 10 for the high, and an RTP frame
        # is 160 samples, every frame boundary happens to land on lut index 0.
        self.buf = []
        self.abs0 = 0
        self.nfed = 0

        # --- prologue ----------------------------------------------------
        self.pro_half = pro_half
        self.g0 = None
        self.clamps = 0               # CMA gain rescues; see _loop()
        self._pend = None              # deferred gain measurement; rescale_to()
        self._pend_n = 0
        self.tau0 = float(self.D + 2 * self.sps)
        self.pro_tau = self.tau0
        self.pro_acc = 0.0
        self.pro_n = 0

        # --- loop state --------------------------------------------------
        self.tau = self.tau0
        self.t_freq = 0.0              # timing loop integrator, samples/half-sym
        self.eq_buf = [0j] * taps      # T/2-spaced equaliser delay line
        self.w = [0j] * taps
        self.w[taps // 2] = 1.0 + 0j
        self.ph = 0.0                  # carrier rotator phase
        self.c_freq = 0.0              # carrier loop integrator, rad/symbol
        self.pwr = mode.power          # running signal power, for the TED
        self.half = []                 # last three half-symbol samples
        self.n = 0                     # half-symbol counter; even = on-symbol
        self.drift = 0.0               # accumulated timing correction, samples
        self.nsym = 0
        self.dd = False
        self.acq_buf = []
        self.lockerr = 0.0             # running mean square decision error
        self.dd_at = 0
        self.retrains = 0
        self.acq_med = None
        self.events = []               # (symbol index, mode entered)
        self.tlog = []
        self.locked_out = []           # symbols produced while settled in dd
        # When frozen, the loops keep running -- so the sample clock, carrier
        # phase and symbol grid stay continuous -- but the taps are not updated
        # and nothing accumulates towards an acquisition decision. That is for
        # stretches of signal that are actively bad for a blind equaliser, such
        # as V.32's two-point S and AC segments. Gating the *input* instead
        # splices the sample stream and the equaliser sees a discontinuity.
        self.frozen = False
        self.fast_err = 0.0            # short-window decision error, for the gate
        self.out_thresh = out_thresh
        self.gated = 0                 # symbols withheld because the gate was shut

    # -- interpolating matched filter -----------------------------------

    def _at(self, tau):
        """Matched-filter output at continuous time tau, or None if the samples
        it needs have not arrived yet.

        The polyphase index depends only on the fractional part of tau: with the
        window starting at floor(tau) - D, tap k's argument is (f + D - k)/SPS,
        a function of f alone. So one table of sub-phases covers every possible
        sampling instant, there is no separate resample-then-filter step to
        accumulate error, and SPS never has to be an integer (it is 13.333).
        """
        m = int(tau)
        f = tau - m
        p = int(f * self.nsub)
        if p >= self.nsub:
            p = self.nsub - 1
        j = m - self.D - self.abs0
        if j < 0 or j + self.L > len(self.buf):
            return None
        row = self.tab[p]
        buf = self.buf
        acc = 0j
        for k in range(self.L):
            acc += buf[j + k] * row[k]
        return acc

    # -- prologue --------------------------------------------------------

    def _prologue(self, final=False):
        """Input gain so the equaliser sees roughly lattice-scaled samples.

        CMA's error term y*(R2 - |y|^2) is cubic in the signal amplitude, so it
        is meaningless until the input is near the scale R2 was derived for.
        Feeding it raw A-law sample magnitudes (order 10^3) makes the very first
        tap update order 10^1 and the equaliser diverges immediately.

        A block receiver normalises the whole window up front. A streaming one
        cannot, so it measures a prologue instead -- 600 half-symbols, about
        0.5 s -- and then rewinds to tau0 and demodulates those same samples.
        Nothing is discarded, it is just delayed, which is why the buffer is not
        trimmed while this is running.
        """
        while self.pro_n < self.pro_half:
            s = self._at(self.pro_tau)
            if s is None:
                if not final:
                    return
                break
            self.pro_acc += abs(s) ** 2
            self.pro_n += 1
            self.pro_tau += self.sps_half
        if self.pro_n == 0 or self.pro_acc <= 0:
            if final:
                self.g0 = 1.0
            return
        self.g0 = math.sqrt(self.mode.power / (self.pro_acc / self.pro_n))

    # -- buffer housekeeping ---------------------------------------------

    def _trim(self, slack=4096):
        keep = int(self.tau) - self.D - 1
        cut = keep - self.abs0
        if cut > slack:
            del self.buf[:cut]
            self.abs0 += cut

    # -- the loop --------------------------------------------------------

    # |y|^2 may reach this multiple of r2 before the gradient step is abandoned
    # for a direct gain correction. 16 is 4x in amplitude -- far outside anything
    # a converging CMA produces, and far inside where the cubic term explodes.
    CMA_CLAMP = 16.0

    def train_ref(self, points):
        """Adapt against a known symbol sequence rather than blindly.

        Reference-directed LMS is what 5.2.3 budgets 1280 to 8192 symbols of TRN
        for, and it converges in a fraction of what a blind algorithm needs --
        the cost of blindness being, in the literature's words, a large
        mean-square error on high-order QAM that damages the switch to
        decision-directed operation. Which is this rig's 14400 exactly: CMA
        settles near 9% and the handover never comes.

        Entering it also asserts dd, so that when the reference runs out the
        loop continues on its own decisions rather than falling back to blind.
        """
        self.ref = list(points)
        self.ref_i = 0
        self.ref_used = 0
        self.ref_err = 0.0
        self.dd = True

    def rescale_to(self, mode, nsym=None):
        """Switch constellation and re-measure the output gain, once.

        A constellation change moves the target power, and how the *input* level
        moves at the same moment is a property of the far end, not something to
        assume. Our own transmitter scales every V.32 constellation by the same
        1/sqrt(10), so its wire level rises 7 dB from the four-point handshake to
        the data phase and the taps need no correction. A real modem does the
        opposite: measured against a Conexant, its level was -25.0 dBFS during
        TRN and -24.6 dBFS in the data phase -- constant line power, no step at
        all -- which leaves the equaliser output sqrt(5) too small for a
        power-10 constellation.

        Either convention costs a long, ugly convergence if it is guessed wrong.
        The first live call spent nine seconds with the decision-directed loop
        walking the taps up by 2.24x, its eye at a median distance of 0.48 with
        only 33% of symbols inside 0.35, and the far end gave up and asked for a
        retrain. So this measures instead: collect nsym symbols, then apply the
        one gain that puts their mean power where the new constellation is. The
        same one-shot correction the CMA-to-dd handover already uses.
        """
        self.mode = mode
        self._pend = []
        # The window has to grow with the constellation. The measurement can
        # straddle the boundary -- the far end may still be sending the 4-point
        # rate signal for a few symbols -- and the contamination is in proportion
        # to the power ratio: 10 against 2 at 9600, but 41 against 2 at 14 400.
        # Measured at 128 points with a fixed 200: the caller's eye sat at a
        # median distance of 0.561 with 823 of 4000 symbols outside the decision
        # radius, against 0.040 and none for the answerer, which switches clear
        # of the boundary.
        if nsym is None:
            nsym = 200 * max(1, len(mode.points) // 32)
        self._pend_n = nsym

    def _rescale_check(self, syms):
        """Apply the deferred gain once enough symbols have arrived."""
        if self._pend is None:
            return
        self._pend.extend(abs(z) ** 2 for z in syms)
        if len(self._pend) < self._pend_n:
            return
        m = sum(self._pend) / len(self._pend)
        self._pend = None
        if m > 0:
            g = math.sqrt(self.mode.power / m)
            self.w = [v * g for v in self.w]
            self.events.append((self.nsym, "rescale %.3f" % g))

    def reacquire(self):
        """Drop back to blind acquisition, keeping timing and carrier.

        The threshold-driven retrain in feed() exists for a channel that drifts
        away underneath a working receiver. This is the other case: the caller
        *knows* the constellation just changed, so the decision-directed solution
        is invalid by construction and there is nothing to wait for. Waiting for
        the error threshold to notice is not equivalent -- on a 16-point set a
        stale equaliser can sit at a median distance of 0.86 against a decision
        half-distance of 1.0, which makes every decision a coin flip, keeps the
        error just under the retrain threshold, and stalls there indefinitely.
        """
        self.dd = False
        self.acq_buf = []
        self.lockerr = 0.0

    def feed(self, samples):
        """Consume samples (any number), return the symbols they produced."""
        lut, per, n0 = self.lut, self.per, self.nfed
        buf = self.buf
        for i in range(len(samples)):
            buf.append(samples[i] * lut[(n0 + i) % per])
        self.nfed = n0 + len(samples)
        out = []
        if self.g0 is None:
            self._prologue()
            if self.g0 is None:
                return out
        self._loop(out)
        self._trim()
        if out:
            self._rescale_check(out)
        return out

    def close(self):
        """End of stream: finish a short capture whose prologue never filled."""
        out = []
        if self.g0 is None:
            self._prologue(final=True)
            if self.g0 is None:
                return out
        self._loop(out)
        return out

    def _loop(self, out):
        # Hot state into locals. The inner loop runs once per half-symbol -- 78
        # thousand times for a minute of line time -- and attribute lookups there
        # cost several times what a local does.
        tau, t_freq = self.tau, self.t_freq
        eq, w = self.eq_buf, self.w
        ph, c_freq, pwr = self.ph, self.c_freq, self.pwr
        half, n, drift, nsym = self.half, self.n, self.drift, self.nsym
        dd, acq_buf, lockerr, dd_at = self.dd, self.acq_buf, self.lockerr, self.dd_at
        ntaps, g0 = self.ntaps, self.g0
        mu_cma, mu_dd, leak = self.mu_cma, self.mu_dd, self.leak
        kp_t, ki_t, acq_t_gain = self.kp_t, self.ki_t, self.acq_t_gain
        kp_c, ki_c = self.kp_c, self.ki_c
        settle, log_every = self.settle, self.log_every
        mode = self.mode
        r2 = mode.r2
        sps, hstep = self.sps, self.sps_half   # `half` is the sample history

        while True:
            s = self._at(tau)
            if s is None:
                break
            s *= g0
            pwr += 0.001 * (abs(s) ** 2 - pwr)
            half.append(s)
            if len(half) > 3:
                half.pop(0)
            eq.pop(0)
            eq.append(s)

            # n is the half-symbol index of the sample just taken, counted from
            # tau0. Even n is an on-symbol instant (tau0 + m*SPS); odd n is a
            # mid-symbol instant. Incrementing before this test -- which an
            # earlier version did -- puts every decision on a mid-point, i.e. the
            # worst possible sampling phase, and no amount of equalisation
            # recovers from that.
            if n % 2 == 0 and n >= 2:
                # ---- Gardner timing error, on the pre-equaliser stream ----
                mid, cur, prev = half[1], half[2], half[0]
                # Gardner's detector is Re{conj(mid) * (cur - prev)}; the sign
                # here is negative because `adj` lengthens the step, so a
                # positive detector output (instants running late relative to
                # the symbol peaks) has to shorten it. Measured both ways: with
                # this sign the loop settles at 0.009 samples of accumulated
                # correction on a clean signal, with the other it runs off to
                # -8.5 samples and the eye closes.
                e_t = -(mid.conjugate() * (cur - prev)).real / (pwr + 1e-9)
                if e_t > 2.0:
                    e_t = 2.0
                elif e_t < -2.0:
                    e_t = -2.0
                tg = 1.0 if dd else acq_t_gain
                t_freq += ki_t * tg * e_t
                if t_freq > 0.05:
                    t_freq = 0.05
                elif t_freq < -0.05:
                    t_freq = -0.05
                adj = kp_t * tg * e_t + t_freq
                if adj > 0.25:
                    adj = 0.25
                elif adj < -0.25:
                    adj = -0.25

                # ---- equaliser output, then derotate ----
                y = 0j
                for k in range(ntaps):
                    y += w[k] * eq[ntaps - 1 - k]
                yr = y * cmath.exp(-1j * ph)

                ref = self.ref
                if ref is not None and self.ref_i < len(ref):
                    # ---- reference-directed (5.2.3's purpose for TRN) ----
                    d = ref[self.ref_i]
                    self.ref_i += 1
                    self.ref_used += 1
                    e_rot = d - yr
                    e_eq = e_rot * cmath.exp(1j * ph)
                    mu = mu_dd
                    nrm = sum(abs(v) ** 2 for v in eq) + 1e-9
                    e_c = (yr * d.conjugate()).imag / (abs(d) ** 2 + 1e-9)
                    c_freq += ki_c * e_c
                    ph += kp_c * e_c + c_freq
                    self.ref_err += 0.01 * (abs(e_rot) ** 2 - self.ref_err)
                    fast = self.fast_err + 0.1 * (abs(e_rot) ** 2
                                                  - self.fast_err)
                    self.fast_err = fast
                elif not dd:
                    # CMA: cost depends on |y| only, so it is blind to ph and
                    # adapts on the unrotated output.
                    a2 = y.real * y.real + y.imag * y.imag
                    if a2 > self.CMA_CLAMP * r2:
                        # Grossly out of range. The cubic gradient step is a
                        # local method and diverges from here -- fed a receiver
                        # whose input gain had been measured on silence, it
                        # overflowed a double in a few hundred symbols and took
                        # the modem down with it. CMA's whole object is to bring
                        # |y|^2 to r2, so when it is this far out, apply the gain
                        # directly instead of taking a gradient step towards it.
                        g = math.sqrt(r2 * self.CMA_CLAMP / a2)
                        w = [v * g for v in w]
                        y *= g
                        a2 = r2 * self.CMA_CLAMP
                        self.clamps += 1
                    e_eq = y * (r2 - a2)
                    mu = mu_cma
                    nrm = sum(abs(v) ** 2 for v in eq) * r2 + 1e-9
                    if not self.frozen:
                        acq_buf.append(y)
                else:
                    d = mode.slice(yr)
                    e_rot = d - yr
                    # the tap update lives before the rotator, so carry the
                    # error back through it
                    e_eq = e_rot * cmath.exp(1j * ph)
                    mu = mu_dd
                    nrm = sum(abs(v) ** 2 for v in eq) + 1e-9
                    # ---- learn nothing while the gain is known to be wrong ----
                    # rescale_to() defers its correction until it has enough
                    # symbols to measure the new constellation's power, and that
                    # measurement is worth about a factor of five. Every decision
                    # taken before it lands is against a target the output cannot
                    # reach, so the error is not an error -- it is the gain step,
                    # in disguise, driving the taps somewhere they should never
                    # go. The window is 200 symbols per 32 points, so it is 400
                    # at 12 000 and 800 at 14 400, and the damage scales with it
                    # twice over: twice as long to do it in, and half the decision
                    # margin to do it with. 12 000 survived and came back; 14 400
                    # locked at a median of 8.8% of the rms radius, on a channel
                    # with no impairment at all, and stayed there.
                    #
                    # fast_err is deliberately still updated: circuit 104 must
                    # stay clamped through this, and a real error is exactly what
                    # should keep it clamped. What is suppressed is *learning*
                    # from it -- the taps, the carrier loop, and the
                    # loss-of-lock detector, which would otherwise read the
                    # pending gain step as a channel that had fallen apart and
                    # call for a retrain.
                    if self._pend is not None:
                        fast = self.fast_err + 0.1 * (abs(e_rot) ** 2
                                                      - self.fast_err)
                        self.fast_err = fast
                        out.append(yr)
                        self.gated += 1
                        nsym += 1
                        tau += hstep + adj
                        drift += adj
                        n += 1
                        continue
                    # ---- carrier loop ----
                    e_c = (yr * d.conjugate()).imag / (abs(d) ** 2 + 1e-9)
                    c_freq += ki_c * e_c
                    ph += kp_c * e_c + c_freq
                    # ---- loss-of-lock detector ----
                    # DD-LMS cannot re-acquire once its decisions go wrong: it
                    # is driven by the very decisions that have become garbage.
                    # Measured on a channel that changes mid-capture, it locks
                    # at 0.07, breaks at the change and never comes back. So
                    # watch the decision error and fall back to CMA, which does
                    # not depend on decisions at all. This is a local version of
                    # what V.22bis 6.4 does on the wire when a modem detects
                    # loss of equalisation.
                    # Two windows on the same error, for two different jobs.
                    # `lockerr` is slow (100 symbols) because deciding to throw
                    # away a converged equaliser should not be a hair trigger.
                    # `fast_err` is short (10 symbols, ~17 ms) and only gates
                    # whether characters are handed on -- V.22bis 6.4 a) says
                    # circuit 104 "may be clamped to binary 1" on loss of
                    # equalisation, and the slow window is far too late for that:
                    # at the end of a call the far carrier dies over about 100 ms
                    # and the slow detector let eight wrong characters through
                    # before it noticed.
                    fast = self.fast_err + 0.1 * (abs(e_rot) ** 2 - self.fast_err)
                    self.fast_err = fast
                    lockerr += 0.01 * (abs(e_rot) ** 2 - lockerr)
                    if lockerr > self.lose_thresh and nsym - dd_at > self.lose_hold:
                        dd = False
                        self.retrains += 1
                        acq_buf = []
                        lockerr = 0.0
                        self.events.append((nsym, "acq"))

                if not self.frozen:
                    g = mu / nrm
                    lk = 1.0 - leak
                    for k in range(ntaps):
                        w[k] = w[k] * lk + g * e_eq * eq[ntaps - 1 - k].conjugate()

                out.append(yr)
                if dd and nsym - dd_at >= settle:
                    if self.fast_err < self.out_thresh:
                        self.locked_out.append(yr)
                    else:
                        self.gated += 1
                nsym += 1
                if log_every and nsym % log_every == 0:
                    self.tlog.append((nsym, tau, e_t, t_freq, abs(w[ntaps // 2])))

                # ---- hand over from CMA to decision-directed ----
                if (not dd and len(acq_buf) >= self.acq_min
                        and len(acq_buf) % self.acq_check == 0):
                    med, gsc, ph0, slope = assess(acq_buf[-self.acq_win:], mode,
                                                  baud=self.baud)
                    self.acq_med = med
                    if med <= self.acq_thresh:
                        # CMA drives |y|^2 -> r2, but the constellation's mean
                        # power is mode.power, so at V.22bis 2400 its output is
                        # sqrt(1.32) too large
                        # (and at 1200, where r2 is exactly 10, it is already
                        # right); fold the correction into the taps, once.
                        w = [v * gsc for v in w]
                        ph, c_freq = ph0, slope
                        dd = True
                        dd_at = nsym
                        lockerr = 0.0
                        acq_buf = []
                        self.events.append((nsym, "dd"))

                tau += hstep + adj
                drift += adj
            else:
                tau += hstep
            n += 1

        (self.tau, self.t_freq, self.w, self.ph, self.c_freq, self.pwr,
         self.n, self.drift, self.nsym, self.dd, self.acq_buf, self.lockerr,
         self.dd_at) = (tau, t_freq, w, ph, c_freq, pwr, n, drift, nsym, dd,
                        acq_buf, lockerr, dd_at)

    # -- outputs ---------------------------------------------------------

    def take_locked(self):
        """Drain the symbols produced while settled in decision-directed mode.

        The gate is `settle` symbols past the last acquisition, which is the
        equaliser's own tap time constant (ntaps/mu_dd ~= 1050 symbols). It is
        the local analogue of V.22bis 6.3.1.1.2 e): the receiver does not claim
        to be carrying data until it has evidence that it is.
        """
        out, self.locked_out = self.locked_out, []
        return out

    def info(self):
        return {"nsym": self.nsym, "dd_reached": self.dd, "input_gain": self.g0,
                "mode": self.mode.name,
                "retrains": self.retrains, "dd_at": self.dd_at,
                "acq_median": self.acq_med, "events": list(self.events),
                "timing_drift_samples": self.drift, "gated": self.gated,
                "carrier_hz": self.c_freq * self.baud / (2 * math.pi),
                "taps": list(self.w), "tlog": self.tlog}


class TrackingRx:
    """Batch wrapper: feed a whole array through StreamRx in one call."""

    def __init__(self, **kw):
        self.kw = kw

    def run(self, x, carrier=v22.LOW, log_every=0):
        rx = StreamRx(carrier=carrier, log_every=log_every, **self.kw)
        syms = rx.feed(x)
        syms += rx.close()
        return syms, rx.info()


def quality(syms, skip=0, mode=QAM2400):
    """Median and 90th-percentile distance to the nearest lattice point."""
    z = syms[skip:]
    if not z:
        return None
    d = sorted(abs(mode.slice(v) - v) for v in z)
    return d[len(d) // 2], d[int(0.9 * len(d))]


def decode(syms, sync=None, mode=QAM2400, descrambler=None):
    """Symbols -> descrambled bit stream, past the descrambler's transient.

    V.22bis 5.2's descrambler is self-synchronising with polynomial
    1 + x^-14 + x^-17, so its shift register is 17 bits deep and its first 17
    output bits are computed from register contents that are not yet the real
    ones. Those bits are wrong by construction, not by accident, and on a stream
    that starts mid-transmission they are the *only* thing wrong: dropping them
    took a 61-second single-pass decode from 7 bad characters in 11 668 to
    exactly zero in 11 667.
    """
    bits, _ = mode.decode(syms)
    scr = descrambler if descrambler is not None else v22.Scrambler()
    if sync is None:
        sync = getattr(scr, "width", 17)
    return scr.descramble(bits)[sync:]


# ---------------------------------------------------------------------------
# Streaming bit and character recovery
# ---------------------------------------------------------------------------

class AsyncFramer:
    """Streaming 8N1 deframer: bits in, characters out.

    Causal, which `v22bis_rx.deframe` is not -- that one searches ten bit offsets
    over a whole array and keeps whichever scored best globally, which is fine
    for scoring a capture and impossible live.

    The hard part is acquiring the framing when joining a stream in progress.
    Three things that look like solutions are not, and all three were measured:

      * Plain hunting -- take the next zero as a start bit -- false-locks and
        stays there, because at a wrong phase the stop bit is mark often enough
        that it rarely re-hunts. On a stream with no idle bits: 1610 of 1644
        characters wrong.
      * Waiting for an idle run does not work, because a run of mark bits is not
        evidence of idle: a 0xFF data byte is nine consecutive ones by itself.
        The framer locked onto a run inside the data and never recovered.
      * Scoring ten framing offsets assumes characters are ten bits apart. The
        Cirrus sends about 12.6 bits per character and V.14 lets the gap vary,
        so there is no fixed stride to score against.
      * Even *validated* hunting -- requiring N consecutive good frames before
        believing the lock -- is not enough on its own, because the hunt itself
        is data-driven: on a periodic pattern it cycles through a subset of
        phases and can never visit the right one. Measured: 122 framing errors,
        zero locks.

    What works is a systematic sweep. Every zero bit in the acquisition window is
    a candidate frame start; for each, simulate forward -- start bit, eight data
    bits, stop bit, then hunt for the next zero -- and count how many consecutive
    frames validate. Take the best. Including the hunt step in the simulation is
    what makes this indifferent to the sender's idle discipline: idle bits are
    simply skipped, so the same search locks a 10-bit back-to-back stream and a
    12.6-bit idle-framed one.

    Once locked, the cheap per-bit machine runs. `resync_after` consecutive
    framing errors send it back to acquisition rather than letting it hunt
    blindly, because blind hunting on a stream with no idle bits is exactly how
    one bit error becomes permanent garbage -- the framing slip written up in
    testrig/v22-modem.md, seen from the receiving end.
    """

    def __init__(self, confirm=8, window=600, cap=40, resync_after=4):
        self.confirm = confirm
        self.window = window
        self.cap = cap
        self.resync_after = resync_after
        self.locked = False
        self.buf = []         # bits held during acquisition
        self.n = 0            # 0 = hunting, 1..8 = collecting, 9 = stop bit due
        self.acc = 0
        self.good = 0
        self.bad = 0
        self.err_run = 0
        self.locks = 0
        self.restored = 0     # stop bits re-inserted per V.14

    @staticmethod
    def _simulate(bits, start, cap):
        """Frames decodable from `start`, as (count, chars, next_index)."""
        i = start
        chars = []
        while len(chars) < cap:
            while i < len(bits) and bits[i]:
                i += 1                          # skip idle / hunt for start bit
            if i + 10 > len(bits):
                break
            if not bits[i + 9]:                 # stop bit is not mark
                break
            v = 0
            for k in range(8):
                if bits[i + 1 + k]:
                    v |= 1 << k
            chars.append(v)
            i += 10
        return len(chars), chars, i

    def _acquire(self, out):
        """Sweep candidate frame starts over the buffered bits."""
        bits = self.buf
        best = (0, None, 0)
        for c in range(len(bits)):
            if bits[c]:
                continue                        # only a zero can be a start bit
            nf, chars, nxt = self._simulate(bits, c, self.cap)
            if nf > best[0]:
                best = (nf, chars, nxt)
        nf, chars, nxt = best
        if nf >= self.confirm:
            # Emit the evidence, not the hypothesis. If the candidate's start is
            # wrong it lands mid-character, so its *first* frame is bogus -- and
            # the hunt that follows lands on a real start bit, after which every
            # later frame is correct. That candidate therefore scores just as
            # well as the right one and, being earlier, wins. Measured: exactly
            # one wrong character, always at index 0, with a valid stop bit and
            # no framing error to show for it.
            #
            # Frames 2..n are hunted forward from the previous stop bit, so they
            # are self-consistent whatever the initial guess was. Dropping frame
            # 1 costs at most one good character and makes everything emitted
            # trustworthy.
            out.extend(chars[1:])
            self.good += nf
            self.locked = True
            self.locks += 1
            self.err_run = 0
            self.n = 0
            self.buf = bits[nxt:]
            # anything after the validated run goes through the locked machine
            tail, self.buf = self.buf, []
            self._run(tail, out)
            return
        # nothing convincing yet: keep the newest half and wait for more
        if len(bits) >= self.window:
            self.buf = bits[len(bits) // 2:]

    def _run(self, bits, out):
        for b in bits:
            n = self.n
            if n == 0:
                if b == 0:
                    self.n, self.acc = 1, 0
            elif n <= 8:
                if b:
                    self.acc |= 1 << (n - 1)
                self.n = n + 1
            else:
                if b:
                    out.append(self.acc & 0xFF)
                    self.good += 1
                    self.err_run = 0
                    self.n = 0
                else:
                    # A zero where the stop bit belongs is what V.14 stop-bit
                    # deletion looks like from here: the character is complete
                    # and this zero is the next start bit. Table 8/V.32 sizes how
                    # often it can happen -- the DTE may run 1% above the line
                    # rate in the basic range, 2.3% extended -- so deletions are
                    # occasional and isolated.
                    #
                    # A framing slip looks the same for one character and then
                    # keeps happening, because at a wrong phase the stop position
                    # is data. So deletion is accepted but counted, and a *run*
                    # of them is still treated as lost framing. That keeps the
                    # resync that stopped one bit error becoming permanent
                    # garbage, while no longer rejecting legal V.14.
                    out.append(self.acc & 0xFF)
                    self.restored += 1
                    self.err_run += 1
                    if self.err_run >= self.resync_after:
                        self.bad += 1
                        self.locked = False
                        self.n = 0
                        self.err_run = 0
                        return bits
                    self.n, self.acc = 1, 0
        return []

    def feed(self, bits):
        out = bytearray()
        pending = list(bits)
        while pending:
            if self.locked:
                pending = self._run(pending, out) or []
            else:
                self.buf.extend(pending)
                pending = []
                if len(self.buf) >= self.window:
                    self._acquire(out)
        return bytes(out)


class SymbolDecoder:
    """Streaming symbols -> characters: differential decode, descramble, deframe.

    All three stages carry state across calls, which is the only reason this can
    run per RTP frame: the quadbit decode is differential so it needs the
    previous symbol, the descrambler is a 17-bit shift register, and the framer
    may be part way through a character when a frame boundary falls.
    """

    def __init__(self, sync=None, mode=QAM2400, descrambler=None):
        self.prev_q = None
        # The descrambler is not a property of the constellation: V.22bis uses
        # one polynomial in both directions, while V.32 4.1.1 gives the calling
        # and answering modems *different* ones and has each descramble with the
        # other's. So it comes in from outside, and `sync` -- the number of
        # output bits that are wrong by construction while the register fills --
        # is its degree: 17 for V.22bis, 23 for V.32.
        self.scr = descrambler if descrambler is not None else v22.Scrambler()
        self.sync = sync if sync is not None else getattr(self.scr, "width", 17)
        self.mode = mode
        self.framer = AsyncFramer()

    def feed(self, syms):
        bits, self.prev_q = self.mode.decode(syms, self.prev_q)
        d = self.scr.descramble(bits)
        if self.sync:
            # drop the descrambler's own transient, once: its register is 17 bits
            # deep, so its first 17 outputs are computed from contents that are
            # not yet the real ones and are wrong by construction.
            k = min(self.sync, len(d))
            d = d[k:]
            self.sync -= k
        return self.framer.feed(d)


class LiveRx:
    """Live V.22bis receive path: sample frames in, characters out.

    Drop-in for an RTP callback. `feed` is bounded work -- the receiver holds a
    sliding sample buffer, not the call -- and it times itself so the real-time
    margin is a measured number rather than a hope.
    """

    def __init__(self, carrier=v22.LOW, mode=QAM2400, descrambler=None, **kw):
        self.mode = mode
        self.rx = StreamRx(carrier=carrier, mode=mode, **kw)
        self.dec = SymbolDecoder(mode=mode, descrambler=descrambler)
        self.data = bytearray()
        self.frames = 0
        self.worst_ms = 0.0
        self.total_ms = 0.0

    def feed(self, samples):
        t0 = time.perf_counter()
        self.rx.feed(samples)
        got = self.dec.feed(self.rx.take_locked())
        if got:
            self.data.extend(got)
        self.frames += 1
        dt = (time.perf_counter() - t0) * 1000.0
        self.total_ms += dt
        if dt > self.worst_ms:
            self.worst_ms = dt
        return got

    def summary(self):
        i = self.rx.info()
        return {"chars": len(self.data), "symbols": i["nsym"],
                "mode": i["mode"], "gated": i["gated"],
                "acquired_at": i["dd_at"], "retrains": i["retrains"],
                "carrier_hz": i["carrier_hz"],
                "framing_good": self.dec.framer.good,
                "framing_bad": self.dec.framer.bad,
                "frames": self.frames, "worst_ms": self.worst_ms,
                "mean_ms": self.total_ms / max(self.frames, 1)}
