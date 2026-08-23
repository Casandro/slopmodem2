"""V.8 state machines. Pure sample-in/sample-out, no sockets.

Originate (call DCE) role, per ITU-T V.8 8.1:
  WAIT_ANS  - listen for the answering tone (ANS/ANSam at 2100 Hz)
  TE        - after detecting it, transmit no signal for Te (>= 0.5 s; >= 1 s if
              echo-canceller disabling per V.25 is wanted)
  CM        - transmit CM repeatedly on V.21(L), listen for JM on V.21(H)
  CJ        - on >= 2 identical JM sequences, send CJ (three all-zero octets)
  GAP       - no signal for 75 +/- 5 ms
  DONE

Note we deliberately do *not* require the answer tone to be verified as ANSam
rather than plain ANS. On this rig the PBX detects a tone in a narrow band around
2100 Hz and regenerates it as a clean unmodulated sine, so ANSam arrives as ANS
and an AM test can never pass. If the far end was in fact sending ANSam it is
expecting CM, and sending CM costs nothing if it was not.
"""
import math
import dsp, v21, v8

SR = 8000
FRAME = 160

WAIT_ANS, TE, CM, CJ, GAP, DONE = "WAIT_ANS", "TE", "CM", "CJ", "GAP", "DONE"

class Originate:
    def __init__(self, modes, level_dbfs=-30.0, te=1.0, cm_max_s=6.0,
                 tone_hz=2100.0, tone_purity=0.30, tone_frames=6, log=None):
        self.mod = v21.V21Mod("L", level_dbfs=level_dbfs)
        self.dem = v21.V21Demod("H")
        self.modes = set(modes)
        self.cm_octets = v8.build_cm(self.modes)
        self.cm_bits = v8.encode_sequence(self.cm_octets)
        self.te = te
        self.cm_max_s = cm_max_s
        self.tone_hz = tone_hz
        self.tone_purity = tone_purity
        self.tone_frames = tone_frames
        self.state = WAIT_ANS
        self.t = 0.0
        self.t_state = 0.0
        self.outq = []
        self.bitq = []
        self.rxbits = []
        self.tone_run = 0
        self.jm_seen = []
        self.jm_octets = None
        self.agreed = None
        self.events = [] if log is None else log
        self.cm_sequences_sent = 0
        self.cj_samples_left = 0

    def _ev(self, msg):
        self.events.append((round(self.t, 3), self.state, msg))

    def _goto(self, s, msg=""):
        self._ev("-> %s %s" % (s, msg))
        self.state = s
        self.t_state = 0.0

    # ---------- outbound ----------
    def _fill(self, n):
        while len(self.outq) < n:
            if self.state == CM:
                if not self.bitq:
                    self.bitq.extend(self.cm_bits)
                    self.cm_sequences_sent += 1
                take, self.bitq = self.bitq[:8], self.bitq[8:]
                self.outq.extend(self.mod.modulate(take))
            elif self.state == CJ and self.bitq:
                take, self.bitq = self.bitq[:8], self.bitq[8:]
                self.outq.extend(self.mod.modulate(take))
            else:
                self.outq.extend([0] * (n - len(self.outq)))

    def step(self, inbound):
        """One 20 ms frame: inbound linear samples in, outbound linear out."""
        dt = FRAME / float(SR)
        self.t += dt
        self.t_state += dt

        if inbound:
            if self.state in (CM, CJ):
                self.rxbits.extend(self.dem.feed(inbound))
                seqs = v8.find_sequences(self.rxbits)
                for _, octs in seqs:
                    key = tuple(tuple(o) for o in octs)
                    if key not in [k for k, _ in self.jm_seen]:
                        self.jm_seen.append((key, octs))
                    else:
                        pass
                # count repeats of the most common sequence
                counts = {}
                for _, octs in seqs:
                    key = tuple(tuple(o) for o in octs)
                    counts[key] = counts.get(key, 0) + 1
                for key, c in counts.items():
                    if c >= 2 and self.state == CM:
                        self.jm_octets = [list(o) for o in key]
                        p = v8.parse_octets(self.jm_octets)
                        self.agreed = p
                        self._ev("JM x%d: %s  modes=%s cf=%s"
                                 % (c, " ".join(p["raw"]), p["modulations"], p["call_function"]))
                        # queue CJ and note how many samples must drain
                        self.bitq = list(v8.encode_cj())
                        self.cj_samples_left = int(len(self.bitq) * SR / v21.BAUD) + FRAME
                        self._goto(CJ)
                        break

        if self.state == WAIT_ANS:
            ms = dsp.mean_square(inbound) if inbound else 0.0
            pur = (dsp.goertzel(inbound, self.tone_hz) / ms) if ms > 500 else 0.0
            if pur > self.tone_purity:
                self.tone_run += 1
                if self.tone_run >= self.tone_frames:
                    self._ev("answer tone detected (2100 Hz, purity %.2f, %.0f dBFS)"
                             % (pur, dsp.dbfs(math.sqrt(ms))))
                    self._goto(TE)
            else:
                self.tone_run = 0
        elif self.state == TE:
            if self.t_state >= self.te:
                self._ev("Te elapsed (%.2f s), starting CM on V.21(L)" % self.t_state)
                self._goto(CM)
        elif self.state == CM:
            if self.t_state >= self.cm_max_s:
                self._ev("no JM after %.1f s of CM" % self.t_state)
                self._goto(DONE)
        elif self.state == CJ:
            self.cj_samples_left -= FRAME
            if self.cj_samples_left <= 0:
                self._ev("CJ sent (3 all-zero octets on V.21(L))")
                self._goto(GAP)
        elif self.state == GAP:
            if self.t_state >= 0.075:
                self._ev("75 ms gap done - V.8 negotiation complete")
                self._goto(DONE)

        self._fill(FRAME)
        out = self.outq[:FRAME]
        self.outq = self.outq[FRAME:]
        if len(out) < FRAME:
            out = out + [0] * (FRAME - len(out))
        return out


