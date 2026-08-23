"""Echo cancellation for the soft modem.

V.32 is full duplex on one circuit, so a modem hears itself. On this rig the
FRITZ!Box's analogue hybrid returns our own signal 50 to 66 ms later at roughly
19 dB down -- measured, see `testrig/echo-cancellation.md` -- and with no canceller
that echo is the dominant noise source in our own receiver. It is why 9600 with
trellis coding has no margin to spare here: at any transmit level either the far
end cannot decode a 32-point constellation or our own echo drowns its reply.

The design is deliberately timid, because a canceller that misbehaves is worse
than no canceller at all:

  * **It does nothing until it has found an echo.** The bulk delay is estimated
    by cross-correlation and has to clear a margin over the correlation floor
    before a single tap is allowed to move. With no echo this is a pass-through,
    and not by accident -- it is the default state.
  * **Leakage** pulls the taps back towards zero, so an echo that goes away takes
    the filter with it instead of leaving a stale filter injecting noise.
  * **A guard measures whether it is actually helping.** ERLE is computed over
    windows; if the canceller is making the signal worse, the taps are decayed
    and the search restarts. "It might be making things worse" is a testable
    condition, so it is tested rather than assumed away.
  * **The delay is constant** whether adapting or not. Switching a delay on when
    the filter locks would splice the sample stream, and this codebase has twice
    paid for that: the equaliser is frozen rather than starved, and the V.14
    framer is gated on its output rather than its input, for this exact reason.

The reference signal must be what was actually *emitted* -- for a G.711 path that
means the encoded-then-decoded samples, not the pre-encode ones, or quantisation
noise limits how far the echo can be cancelled.
"""
import math

SR = 8000.0

# The echo cannot come back sooner than one RTP frame, and that is not an
# assumption about the network -- it is forced by the loop. Our frame k is emitted
# only after inbound frame k has been read, and the far end has to packetise
# whatever it reflects, so the earliest our own signal can reappear is in inbound
# frame k+1.
#
# That makes the whole thing causal with **no delay at all**. To cancel inbound
# sample j we need tx[j - bulk]; with bulk >= FRAME the newest sample required is
# 160k - 1, which is exactly what has been pushed by the time inbound frame k
# arrives. An earlier version held a frame back to be safe, which cost 48 T of
# latency, broke 5.4.2's 64 +/- 2 T turnaround and stopped one of the two modems
# from completing a handshake at all. There was never anything to hold back for.
FRAME = 160
HOLD = 0

SPAN = 32           # taps, once the bulk delay is known
# A lag below one frame is not a short echo, it is a spurious correlation peak,
# and locking onto one fits the filter to noise and *adds* to the residual. The
# first version searched from 8 and duly found a strong peak at 77 samples --
# 9.6 ms, which would have the echo arriving before it was transmitted.
SEARCH_MIN = FRAME
# The echo delay is NOT a property of the rig, it is a property of the call.
# Four consecutive connections put it at 397, 461, 493 and 525 samples -- 50 to
# 66 ms -- because the loop includes the FRITZ!Box's jitter buffer and whatever
# frame alignment the call happens to settle on. (Two of those were first read off
# capture files as 77 and 205; rtp.pump emits two priming frames before any
# inbound exists, so every file-derived lag was 320 samples short. The scan log
# was never affected.) This covers 100 ms and pays for the extra lags with a long
# window.
# Widening costs threshold margin as sqrt(ln L), which is cheap; guessing the
# range costs the whole feature.
SEARCH_MAX = 800
SEARCH_WIN = 32768  # samples of history one search correlates over
SEARCH_STEP = 2     # ... sampled this sparsely, so 16384 terms per lag
SEARCH_BUDGET = 1   # lags per frame. One lag over a 16384-term window is
                    # ~16k multiplies, about 15% of a 20 ms frame's budget in
                    # pure Python, so a full scan takes ten seconds. Slow, but
                    # the pump keeps its deadline, which matters more.