# ---------------------------------------------------------------------------
# Answer (answer DCE) role, per ITU-T V.8 8.2
#   SILENCE  - no signal for >= 0.2 s after connecting to line (8.2)
#   ANSAM    - transmit ANSam, listen for CM on V.21(L)
#   JM       - on >= 2 identical CM sequences, transmit JM on V.21(H) (8.2.2)
#   CJWAIT   - continue JM until all three octets of CJ are received (8.2.3)
#   GAP      - no signal for 75 +/- 5 ms
#   DONE
#
# The ANSam carrier is offset from 2100 Hz on purpose. This rig's PBX detects a
# tone within roughly 2100 +/- 5 Hz and regenerates it as an unmodulated 2100 Hz
# sine, which turns ANSam into plain ANS in transit; the far end then correctly
# abandons V.8 and answers with V.32 signal AA. A carrier at 2090 or 2110 Hz
# escapes the regenerator while still sitting inside the +/- 15 Hz window a V.25
# answer-tone detector accepts, so the AM survives and CM follows.
# ---------------------------------------------------------------------------

SILENCE, ANSAM, JMSTATE, CJWAIT = "SILENCE", "ANSAM", "JM", "CJWAIT"

class Answer:
    def __init__(self, our_modes, level_dbfs=-24.0, carrier=2110.0,
                 lead=0.25, ansam_max=20.0, jm_max=8.0, log=None,
                 ec_prefix=0.0, am_depth=None):
        import ansam as _ansam
        self.mod = v21.V21Mod("H", level_dbfs=level_dbfs)
        self.dem = v21.V21Demod("L")
        self.our_modes = set(our_modes)
        self.carrier = carrier
        self.lead = lead
        self.ansam_max = ansam_max
        self.jm_max = jm_max
        # a long ANSam buffer, streamed out frame by frame
        kw = dict(level_dbfs=level_dbfs, f=carrier, reversal_ms=None)
        if am_depth is not None:
            kw["am_depth"] = am_depth
        self.ansam_buf = _ansam.ansam_samples(ansam_max + 2.0, **kw)
        if ec_prefix > 0:
            # Lead with the echo-canceller disabling tone: 2100 Hz with phase
            # reversals (V.25 2.3), which is the only signal G.168 7.1 says a
            # tone disabler should act on.
            self.ansam_buf = (_ansam.ec_disable_samples(ec_prefix,
                                                        level_dbfs=level_dbfs,
                                                        f=carrier)
                              + self.ansam_buf)
        self.ansam_pos = 0
        self.state = SILENCE
        self.t = 0.0
        self.t_state = 0.0
        self.outq = []
        self.bitq = []
        self.rxbits = []
        self.cm_octets = None
        self.cm_parsed = None
        self.jm_octets = None
        self.agreed = None
        self.cj_count = 0
        self.events = [] if log is None else log
        self.jm_sequences_sent = 0

    def _ev(self, msg):
        self.events.append((round(self.t, 3), self.state, msg))

    def _goto(self, s, msg=""):
        self._ev("-> %s %s" % (s, msg))
        self.state = s
        self.t_state = 0.0

    def _fill(self, n):
        while len(self.outq) < n:
            if self.state == ANSAM:
                take = self.ansam_buf[self.ansam_pos:self.ansam_pos + n]
                self.ansam_pos += len(take)
                if not take:
                    take = [0] * n
                self.outq.extend(take)
            elif self.state in (JMSTATE, CJWAIT):
                if not self.bitq:
                    self.bitq.extend(v8.encode_sequence(self.jm_octets))
                    self.jm_sequences_sent += 1
                take, self.bitq = self.bitq[:8], self.bitq[8:]
                self.outq.extend(self.mod.modulate(take))
            else:
                self.outq.extend([0] * (n - len(self.outq)))

    def step(self, inbound):
        dt = FRAME / float(SR)
        self.t += dt
        self.t_state += dt

        if inbound and self.state in (ANSAM, JMSTATE, CJWAIT):
            self.rxbits.extend(self.dem.feed(inbound))
            if self.state == ANSAM:
                seqs = v8.find_sequences(self.rxbits)
                counts = {}
                for _, octs in seqs:
                    key = tuple(tuple(o) for o in octs)
                    counts[key] = counts.get(key, 0) + 1
                for key, c in counts.items():
                    if c >= 2:
                        self.cm_octets = [list(o) for o in key]
                        p = v8.parse_octets(self.cm_octets)
                        self.cm_parsed = p
                        self._ev("CM x%d: %s  modes=%s cf=%s"
                                 % (c, " ".join(p["raw"]), p["modulations"], p["call_function"]))
                        self.jm_octets, common = v8.build_jm(self.cm_octets, self.our_modes)
                        self.agreed = common
                        self._ev("JM will advertise %s" % (common or "NONE (8.2.3 all-zero)"))
                        self.bitq = []
                        self._goto(JMSTATE)
                        break
            else:
                n = v8.count_cj(self.rxbits[-400:] if len(self.rxbits) > 400 else self.rxbits)
                if n > self.cj_count:
                    self.cj_count = n
                if self.cj_count >= 3:
                    self._ev("CJ received (3 all-zero octets) - negotiation complete")
                    self._goto(GAP)

        if self.state == SILENCE:
            if self.t_state >= self.lead:
                self._ev("emitting ANSam at %.0f Hz" % self.carrier)
                self._goto(ANSAM)
        elif self.state == ANSAM:
            if self.t_state >= self.ansam_max:
                self._ev("no CM within %.1f s" % self.t_state)
                self._goto(DONE)
        elif self.state == JMSTATE:
            if self.t_state >= 1.0:
                self._goto(CJWAIT, "(JM continues)")
        elif self.state == CJWAIT:
            if self.t_state >= self.jm_max:
                self._ev("no CJ within %.1f s of JM" % (self.t_state + 1.0))
                self._goto(DONE)
        elif self.state == GAP:
            if self.t_state >= 0.075:
                self._ev("75 ms gap done")
                self._goto(DONE)

        self._fill(FRAME)
        out = self.outq[:FRAME]
        self.outq = self.outq[FRAME:]
        if len(out) < FRAME:
            out = out + [0] * (FRAME - len(out))
        return out