# A correlation peak has to beat what pure chance produces. For L lags and N
# terms the largest spurious |rho| is about sqrt(2 ln L / N): with L = 592 and
# N = 1024 that is 0.11, which is *exactly* the peak an echo-free line produced
# on the first attempt here. Comparing the peak against the median across lags
# is not a test -- the maximum of many noisy estimates is several times the
# median by construction. So the threshold is derived from the null instead.
#
# The echo this exists for gives rho ~= 0.12 while both ends are talking, and
# with 792 lags and 16384 terms the null maximum is 0.029, so the threshold sits
# at 0.051 with a margin of about 2.3. On a real call the strongest unrelated
# peak measured 0.042, below that threshold. Two scans must also agree on the
# lag, which chance will not do twice.
NULL_K = 1.8        # margin over the expected spurious maximum
CONFIDENT_RHO = 0.5  # above this it is unambiguous: lock on one look
CONFIDENT_MULT = 2.0  # ... and so is anything this far above the threshold. At
                      # twice the threshold a spurious peak needs 3.6 times the
                      # null, which for 16384 terms is not going to happen; the
                      # real echo measured 0.12 against a threshold of 0.05.
MIN_RHO = 0.02      # never lock below this, whatever the statistics say
# On a full duplex circuit the near-end signal is always in the error term, and
# here it arrives about 19 dB *above* the echo we are trying to remove. NLMS
# misadjustment goes as mu times that ratio, so the step has to be small: at
# mu = 1e-3 the predicted residual is about -14 dB, which is what is measured,
# and the price is a convergence time near span/mu = 32 000 samples, four
# seconds. Faster steps simply track the far end's data instead of the echo.
MU = 0.001          # NLMS step, once converged
MU_FAST = 0.01      # ... and while pulling in, for the first FAST_N samples
FAST_N = 8000
LEAK = 2e-6         # per-sample pull towards zero; 60 s time constant, slow
                    # enough not to fight convergence. The guard, not leakage,
                    # is what reacts quickly when an echo disappears.
GUARD_WIN = 8000    # 1 s of ERLE before the guard passes judgement
GUARD_MIN_DB = -0.5  # below this the canceller is hurting, not helping
WMAX = 4.0          # any tap beyond this is divergence, not adaptation


def null_rho(lags, terms):
    """The largest |rho| chance alone is expected to produce."""
    if lags < 2 or terms < 2:
        return 1.0
    return math.sqrt(2.0 * math.log(lags) / terms)