# ---------------------------------------------------------------------------
# V.32/V.32bis answer-side start-up, the non-V.8 path (V.32 A.2.2 -> 5.4.2)
#
#   SILENCE -> ANS      transmit the plain V.25 answer tone, listen for AA
#   AC                  on AA, transmit alternating states A and C
#   CA                  after >=128 symbols of AC and 1800 Hz seen for 64
#                       symbol periods, switch to CA and watch for an incoming
#                       phase reversal
#   AC2                 on that reversal, revert to AC (64 +/- 2 symbols later)
#   OBSERVE             log whatever the caller does next (amplitude drop,
#                       then its receiver-conditioning signal)
#
# Deliberately stops before TRN/R1: the data phase is not implemented. The
# ceiling on this path is 14.4 kbit/s, since V.8 8.1.1 sends a caller that
# heard plain ANS to Annex A/V.32 bis rather than into a CM/JM exchange.
# ---------------------------------------------------------------------------

ANS, ACST, CAST, AC2, OBSERVE = "ANS", "AC", "CA", "AC2", "OBSERVE"

class AnswerV32:
    def __init__(self, level_dbfs=-24.0, lead=0.25, ans_s=3.3,
                 aa_purity=0.30, aa_frames=4, log=None):
        import ansam as _ansam
        import v32 as _v32
        self.v32 = _v32
        self.level = level_dbfs
        self.lead = lead
        self.ans_buf = _ansam.ans_samples(ans_s + 20.0, level_dbfs=level_dbfs)
        self.ans_pos = 0
        self.aa_purity = aa_purity
        self.aa_frames = aa_frames
        self.state = SILENCE
        self.t = 0.0
        self.t_state = 0.0
        self.outq = []
        self.events = [] if log is None else log
        self.aa_run = 0
        self.aa_seen_s = 0.0
        self.ac_symbols = 0
        self.prev_phase = None
        self.rev_detected = False
        self.rev_delay = 0
        self.spectrum = []          # (t, dom Hz, purity, rms) for the log
        self._ph = 0.0              # continuous carrier phase for AC/CA

    def _ev(self, msg):
        self.events.append((round(self.t, 3), self.state, msg))

    def _goto(self, s, msg=""):
        self._ev("-> %s %s" % (s, msg))
        self.state = s
        self.t_state = 0.0

    # ---- inbound analysis: 1800 Hz presence and phase, per frame ----
    def _look(self, x):
        """1800 Hz phase, purity and level for one frame.

        A 160-sample frame is exactly 36 cycles of 1800 Hz at 8 kHz, so the
        phase measured frame to frame needs no de-trending -- consecutive
        readings are directly comparable and a 180 degree step shows up as one.
        (Measuring over any other window length introduces a constant apparent
        jump from the carrier advancing between windows.)
        """
        if not x:
            return None, 0.0, 0.0
        ms = dsp.mean_square(x)
        rms = math.sqrt(ms)
        if ms < 200:
            return None, 0.0, rms
        w = 2 * math.pi * 1800.0 / SR
        re = im = 0.0
        for k, v in enumerate(x):
            re += v * math.cos(w * k)
            im -= v * math.sin(w * k)
        mag = math.sqrt(re * re + im * im) * 2.0 / len(x)
        pur = (mag * mag / 2.0) / ms
        ph = math.atan2(im, re)
        return ph, pur, rms

    def _emit_states(self, pattern, n):
        """Append n samples of a repeating carrier-state pattern."""
        amp = 32768.0 * (10 ** (self.level / 20.0)) * math.sqrt(2.0)
        sps = SR / self.v32.BAUD
        dph = 2 * math.pi * self.v32.CARRIER / SR
        for _ in range(n):
            k = int(self.ac_symbols)
            st = pattern[k % len(pattern)]
            self.outq.append(int(amp * math.sin(self._ph + self.v32.STATE_PHASE[st])))
            self._ph += dph
            if self._ph > 2 * math.pi:
                self._ph -= 2 * math.pi
            self.ac_symbols += 1.0 / sps

    def _fill(self, n):
        while len(self.outq) < n:
            if self.state == ANS:
                take = self.ans_buf[self.ans_pos:self.ans_pos + n]
                self.ans_pos += len(take)
                self.outq.extend(take if take else [0] * n)
            elif self.state in (ACST, AC2):
                self._emit_states("AC", n - len(self.outq))
            elif self.state in (CAST, "CADELAY"):
                self._emit_states("CA", n - len(self.outq))
            else:
                self.outq.extend([0] * (n - len(self.outq)))

    def step(self, inbound):
        dt = FRAME / float(SR)
        self.t += dt
        self.t_state += dt
        ph, pur, rms = self._look(inbound)

        if pur > 0.20:
            self.spectrum.append((round(self.t, 2), 1800, round(pur, 3), round(rms)))

        if self.state == SILENCE:
            if self.t_state >= self.lead:
                self._ev("transmitting plain ANS (2100 Hz, no AM)")
                self._goto(ANS)
        elif self.state == ANS:
            if pur > self.aa_purity:
                self.aa_run += 1
                self.aa_seen_s += dt
                if self.aa_run >= self.aa_frames:
                    self._ev("AA detected (1800 Hz, purity %.2f, %.0f dBFS) - "
                             "caller took the Annex A/V.32bis branch"
                             % (pur, dsp.dbfs(rms)))
                    self.ac_symbols = 0.0
                    self._goto(ACST, "(transmitting alternating A/C)")
            else:
                self.aa_run = 0
        elif self.state == ACST:
            # V.32 5.4.2: >=128 symbol intervals of AC and 1800 Hz seen for 64
            # symbol periods before switching to CA
            if self.ac_symbols >= 128 and self.aa_seen_s >= self.v32.symbols(64):
                self._ev("128 symbols of AC sent and 1800 Hz held - switching to CA")
                self.prev_phase = ph
                self._goto(CAST)
        elif self.state == CAST:
            if ph is not None and self.prev_phase is not None:
                d = abs(((ph - self.prev_phase + math.pi) % (2 * math.pi)) - math.pi)
                if d > 2.0:
                    # V.32 5.4.2: the CA->AC transition must appear at the line
                    # 64 +/- 2 symbol periods after the reversal is received.
                    self._ev("phase reversal in incoming tone (%.2f rad) - caller "
                             "switched AA->CC; reverting to AC after 64 symbols" % d)
                    self.rev_detected = True
                    self.rev_delay = int(self.v32.symbols(64) * SR)
                    self._goto("CADELAY")
                else:
                    self.prev_phase = ph
            if self.state == CAST and self.t_state > 6.0:
                self._ev("no incoming phase reversal within 4 s")
                self._goto(OBSERVE)
        elif self.state == "CADELAY":
            self.rev_delay -= FRAME
            if self.rev_delay <= 0:
                self._goto(AC2, "(64-symbol delay elapsed)")
        elif self.state == AC2:
            if self.t_state > 6.0:
                self._ev("caller did not cease transmitting - it is waiting for the "
                         "S sequence, which is not implemented")
                self._goto(OBSERVE)

        self._fill(FRAME)
        out = self.outq[:FRAME]
        self.outq = self.outq[FRAME:]
        if len(out) < FRAME:
            out = out + [0] * (FRAME - len(out))
        return out