class EchoCanceller:
    """Our transmit history in, our receive stream out, minus what we caused.

    Feed it with `push_tx` (what we emitted) and `feed` (what arrived). `feed`
    returns cancelled samples HOLD behind the input, always -- so the stream it
    produces is contiguous and its length is stable, whatever the filter is
    doing.
    """

    def __init__(self, span=SPAN, hold=HOLD, mu=MU, mu_fast=MU_FAST,
                 fast_n=FAST_N, leak=LEAK,
                 search=(SEARCH_MIN, SEARCH_MAX), min_rho=MIN_RHO,
                 guard_win=GUARD_WIN, enabled=True, budget=SEARCH_BUDGET,
                 win=SEARCH_WIN, step=SEARCH_STEP):
        self.span = int(span)
        self.hold = int(hold)
        self.mu = float(mu)
        # A single step size has to choose between pulling in quickly and
        # settling accurately. Two do not: a large step for the first second
        # after locking gets most of the echo out of the way, then a small one
        # stops the far end's data from being tracked as if it were echo.
        self.mu_fast = float(mu_fast)
        self.fast_n = int(fast_n)
        self.locked_n = 0
        self.leak = float(leak)
        self.search_lo, self.search_hi = search
        self.min_rho = float(min_rho)
        self.guard_win = int(guard_win)
        self.enabled = bool(enabled)
        # Lags per frame. The default keeps one scan inside a fraction of a
        # frame's compute budget; offline callers can raise it freely, since it
        # trades wall-clock against nothing else.
        self.budget = int(budget)
        # Window length and how sparsely it is sampled. Exposed because the
        # threshold is derived from them, so a test that wants a cheap scan can
        # have one without silently weakening the statistics it is checking.
        self.win = int(win)
        self.step = int(step)

        self.w = [0.0] * self.span
        self.bulk = None            # samples of bulk delay, once found
        self.locked = False

        self.tx = []                # emitted samples, absolute index tx0 + i
        self.tx0 = 0
        self.rx = []                # arrived samples, absolute index rx0 + i
        self.rx0 = 0
        self.out_n = 0              # how many samples we have emitted

        self._pow = 0.0             # running power of the tap window
        self._pow_valid = False     # ... whether it has been primed; see feed
        self.d2 = 0.0               # guard accumulators
        self.e2 = 0.0
        self.gn = 0

        self.scan_r = None          # the snapshot being scanned
        self.scan_floor = 0         # correlate nothing older than this index
        self.scan_t = None
        self.scan_lag = 0
        self.scan_best = (0.0, None)
        self.scan_rr = 0.0
        self.scan_tt = 1.0
        self.scan_off = 0
        self.agree = None           # a lag one scan proposed, awaiting a second
        self.last_thresh = 0.0
        self.scan_log = []          # (peak, lag) per completed scan
        self.searches = 0
        self.adapts = 0
        self.resets = 0
        self.erle_db = 0.0          # last completed guard window
        self.best_rho = 0.0

    # -- input ----------------------------------------------------------

    def push_tx(self, samples):
        self.tx.extend(samples)
        self._trim()

    def _trim(self):
        """Keep only the history the search and the filter can still ask for."""
        need = self.search_hi + self.span + self.win + 4 * self.hold
        if len(self.tx) > 2 * need:
            drop = len(self.tx) - need
            del self.tx[:drop]
            self.tx0 += drop
        if len(self.rx) > 2 * need:
            drop = len(self.rx) - need
            del self.rx[:drop]
            self.rx0 += drop

    def _tx_at(self, idx):
        i = idx - self.tx0
        return self.tx[i] if 0 <= i < len(self.tx) else 0.0

    def _rx_at(self, idx):
        i = idx - self.rx0
        return self.rx[i] if 0 <= i < len(self.rx) else 0.0

    # -- the search -----------------------------------------------------

    def defer_search(self):
        """Drop any part-finished scan, and correlate nothing recorded before
        now.

        The caller is the one that knows when the line is worth correlating. A
        cross-correlation delay estimate wants a long stationary stretch, and a
        V.32 start-up is the opposite of that: a dozen short segments -- answer
        tone, AC, CA, S, TRN, rate sequences -- each with its own spectrum. It is
        also the part of the call with the tightest timing, and a scan costs
        about 1.4 ms of every 20 ms frame.

        Measured, on the Cirrus dial-in: 9 of 9 calls reached the data phase with
        no canceller, 4 of 4 with the canceller in the path but not scanning, and
        1 of 9 with it scanning. The samples were provably identical in all three
        -- the filter never locked -- so what the scan was costing was time, and
        it could not have paid it back: 640 lags at one lag per frame is 12.8 s,
        longer than the whole handshake.
        """
        self.scan_r = None
        self.scan_floor = min(self.rx0 + len(self.rx), self.tx0 + len(self.tx))

    def _begin_scan(self):
        """Snapshot a window and start scanning it, a few lags per frame.

        The window is copied because it has to hold still while it is scanned,
        and the scan is spread over frames because doing 2.4 million multiplies
        in one call would stall the RTP pump and drop audio. Correctness of the
        estimate does not depend on finishing quickly; the pump's does.
        """
        hi = min(self.rx0 + len(self.rx), self.tx0 + len(self.tx))
        start = hi - self.win
        if start < max(self.rx0, self.tx0 + self.search_hi, self.scan_floor):
            return False
        # The receive side is sampled sparsely -- that only costs terms in the
        # estimate. The transmit side is kept at full rate, because a lag that
        # is not a multiple of the step still has to land on a real sample, and
        # decimating it made every odd lag index the wrong place.
        self.scan_r = [self._rx_at(j) for j in range(start, hi, self.step)]
        self.scan_t = [self._tx_at(j) for j in
                       range(start - self.search_hi, hi)]
        self.scan_off = self.search_hi
        rr = 0.0
        for v in self.scan_r:
            rr += v * v
        self.scan_rr = rr
        # The transmit-side normaliser is computed once for the whole snapshot
        # instead of once per lag. Over a stationary window a one-sample shift
        # does not change the energy meaningfully, and comparisons between lags
        # are unaffected either way -- but it halves the inner loop.
        tt = 0.0
        m = len(self.scan_r)
        base = self.search_hi - (self.search_lo + self.search_hi) // 2
        for i in range(m):
            u = self.scan_t[i * self.step + base]
            tt += u * u
        self.scan_tt = tt if tt > 0 else 1.0
        self.scan_lag = self.search_lo
        self.scan_best = (0.0, None)
        self.searches += 1
        return True

    def _scan(self, budget):
        """Evaluate up to `budget` more lags. Returns True when a lock is made."""
        n = len(self.scan_r)
        if not n or self.scan_rr <= 0.0:
            self.scan_r = None
            return False
        r = self.scan_r
        t = self.scan_t
        off = self.scan_off
        step = self.step
        done = 0
        nt = len(t)
        while self.scan_lag < self.search_hi and done < budget:
            base = off - self.scan_lag        # t[i*step + base] pairs with r[i]
            s_ = 0.0
            for i in range(n):
                k = i * step + base
                if 0 <= k < nt:
                    s_ += t[k] * r[i]
            rho = abs(s_) / math.sqrt(self.scan_tt * self.scan_rr)
            if rho > self.scan_best[0]:
                self.scan_best = (rho, self.scan_lag)
            self.scan_lag += 1
            done += 1
        if self.scan_lag < self.search_hi:
            return False
        # the scan finished: decide
        peak, lag = self.scan_best
        self.best_rho = max(self.best_rho, peak)
        if len(self.scan_log) < 24:
            self.scan_log.append((peak, lag))
        self.scan_r = self.scan_t = None
        thresh = max(self.min_rho,
                     NULL_K * null_rho(self.search_hi - self.search_lo, n))
        self.last_thresh = thresh
        if peak < thresh or lag is None:
            self.agree = None
            return False
        # One confident look is enough; otherwise two scans must agree, which
        # chance will not do for us.
        if peak >= CONFIDENT_RHO or peak >= CONFIDENT_MULT * thresh \
                or (self.agree is not None and abs(self.agree - lag) <= 2):
            # Never below one frame: the taps run from bulk upwards, so bulk is
            # what decides whether the transmit history exists yet.
            self.bulk = max(FRAME, self.search_lo, lag - self.span // 4)
            self.locked = True
            self.w = [0.0] * self.span
            self._pow_valid = False
            self.locked_n = 0
            self.agree = None
            return True
        self.agree = lag
        return False

    # -- the guard ------------------------------------------------------

    def _judge(self):
        """Did the last window actually improve anything?"""
        if self.d2 <= 0.0 or self.e2 <= 0.0:
            self.d2 = self.e2 = 0.0
            self.gn = 0
            return
        self.erle_db = 10.0 * math.log10(self.d2 / self.e2)
        if self.erle_db < GUARD_MIN_DB:
            # Not helping. Back off rather than persist, and go looking again.
            self.w = [v * 0.5 for v in self.w]
            self.resets += 1
            if self.resets % 3 == 0:
                self.locked = False
                self.bulk = None
                self.w = [0.0] * self.span
        self.d2 = self.e2 = 0.0
        self.gn = 0

    def _diverged(self):
        for v in self.w:
            if not (-WMAX < v < WMAX):
                return True
        return False

    # -- the filter -----------------------------------------------------

    def feed(self, inbound, adapt=True):
        """Arrived samples in; cancelled samples out, HOLD samples behind."""
        self.rx.extend(inbound)
        self._trim()
        # Everything we can emit given the transmit history we hold. Unlocked we
        # are a pass-through and nothing is required, so emit it all; locked, the
        # oldest transmit sample a tap can reach is j - bulk - span, and the
        # newest is j - bulk, so j may run to tx_end + bulk. With bulk >= FRAME
        # that is at or past rx_end, which is why no frame has to be held.
        rx_end = self.rx0 + len(self.rx)
        tx_end = self.tx0 + len(self.tx)
        last = rx_end if not self.locked else min(rx_end, tx_end + self.bulk)
        out = []
        if not self.enabled:
            while self.out_n < last:
                out.append(self._rx_at(self.out_n))
                self.out_n += 1
            return out

        if self.budget > 0 and not self.locked and self.searches < 200:
            if self.scan_r is None:
                self._begin_scan()
            if self.scan_r is not None:
                self._scan(self.budget)

        w = self.w
        span = self.span
        bulk = self.bulk
        leak = self.leak
        while self.out_n < last:
            j = self.out_n
            d = self._rx_at(j)
            if not self.locked:
                out.append(d)
                self.out_n += 1
                continue
            base = j - bulk
            y = 0.0
            for k in range(span):
                y += w[k] * self._tx_at(base - k)
            e = d - y
            out.append(e)
            self.d2 += d * d
            self.e2 += e * e
            self.gn += 1
            if adapt:
                # NLMS with leakage. The normalisation is the power in the tap
                # window, kept as a running sum so this stays O(1) per sample.
                #
                # Priming that sum matters more than it looks. Starting it at
                # zero and then subtracting the outgoing sample drives it
                # negative, and a divisor clamped to a tiny epsilon turns the
                # first update into an enormous one: the filter diverged on the
                # very sample it was switched on, tripped the divergence guard,
                # and unlocked -- over and over, looking exactly like a search
                # that could not make up its mind.
                if not self._pow_valid:
                    acc = 0.0
                    for k in range(span):
                        u = self._tx_at(base - k)
                        acc += u * u
                    self._pow = acc
                    self._pow_valid = True
                else:
                    xin = self._tx_at(base)
                    xout = self._tx_at(base - span)
                    self._pow += xin * xin - xout * xout
                    if self._pow < 0.0:
                        self._pow = 0.0
                # A floor of one least-significant bit per tap, so silence
                # cannot manufacture a huge gradient either.
                mu = self.mu_fast if self.locked_n < self.fast_n else self.mu
                self.locked_n += 1
                g = mu * e / (self._pow + span)
                for k in range(span):
                    w[k] = w[k] * (1.0 - leak) + g * self._tx_at(base - k)
                self.adapts += 1
            self.out_n += 1
            if self.gn >= self.guard_win:
                self._judge()
            if self._diverged():
                self.w = w = [0.0] * self.span
                self.locked = False
                self.bulk = None
                self._pow_valid = False
                self.resets += 1
        return out

    # -- reporting ------------------------------------------------------

    def state(self):
        return ("echo: %s, bulk %s, |w|max %.4f, ERLE %.1f dB, "
                "%d searches (best rho %.3f, threshold %.3f), %d resets\n"
                "        scans: %s"
                % ("locked" if self.locked else "searching",
                   self.bulk if self.bulk is not None else "-",
                   max((abs(v) for v in self.w), default=0.0),
                   self.erle_db, self.searches, self.best_rho,
                   self.last_thresh, self.resets,
                   ", ".join("%.3f@%s" % r for r in self.scan_log)))