# ---------------------------------------------------------------------------
# V.22bis answer-side handshake, closed loop (V.22bis §6.3.1.1.2)
#
#   SILENCE -> ANS      V.25 answer tone
#   USB1                unscrambled binary 1 at 1200 bit/s in the high channel;
#                       the caller needs 155 +/- 10 ms of it, then goes quiet for
#                       456 +/- 10 ms and sends its own S1 for 100 +/- 3 ms
#   POST                triggered by the *end* of the caller's S1, which is what
#                       turns on our circuit 112. From there the schedule is
#                       fixed by the spec: our S1 for 100 ms, SB1 at 1200 bit/s
#                       until 600 ms after 112, then scrambled binary 1 at
#                       2400 bit/s, then data.
#
# Detection is spectral rather than by demodulation, because the three handshake
# signals have distinct line spectra in the low channel and that is far cheaper
# and more robust than running a demodulator during acquisition:
#
#   USB1  dibits 11 -> +270 deg/symbol -> a steady -150 Hz shift -> line at 1050 Hz
#   S1    alternating 00,11 -> phase toggles 0/+90 deg at 300 Hz -> carrier line
#         at 1200 Hz with sidebands at 900 and 1500 Hz
#   SB1   scrambled -> broadband, no lines
#
# Measured on a real caller: E1200 = 0.55, E900 = 0.20, E1500 = 0.23 during S1,
# against < 0.04 for everything else.
# ---------------------------------------------------------------------------

import v22 as _v22mod
_V22_LOW, _V22_HIGH = _v22mod.LOW, _v22mod.HIGH

USB1ST, POST, DATA = "USB1", "POST", "DATA"
WAITUSB1, GAP, S1TX, SB1TX, TRAIN, FAILED = ("WAITUSB1", "GAP", "S1TX",
                                            "SB1TX", "TRAIN", "FAILED")
D1200 = "DATA1200"          # 6.3.1.2, the V.22-compatible 1200 bit/s data phase

# Band-presence detection, for "scrambled binary 1 at 1200 bit/s" -- the signal
# that selects the 1200 bit/s handshake (6.3.1.1.1 c, 6.3.1.1.2 b). Unlike USB1
# and S1 it has no tone to look for: it is just a modulated carrier. So the test
# is that the far channel carries more than the near one does, which works while
# we are transmitting because the two channels are separated in frequency.
#
# Measured with our own transmission echoed back at -12 dB, far/near band power:
#   far SB1 16.2   far S1 56.1   far USB1 56.1   far ANS 752   far silent 0.007
# so a threshold of 1.0 sits a factor of 16 below the lowest positive case and a
# factor of 140 above the negative one.
_LOW_BAND = (750.0, 900.0, 1050.0, 1200.0, 1350.0, 1500.0, 1650.0)
_HIGH_BAND = (1950.0, 2100.0, 2250.0, 2400.0, 2550.0, 2700.0, 2850.0)


def _band_ratio(x, far, near):
    if not x:
        return 0.0
    a = sum(dsp.goertzel(x, f) for f in far)
    b = sum(dsp.goertzel(x, f) for f in near)
    return a / (b + 1e-9)

def _async_bits(data, idle=2):
    """8N1 framing: start bit 0, eight data bits LSB first, stop bit 1, plus
    `idle` extra mark bits between characters.

    The idle bits matter. Back-to-back characters at exactly the line rate give
    the far end no mark period to reference, so a single channel bit error makes
    its receiver hunt for a start bit and re-lock at the wrong phase -- after
    which stop bits still land on ones often enough that it never hunts again,
    and every character is wrong from then on. Padding each character with a
    couple of mark bits is what a real DTE running below the line rate produces,
    and it gives the receiver a clean reference to re-lock against. It costs
    throughput: 12 bits per character instead of 10, so 200 char/s rather than
    240 at 2400 bit/s.
    """
    out = []
    for ch in data:
        b = ch if isinstance(ch, int) else ord(ch)
        out.append(0)
        for k in range(8):
            out.append((b >> k) & 1)
        out.append(1)
        out.extend([1] * idle)
    return out


class AnswerV22bis:
    # The answering modem transmits in the high channel and receives the low one
    # (6.1.1). Naming these the same way on both classes is what lets one call
    # runner drive either role without asking which it has.
    rx_carrier = _V22_LOW
    role = "answer"
    # 6.3.1.2.2: the 1200 bit/s fallback, for a V.22 caller that never sends S1
    F_SB1 = 14          # 270 +/- 40 ms of scrambled binary 1 in the low channel
    F_SB1_DATA = 38     # after 765 +/- 10 ms of our own SB1, ready both ways

    def __init__(self, level_dbfs=-18.0, lead=0.25, ans_s=2.2,
                 payload=b"SLOPMODEM", guard_tone=False, log=None, data_s=30.0,
                 idle=2, tx_source=None):
        import ansam as _ansam
        import v22 as _v22
        import v22bis as _v22bis
        self.v22 = _v22
        self.level = level_dbfs
        self.lead = lead
        self.state = SILENCE
        self.t = 0.0
        self.t_state = 0.0
        self.events = [] if log is None else log

        # ANS, then a long USB1 run. The ANS run is snapped to a whole number of
        # RTP frames so that the USB1 run starts on a frame boundary -- the join
        # arithmetic below depends on it.
        self.ans_n = int(round(ans_s * SR / FRAME)) * FRAME
        self.ans_s = self.ans_n / float(SR)
        self.pre = list(_ansam.ans_samples(self.ans_s, level_dbfs=level_dbfs))[:self.ans_n]
        m1 = _v22bis.Mod("high", level_dbfs=level_dbfs, guard_tone=guard_tone)
        self.pre += m1.modulate(_v22.usb1_bits(int(6.0 * 1200)),
                                scramble=False, bps=2)
        self.pre_pos = 0

        # Everything after the caller's S1, rendered by ONE modulator as a single
        # continuous stream: our S1 (100 ms), SB1 at 1200 to fill out the 600 ms
        # from circuit 112 (§6.3.1.1.2 c), then scrambled binary 1 at 2400 for
        # 400 ms (§6.3.1.1.2 d needs 200 ms), then the payload at 2400.
        #
        # The 1200 -> 2400 change is only a change of bits per symbol; the baud
        # rate, the carrier and the constellation lattice are the same, so a
        # single v22bis.Mod renders the whole thing with no discontinuity.
        #
        # Joining `post` onto a `pre` that gets cut short needs the two streams
        # to agree on carrier phase, symbol phase and differential quadrant at
        # the cut. They do, because the numbers line up exactly:
        #   160 samples (one RTP frame) = 12 symbols at 600 baud
        #                              = 48 cycles of the 2400 Hz carrier
        #                              = 12 USB1 dibits, a multiple of the
        #                                4-symbol quadrant cycle
        # so any frame boundary is also a symbol boundary, a carrier zero-phase
        # point, and a quadrant-cycle boundary. Priming this modulator with 12
        # USB1 symbols and discarding those 160 samples leaves it on that same
        # grid with its pulse-shaper carry filled, so the first real sample of
        # `post` continues `pre` seamlessly wherever `pre` was cut.
        m = _v22bis.Mod("high", level_dbfs=level_dbfs, guard_tone=guard_tone)
        prime = m.modulate([1] * 24, scramble=False, bps=2)
        assert len(prime) == FRAME, len(prime)       # discarded
        post = m.modulate(_v22.s1_bits(int(0.100 * 1200)), scramble=False, bps=2)
        post += m.modulate([1] * int(0.500 * 1200), scramble=True, bps=2)
        post += m.modulate([1] * int(0.400 * 2400), scramble=True)
        # enough repeats to hold the carrier up for data_s seconds: 2400 bit/s
        # with 10 bits per character is 240 characters per second
        # 12 bits per character now, so 200 characters per second
        # Two ways to fill the data phase.
        #
        # tx_source is a callable(nbits) -> bits: the streaming path, where the
        # bits come from whatever the DTE has queued and the modulator is driven
        # one frame at a time. That is what a modem does, and it is the only way
        # the transmitted data can depend on anything that happens during the
        # call. `shape()` is stateful and continuous, so carrying on with the
        # same modulator after the pre-rendered training joins seamlessly -- the
        # rate change from 1200 to 2400 was already only a change of bits per
        # symbol.
        #
        # Without one, the old behaviour: a fixed payload repeated for data_s
        # seconds, rendered up front. Kept because every earlier measurement in
        # testrig/v22-modem.md was made with it.
        self.tx_source = tx_source
        self.mod = m
        if tx_source is None:
            cps = 2400.0 / (10 + idle)
            reps = max(1, int(data_s * cps / max(len(payload), 1)))
            body = _async_bits(payload, idle=idle) * reps
            post += m.modulate(body, scramble=True)
        self.post = post
        self.post_pos = 0
        self.data_bits = 0

        self.s1_run = 0
        self.s1_seen = False
        self.s1_gap = 0
        self.usb1_s = 0.0
        self.sb1_run = 0
        self.f_in_state = 0
        self.c112 = True            # 2400 bit/s until something says otherwise
        self.c109 = False
        # The 1200 bit/s fallback generates its stream on demand, like the
        # 2400 bit/s data phase does, so it needs a bit source either way.
        self.tx12 = tx_source or self._fixed12(payload, idle)

    def _ev(self, msg):
        self.events.append((round(self.t, 3), self.state, msg))

    def _goto(self, s, msg=""):
        self._ev("-> %s %s" % (s, msg))
        self.state = s
        self.t_state = 0.0
        self.f_in_state = 0

    def _look(self, x):
        """Return (is_S1, rms). Spectral test on the low channel."""
        if not x:
            return False, 0.0
        ms = dsp.mean_square(x)
        if ms < 200:
            return False, math.sqrt(ms)
        e12 = dsp.goertzel(x, 1200.0) / ms
        e9 = dsp.goertzel(x, 900.0) / ms
        e15 = dsp.goertzel(x, 1500.0) / ms
        return (e12 > 0.20 and (e9 + e15) > 0.12), math.sqrt(ms)

    def _fill(self, n):
        while len(self.outq) < n:
            if self.state in (ANS, USB1ST):
                take = self.pre[self.pre_pos:self.pre_pos + n]
                self.pre_pos += len(take)
                self.outq.extend(take if take else [0] * n)
            elif self.state == POST:
                take = self.post[self.post_pos:self.post_pos + n]
                self.post_pos += len(take)
                self.outq.extend(take if take else [0] * n)
            elif self.state == DATA:
                # 20 ms at 2400 bit/s is 48 bits, which is 12 symbols at 600
                # baud, which is exactly 160 samples. The modulator returns
                # whatever the fractional symbol grid gives it and outq carries
                # the remainder, so the arithmetic does not have to be exact.
                bits = self.tx_source(48)
                self.outq.extend(self.mod.modulate(bits, scramble=True))
                self.data_bits += 48
            elif self.state == SB1TX:
                # 6.3.1.2.2 b): scrambled binary 1 at 1200 bit/s. Generated from
                # the same modulator that rendered `post`, which is still on the
                # frame grid -- prime (12 symbols) plus post (600 symbols) is 612,
                # a multiple of 12 -- so this joins the truncated USB1 run with no
                # phase step, exactly as the 2400 bit/s path does.
                self.outq.extend(self.mod.modulate([1] * 24, scramble=True,
                                                   bps=2))
            elif self.state == D1200:
                self.outq.extend(self.mod.modulate(self.tx12(24), scramble=True,
                                                   bps=2))
                self.data_bits += 24
            else:
                self.outq.extend([0] * (n - len(self.outq)))

    outq = None

    @staticmethod
    def _fixed12(payload, idle):
        pattern = _async_bits(payload, idle=idle)
        box = {"i": 0}

        def take(n):
            out = []
            i = box["i"]
            for _ in range(n):
                out.append(pattern[i % len(pattern)])
                i += 1
            box["i"] = i
            return out
        return take

    @property
    def rx_open(self):
        """From POST onward on the 2400 bit/s path: before that the caller is
        sending S1 and SB1 at 1200, where there is no 16-QAM to acquire. On the
        1200 bit/s path, from the moment the fallback is taken."""
        return self.state in (POST, DATA, SB1TX, D1200)

    @property
    def line_rate(self):
        return 1200 if self.c109 or self.state in (SB1TX, D1200) else 2400

    @property
    def rx_mode(self):
        import tracking
        return (tracking.QPSK1200 if self.state in (SB1TX, D1200)
                else tracking.QAM2400)

    def step(self, inbound):
        if self.outq is None:
            self.outq = []
        dt = FRAME / float(SR)
        self.t += dt
        self.t_state += dt
        self.f_in_state += 1
        is_s1, rms = self._look(inbound)

        if self.state == SILENCE:
            if self.t_state >= self.lead:
                self._ev("ANS then USB1 (unscrambled binary 1, high channel)")
                self._goto(ANS)
        elif self.state == ANS:
            # the pre-rendered stream carries ANS then USB1; note the crossover
            if self.pre_pos >= self.ans_n:
                self._goto(USB1ST, "(USB1 on air)")
        elif self.state == USB1ST:
            self.usb1_s += dt
            if is_s1:
                self.s1_run += 1
                self.s1_gap = 0
                if not self.s1_seen and self.s1_run >= 2:
                    self.s1_seen = True
                    self._ev("caller's S1 detected (%.0f dBFS) after %.2f s of USB1"
                             % (dsp.dbfs(rms), self.usb1_s))
            elif self.s1_seen:
                self.s1_gap += 1
                if self.s1_gap >= 2:
                    self._ev("caller's S1 ended - circuit 112 on; our S1, then "
                             "SB1 at 1200, then scrambled ones at 2400")
                    self._goto(POST)
            else:
                # 6.3.1.1.2 b): scrambled binary 1 or 0 in the low channel at
                # 1200 bit/s for 270 +/- 40 ms, with no S1, means a V.22 caller,
                # and the handshake continues per 6.3.1.2.2 b) and c).
                if _band_ratio(inbound, _LOW_BAND, _HIGH_BAND) > 1.0:
                    self.sb1_run += 1
                    if self.sb1_run >= self.F_SB1:
                        self.c112 = False
                        self._ev("%d ms of scrambled binary 1 at 1200 and no S1 "
                                 "- 1200 bit/s fallback (6.3.1.2.2 b): circuit "
                                 "112 off, SB1 at 1200 for %d ms"
                                 % (self.sb1_run * 20, self.F_SB1_DATA * 20))
                        self._goto(SB1TX)
                else:
                    self.sb1_run = 0
        elif self.state == SB1TX:
            if self.f_in_state >= self.F_SB1_DATA:
                self.c109 = True
                self._ev("765 ms of scrambled binary 1 sent - circuit 106 and "
                         "109 on, ready both ways at 1200 bit/s (6.3.1.2.2 c)")
                self._goto(D1200)
        elif self.state == POST:
            if self.post_pos >= len(self.post):
                if self.tx_source is not None:
                    # 6.3.1.1.2 d): after scrambled binary 1 at 2400 bit/s for
                    # 200 ms, circuit 106 is conditioned to respond and the
                    # modem is ready to transmit data.
                    self._ev("training done - ready to transmit, streaming "
                             "from the DTE (circuit 106 on)")
                    self._goto(DATA)
                else:
                    self._ev("post-handshake stream exhausted")
                    self._goto(DONE)

        self._fill(FRAME)
        out = self.outq[:FRAME]
        self.outq = self.outq[FRAME:]
        if len(out) < FRAME:
            out = out + [0] * (FRAME - len(out))
        return out


# ---------------------------------------------------------------------------
# The calling side of V.22bis, 6.3.1.1.1
# ---------------------------------------------------------------------------

class OriginateV22bis:
    """Calling modem. The mirror of AnswerV22bis, and the patient one.

    Channel assignment swaps: 6.1.1 puts the calling modem's transmitter in the
    low channel and its receiver in the high channel. The guard tone is the
    answering modem's job (V.22 2.4), so we do not send one.

    Every step is timed from something detected rather than from when the call
    started, which is what makes this side awkward: the answerer sends its answer
    tone for as long as it likes -- 2.2 s from our own answerer, closer to 5 s
    from the hardware, since V.8 has it wait out Te before giving up on CM -- and
    only then unscrambled binary 1. So the receiver has to sit through an unknown
    silence and pick USB1 out of it.

    The detectors are calibrated, not assumed. Measured over 160-sample frames on
    the high channel, normalised by frame power:

      signal   e2250   e2400   e2100   e2700
      USB1     0.937   0.000   0.000   0.000
      S1       0.000   0.505   0.247   0.247
      SB1      0.036   0.109   0.018   0.010
      data     0.083   0.106   0.052   0.039
      ANS      0.000   0.000   1.000   0.000

    USB1 is unscrambled binary 1 at 1200 bit/s, so every dibit is 11, which
    Table 1 makes a 270 degree quadrant change -- i.e. -90 degrees per symbol.
    At 600 baud that is -150 Hz, putting the tone at 2400 - 150 = 2250 Hz, not
    at 1950 as a first pass through the arithmetic suggested. That 2250 Hz line
    is the one recorded as unidentified in sip-audio-project notes early on.

    S1 alternates 00 and 11, so the phasor alternates between two points 90
    degrees apart at 300 Hz: carrier plus sidebands at 2400 +/- 300. The
    thresholds are the same ones AnswerV22bis uses on the low channel.
    """

    rx_carrier = _V22_HIGH
    role = "originate"

    # 6.3.1.1.1, in 20 ms frames
    F_USB1 = 8          # 155 +/- 10 ms of unscrambled binary 1 detected
    F_GAP = 23          # then silent for a further 456 +/- 10 ms
    F_S1 = 5            # then S1 for 100 +/- 3 ms
    F_112_2400 = 30     # 600 +/- 10 ms after circuit 112, start 2400 bit/s
    F_TRAIN = 10        # after 200 +/- 10 ms of that, circuit 106 on (e)
    # ...but being *ready* to transmit is not the same as having something to
    # transmit, and nothing in 6.3.1.1.1 requires data to start the instant 106
    # comes on. The answering side of this codebase holds scrambled binary 1 at
    # 2400 for 400 ms, and both hardware modems lock onto that reliably. With
    # only the 200 ms minimum, the Conexant as answerer delivered about half a
    # second of garbage to its DTE before its receiver converged. So keep
    # transmitting scrambled ones for as long as the answerer does.
    F_TRAIN_DATA = 20   # 400 ms of scrambled binary 1 at 2400 before data
    # 6.3.1.2.1: the 1200 bit/s fallback, for a V.22 answerer that never sends S1
    F_SB1 = 14          # 270 +/- 40 ms of scrambled binary 1 in the high channel
    F_109_DATA = 38     # 765 +/- 10 ms after circuit 109, ready to transmit

    def __init__(self, level_dbfs=-18.0, tx_source=None, payload=b"SLOPMODEM ",
                 idle=2, log=None, usb1_timeout=25.0, s1_timeout=8.0,
                 v22_only=False):
        import v22 as _v22
        import v22bis as _v22bis
        self.v22 = _v22
        self.level = level_dbfs
        self.mod = _v22bis.Mod("low", level_dbfs=level_dbfs, guard_tone=False)
        self.events = [] if log is None else log
        self.state = WAITUSB1
        self.t = 0.0
        self.t_state = 0.0
        self.usb1_timeout = usb1_timeout
        self.s1_timeout = s1_timeout
        # 6.3.1.2.1 b) is 6.3.1.1.1 b) without the S1: a modem that only does
        # 1200 bit/s goes straight from the 456 ms gap to scrambled binary 1.
        # Useful for its own sake, and it is how the fallback gets tested against
        # our own answerer without a second implementation.
        self.v22_only = v22_only

        self.outq = []
        self.usb1_run = 0
        self.usb1_seen_s = 0.0
        self.s1_run = 0
        self.s1_seen = False
        self.s1_gap = 0
        self.c112 = False           # circuit 112: 2400 bit/s selected
        self.c109 = False           # circuit 109: 1200 bit/s fallback taken
        self.f_since_112 = 0
        self.f_since_109 = 0
        self.sb1_run = 0
        self.f_in_state = 0
        self.data_bits = 0
        self.tx_source = tx_source or self._fixed(payload, idle)

    # -- transmit source -------------------------------------------------

    @staticmethod
    def _fixed(payload, idle):
        """Fallback for tests: the payload, framed and repeated for ever."""
        pattern = _async_bits(payload, idle=idle)
        box = {"i": 0}

        def take(n):
            out = []
            i = box["i"]
            for _ in range(n):
                out.append(pattern[i % len(pattern)])
                i += 1
            box["i"] = i
            return out
        return take

    # -- detectors -------------------------------------------------------

    def _look(self, x):
        """(is_usb1, is_s1, rms) on the high channel."""
        if not x:
            return False, False, 0.0
        ms = dsp.mean_square(x)
        if ms < 200:
            return False, False, math.sqrt(ms)
        e2250 = dsp.goertzel(x, 2250.0) / ms
        e2400 = dsp.goertzel(x, 2400.0) / ms
        e2100 = dsp.goertzel(x, 2100.0) / ms
        e2700 = dsp.goertzel(x, 2700.0) / ms
        usb1 = e2250 > 0.50 and e2100 < 0.30
        s1 = e2400 > 0.20 and (e2100 + e2700) > 0.12
        return usb1, s1, math.sqrt(ms)

    # -- bookkeeping -----------------------------------------------------

    def _ev(self, msg):
        self.events.append((round(self.t, 3), self.state, msg))

    def _goto(self, s, msg=""):
        self._ev("-> %s %s" % (s, msg))
        self.state = s
        self.t_state = 0.0
        self.f_in_state = 0

    @property
    def rx_open(self):
        """Whether the receiver should be listening yet.

        From circuit 112 onward on the 2400 bit/s path. That is 420 ms before the
        answerer switches to 2400, which is deliberate: the streaming receiver
        spends its first 600 half-symbols measuring an input gain, and starting it
        early means that prologue finishes just as the 16-QAM arrives. On the
        1200 bit/s path it is circuit 109, which leaves 765 ms.
        """
        return self.c112 or self.c109

    @property
    def line_rate(self):
        return 1200 if self.c109 else 2400

    @property
    def rx_mode(self):
        import tracking
        return tracking.QPSK1200 if self.c109 else tracking.QAM2400

    # -- transmitter -----------------------------------------------------

    def _fill(self, n):
        while len(self.outq) < n:
            if self.state == S1TX:
                # 1200 bit/s: 24 bits per 20 ms frame = 12 symbols = 160 samples
                self.outq.extend(self.mod.modulate(
                    self.v22.s1_bits(24), scramble=False, bps=2))
            elif self.state == SB1TX:
                self.outq.extend(self.mod.modulate([1] * 24, scramble=True, bps=2))
            elif self.state == TRAIN:
                self.outq.extend(self.mod.modulate([1] * 48, scramble=True))
            elif self.state == DATA:
                self.outq.extend(self.mod.modulate(self.tx_source(48),
                                                   scramble=True))
                self.data_bits += 48
            elif self.state == D1200:
                # 1200 bit/s is 24 bits per 20 ms frame, still 12 symbols
                self.outq.extend(self.mod.modulate(self.tx_source(24),
                                                   scramble=True, bps=2))
                self.data_bits += 24
            else:
                # 6.3.1.1.1 a) and b): silent until the answerer has been heard,
                # and silent again for the 456 ms gap. Nothing touches the
                # modulator here, so its carrier phase and pulse-shaper state are
                # still pristine when S1 starts.
                self.outq.extend([0] * (n - len(self.outq)))

    # -- one frame -------------------------------------------------------

    def step(self, inbound):
        dt = FRAME / float(SR)
        self.t += dt
        self.t_state += dt
        self.f_in_state += 1
        usb1, s1, rms = self._look(inbound)
        if self.c112:
            self.f_since_112 += 1
        if self.c109:
            self.f_since_109 += 1

        # 6.3.1.1.1 c): scrambled binary 1 in the high channel at 1200 bit/s for
        # 270 +/- 40 ms, with no S1, means the answerer is a V.22 modem (or is
        # set to 1200), and the handshake continues per 6.3.1.2.1 c) and d).
        if self.state == SB1TX and not self.c112 and not self.c109 \
                and not self.s1_seen:
            if (not s1 and not usb1
                    and _band_ratio(inbound, _HIGH_BAND, _LOW_BAND) > 1.0):
                self.sb1_run += 1
                if self.sb1_run >= self.F_SB1:
                    self.c109 = True
                    self.f_since_109 = 0
                    self._ev("%d ms of scrambled binary 1 at 1200 and no S1 - "
                             "1200 bit/s fallback (6.3.1.2.1 c): circuit 109 on, "
                             "112 off; data in %d ms"
                             % (self.sb1_run * 20, self.F_109_DATA * 20))
            else:
                self.sb1_run = 0

        # Circuit 112 comes from the *end* of the answerer's S1 (6.3.1.1.1 c),
        # and it can arrive in any of the transmitting states.
        if not self.c112 and self.state in (S1TX, SB1TX):
            if s1:
                self.s1_run += 1
                self.s1_gap = 0
                if not self.s1_seen and self.s1_run >= 2:
                    self.s1_seen = True
                    self._ev("answerer's S1 detected (%.0f dBFS)" % dsp.dbfs(rms))
            elif self.s1_seen:
                self.s1_gap += 1
                if self.s1_gap >= 2:
                    self.c112 = True
                    self.f_since_112 = 0
                    self._ev("answerer's S1 ended - circuit 112 on; 2400 bit/s "
                             "in %d ms" % int(self.F_112_2400 * 20))

        if self.state == WAITUSB1:
            if usb1:
                self.usb1_run += 1
                self.usb1_seen_s += dt
                if self.usb1_run >= self.F_USB1:
                    self._ev("%d ms of unscrambled binary 1 detected (%.0f dBFS)"
                             % (self.usb1_run * 20, dsp.dbfs(rms)))
                    self._goto(GAP, "(456 ms silent, then S1)")
            else:
                self.usb1_run = 0
                if self.t_state > self.usb1_timeout:
                    self._ev("no USB1 within %.0f s - giving up"
                             % self.usb1_timeout)
                    self._goto(FAILED)
        elif self.state == GAP:
            if self.f_in_state >= self.F_GAP:
                if self.v22_only:
                    self._goto(SB1TX, "(scrambled binary 1 at 1200, no S1 - "
                                      "6.3.1.2.1 b)")
                else:
                    self._goto(S1TX, "(unscrambled double dibit 00/11, 100 ms)")
        elif self.state == S1TX:
            if self.f_in_state >= self.F_S1:
                self._goto(SB1TX, "(scrambled binary 1 at 1200)")
        elif self.state == SB1TX:
            if self.c112 and self.f_since_112 >= self.F_112_2400:
                self._goto(TRAIN, "(scrambled binary 1 at 2400)")
            elif self.c109 and self.f_since_109 >= self.F_109_DATA:
                self._ev("765 ms since circuit 109 - circuit 106 on, ready to "
                         "transmit data at 1200 bit/s (6.3.1.2.1 d)")
                self._goto(D1200)
            elif not self.c112 and not self.c109 and self.t_state > self.s1_timeout:
                self._ev("neither S1 nor scrambled binary 1 from the answerer "
                         "within %.0f s - giving up" % self.s1_timeout)
                self._goto(FAILED)
        elif self.state == TRAIN:
            if self.f_in_state == self.F_TRAIN:
                self._ev("200 ms of scrambled binary 1 at 2400 - circuit 106 on")
            if self.f_in_state >= self.F_TRAIN_DATA:
                self._ev("%d ms of training sent - starting data"
                         % (self.F_TRAIN_DATA * 20))
                self._goto(DATA)

        self._fill(FRAME)
        out = self.outq[:FRAME]
        self.outq = self.outq[FRAME:]
        if len(out) < FRAME:
            out = out + [0] * (FRAME - len(out))
        return out
