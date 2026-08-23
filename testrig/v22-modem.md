# V.22 1200 bit/s modem — implementation status

Part of the ANS path (see `v32-ans-path.md`), which caps the link at 14.4 kbit/s but needs no
signalling this rig destroys. V.22 is the cheapest of the automode modes to build: 600 baud with
differential phase encoding, no adaptive equaliser, no echo canceller.

## What is implemented and tested

`softmodem/v22.py` — the V.22 1200 bit/s modem core. Parameters taken from ITU-T V.22 (11/1988):

| Item | Spec | Value |
|---|---|---|
| Carriers | §2.1 | low channel 1200 ± 0.5 Hz, high channel 2400 ± 1 Hz |
| Guard tone | §2.1, §2.2 | 1800 ± 20 Hz, high channel only, 6 ± 1 dB below data power, disableable |
| Symbol rate | §2.5.1 | 600 baud — 13⅓ samples per symbol at 8 kHz |
| Pulse shaping | §2.4 | square root of raised cosine, 75 % roll-off |
| Encoding | §2.5.2, Table 1 | dibit → phase change: `00` +90°, `01` 0°, `11` +270°, `10` +180° |
| Scrambler | §5.1 | 1 ⊕ x⁻¹⁴ ⊕ x⁻¹⁷, inverting the next input after 64 consecutive ones out |
| Offset tolerance | V.22bis §2.6 | ±7 Hz |

The answer side transmits in the high channel and receives in the low channel; the call side does
the reverse.

Differential encoding is what makes this tractable without an equaliser: the decision needs only the
phase *change* between adjacent symbols, so no absolute carrier phase reference is required, and a
7 Hz offset rotates the constellation by just 4° per symbol against 90° decision spacing.

Receiver chain: complex downconversion at the channel carrier, SRRC matched filter, then symbol
timing from a Gardner detector driving a proportional-integral loop. Gardner suits differential
detection because its error term — mid-sample × (current − previous) — is independent of
constellation rotation.

## Test results

`test_v22.py`, 22 checks, no hardware:

| Condition | Steady-state BER |
|---|---|
| high channel, −12 / −24 / −36 dBFS | **0** |
| low channel, −12 / −24 / −36 dBFS | **0** |
| 30 / 20 / 15 dB SNR | **0** |
| with the 1800 Hz guard tone | **0** |
| 3 Hz and 7 Hz carrier offset | **0** |

Steady-state means after the timing loop acquires. Raw BER including acquisition is 0.0042, and the
errors are confined to bit positions 0–17 — about nine symbols — with zero errors in the remaining
1182 bits. A real modem trains before carrying data, so the steady-state figure is the meaningful
one; the test reports both. Constellation magnitude spread is 1.2 % of the mean, as expected for a
single-amplitude constellation.

Two bugs found while building the tests, both worth remembering:

- **The frequency-offset test was wrong before it was right.** Multiplying a passband signal by a
  cosine is DSB modulation, not a frequency shift — it creates two copies at fc ± Δf and destroys the
  signal, which showed up as a spurious BER of 0.46 at 3 Hz. A frequency offset means moving the
  transmit carrier, not multiplying the output.
- **The first timing loop was not a controller.** It nudged the sampling instant by a fraction of the
  error with no integral term, and fell apart at 15 dB SNR. Replaced with a proper PI loop.

## Closed-loop handshake and the data connection

`fsm.AnswerV22bis` implements the answering side of §6.3.1.1.2 with real detection rather than a
fixed schedule. Detection is spectral, not by demodulation, because the three handshake signals have
distinct line spectra and that is far cheaper and more robust during acquisition:

| Signal | Line spectrum in the low channel | Measured on the real caller |
|---|---|---|
| `USB1` | dibits `11` → +270°/symbol → −150 Hz shift → line at 1050 Hz | — (caller does not send it) |
| `S1` | phase toggles 0/+90° at 300 Hz → carrier at 1200 Hz plus sidebands at 900 and 1500 Hz | E1200 = 0.55, E900 = 0.20, E1500 = 0.23 |
| `SB1` | scrambled → broadband, no lines | all bands < 0.04 |

The state machine waits for the caller's `S1`, and it is the *end* of that signal which turns on the
equivalent of circuit 112 and starts the fixed part of the schedule: our `S1` for 100 ms, `SB1` at
1200 bit/s to fill out 600 ms from circuit 112, then scrambled binary 1 at 2400 bit/s, then data.

### It connects, and data flows both ways

```
[ 2.500] ANS      -> USB1 (USB1 on air)
[ 3.260] USB1     caller's S1 detected (-24 dBFS) after 0.76 s of USB1
[ 3.360] USB1     caller's S1 ended - circuit 112 on; our S1, then SB1 at 1200,
                  then scrambled ones at 2400
```

and on the modem's own serial port:

```
CONNECT 2400
sent 2860 bytes in 12.0s (238 byte/s)
received 2882 bytes
```

**`CONNECT 2400`** is the modem's own verdict: it considers itself connected at 2400 bit/s to our
soft-modem. 238 byte/s in each direction is exactly 2400 bit/s with ten bits per character. So there
is a real V.22bis data connection, carrying bytes in both directions simultaneously.

**Our transmitted text arrives correctly**, and in the first run — with the modem receiving only —
it arrived byte-exact and at the right rate:

```
CONNECT
SLOPMODEM SLOPMODEM SLOPMODEM SLOPMODEM SLOPMODEM SLOPMODEM SLOPMODEM ...
```

at 48 characters per 200 ms logging interval, i.e. 240 char/s.

**The modem's own SB1 decodes perfectly**: descrambled to all ones with median distance to the
lattice of 0.044–0.065, across every segment where it was idling.

### The framing slip: found and fixed

Our characters arrived correct for about 21 of them and then degraded into a fixed repeating pattern
whose period equalled the payload length. Four candidate causes were tested; three were wrong, and
saying which is the useful part.

**Ruled out — a self-consistent false alignment in the test data.** If some wrong bit offset happened
to give a valid start bit and stop bit on every frame, a receiver could latch there permanently. It
does not: across all ten offsets the best false alignment scores 0.82, not 1.00, so a receiver sitting
there would see ~18 % framing errors and be forced to hunt.

**Ruled out — the RTP watchdog.** `rtp.pump` emits an extra outbound frame when the inbound stream
goes quiet, which would push 160 unrequested samples — 12 symbols, 48 bits — at the far end. That
would slip framing permanently, and the runs did report `1 watchdog`. Disabling it gave strict 1:1
pacing and `0 watchdog`, and the slip happened anyway. (The change was kept regardless: extra frames
are simply wrong for a modem stream.)

**Ruled out — the scrambler's 64-ones guard.** §5.1 says the guard "will not operate during
handshaking", so a guard firing at the wrong moment would put a stray bit into the stream.
Instrumenting the actual transmitted sequence: it never fires, and the longest run of ones at the
scrambler output is 18.

**Ruled out — the transmitter.** Decoding our *own* generated audio with our own receiver, offline,
gives framing 1.00 and clean text in every two-second window across the whole twelve seconds. The
transmit chain is correct end to end in the audio domain, so the corruption is introduced on the line.

**The actual cause: no idle reference for the far end to re-lock against.** Characters were being sent
back to back at exactly the line rate, 10 bits each at 2400 bit/s, which is the one thing a real DTE
never does. With no mark period between characters, a single channel bit error makes the far end's
receiver see a bad stop bit, hunt for the next start bit, and re-lock at the wrong phase — after
which stop bits still land on ones often enough that it never hunts again. One bit error, permanent
garbage.

The fix is two idle mark bits after each character, which is what a DTE running just below the line
rate produces. It costs throughput — 12 bits per character instead of 10, so 200 char/s rather than
240 — and it buys a receiver that recovers from any single error.

### Result: a clean bidirectional connection, sustained for a minute

| | Soft-modem → modem | Modem → soft-modem |
|---|---|---|
| Duration | 65 s | 65 s |
| Rate | 200 char/s (12-bit framing) | 240 char/s (10-bit framing) |
| Bytes checked | **13 009** | **4 886** (7 windows spanning 16–77 s) |
| Correct | **100.000 %** | **100.000 %** |

The outbound direction is scored on the modem's own serial output against the expected repeating
pattern, at every phase. The inbound direction is decoded from the captured RTP audio through the full
receive chain — timing search, carrier offset removal, CMA, alignment, carrier tracking, DD-LMS,
quadbit decode, descramble, deframe — with median distance to the lattice of 0.063–0.078 and framing
1.00 in every window.

**That meets the bar in `rules.md`:** at least a minute of data-phase data transmitted in both
directions.

### One more bug fixed on the way

The descrambler was missing the counterpart of the scrambler's 64-ones guard. §5.1 has the scrambler
invert its next *input* after 64 consecutive ones at its output; §5.2 has the descrambler invert its
next *output* on seeing the same condition at its input. Implementing one without the other leaves a
single-bit error every time the guard fires — and, as this whole exercise showed, a stray zero inside
idle marks reads as a start bit. It never fired in these runs, but it was a live trap. `test_v22.py`
now constructs an input that deliberately forces a 64-long run of ones at the scrambler output and
checks the round trip survives.

## The second modem: Cirrus (`**1`)

Repeating the exercise on the other unit found two real conformance defects that the Conexant had
been quietly absorbing. Setup differences first: the Cirrus takes the legacy Rockwell command set,
so buffered mode is `AT\N0` and compression off is `AT%C0` (it has no `+ES`/`+DS`), and it needs
`ATDT` rather than `ATD` for the `*` digits.

### The symptom, and what it ruled out

It reached `CONNECT` and stayed up, but everything it handed its DTE was garbage. Three measurements
narrowed that down before touching any code:

- **Its own transmitter is textbook.** Ring radii from the captured audio came out 1 : 2.24 : 3.00
  against the ideal √2 : √10 : √18 = 1 : 2.236 : 3.000, with ring populations 27 / 48 / 26 % against
  the 25 / 50 / 25 % that uniform data over Figure 2 gives. It was transmitting proper 16-QAM at
  2400 bit/s, and our receiver decoded its payload cleanly.
- **It was not a framing slip.** The garbage arrived at 240 char/s — exactly 2400 ÷ 10 — which looks
  like a receiver deframing 10-bit characters against our 12-bit ones. But autocorrelating the
  received bytes over lags 1–260 peaked at 0.8 %, which is the 1/256 chance floor. A misframed but
  bit-locked receiver would reproduce our repeating payload at *some* lag, at 100 %. Nothing was
  periodic, so the receiver was not locked at all.
- **So the fault was in what we transmit,** despite the identical stream working on the Conexant.

### Defect 1: the 1200 bit/s constellation was 18.43° off the lattice

V.22bis §2.5.2.2, for the 1200 bit/s phases: the dibit gives the phase quadrant change exactly as at
2400 bit/s, and *"the signalling elements corresponding to 01 in the signal constellation shall be
transmitted irrespective of the quadrant concerned. This ensure[s] compatibility with Recommendation
V.22."*

Those four points — (3,1), (−1,3), (−3,−1), (1,−3) — are all of magnitude √10, so after
normalisation they are unit-amplitude just like V.22's QPSK. But they lie at **18.43° + k·90°, not
on the axes.** We were rendering the 1200 bit/s phases with `v22.Mod`, which emits
`exp(j·quadrant)` from a zero start, i.e. on the axes. Every 1200 bit/s signal we sent — USB1, S1,
SB1 — was therefore rotated 18.43° away from the 16-QAM lattice that the far modem is about to make
16-way decisions against, having trained its carrier phase on exactly that signal.

It is spectrally invisible and still differentially decodable, which is why it survived this long.

### Defect 2: the modulator restarted on every call

`Mod.shape()` rendered each burst independently. Three things restarted every time it was called:
the carrier phase (`cos(w·i)` from `i = 0`), the fractional 13⅓-sample symbol grid, and the 81-tap
SRRC filter, whose tail was allocated, half-filled and then thrown away.

The answer sequence is four concatenated calls — S1, SB1 at 1200, scrambled ones at 2400, then data
— so all three discontinuities landed **at the 1200 → 2400 handover**, which per §6.3.1.1.1 d) is
450 ms after circuit 112 goes on: the exact moment the far receiver starts making 16-way decisions.
The Conexant's equaliser rode through it. The Cirrus's never recovered.

The fix is what a real modem does: one modulator for the whole sequence. The rate change is *only* a
change of bits per symbol — 600 baud, same carrier, same lattice — so a single `v22bis.Mod` renders
S1 through the payload with no discontinuity at all. `shape()` now carries three pieces of state: the
carrier sample index, a global symbol counter, and the pulse-shaper's overlap-add residue. Callers
that render a one-shot burst and stop must now ask for the tail explicitly with `flush()`.

### Joining a stream that gets cut short

The complication is that the USB1 run is pre-rendered for 6 s and then cut wherever the caller's S1
happens to end, so the following stream has to pick up at an unknown point. The numbers make this
exact rather than approximate:

```
160 samples (one RTP frame) = 12 symbols at 600 baud
                            = 48 cycles of the 2400 Hz carrier
                            = 12 USB1 dibits, a multiple of the 4-symbol quadrant cycle
```

Every RTP frame boundary is therefore simultaneously a symbol boundary, a carrier zero-phase point,
and a quadrant-cycle boundary — and the cut can only fall on a frame boundary, because that is the
granularity the RTP pump consumes. Priming the second modulator with 12 USB1 symbols and discarding
those 160 samples leaves it on the same grid with its shaper carry filled.

Verified rather than argued: splicing the cut stream onto the primed one and comparing 14 400 samples
against a single modulator that rendered the whole thing without a splice gives **max difference
0 LSB, zero differing samples.**

### Result

| | Soft-modem → modem | Modem → soft-modem |
|---|---|---|
| Duration | 65 s | 7 windows spanning 10–68 s |
| Bytes checked | **13 003** | **3 555** |
| Correct | **100.000 %** | **100.000 %** |

Outbound is scored on the Cirrus's own serial output against the expected repeating pattern at every
phase; the byte autocorrelation that read 0.8 % before the fix now reads 100.0 % at every multiple of
the payload length. Inbound is decoded from the captured RTP audio through the full receive chain,
with median distance to the lattice of 0.067–0.080.

**The Conexant was re-run afterwards on the same code and still scores 100.000 % (13 019 bytes),** so
the fix is a fix and not a trade.

### Honest limit: the offline receiver is a block equaliser

The inbound figure above is scored over 3-second windows. Widen them to 5 seconds and the Cirrus
decode degrades to 88.5 %, with median distance to the lattice rising from 0.07 to 0.22–0.34; the
Conexant holds 100 % at 0.12–0.18 over the same windows. That is a property of our receiver, not of
either link: `equalise.cma()` and `equalise.dd_lms()` each compute **one** tap set and apply it to
the whole window, so nothing tracks channel drift *within* a window, and the longer the window the
worse the compromise. Each window also pays its own blind-acquisition transient, since CMA restarts
from a centre spike every time — which is why the post-acquisition figure is the meaningful one and
the whole-window figure (98.1 %) is not. A sample-by-sample tracking equaliser removes both
artefacts; that is what the next section builds, and it takes the same Cirrus capture to a median of
0.014 over a single 61-second pass.

### A test that was passing by luck

Making the modulator continuous shifted its output by the shaper's 40-sample group delay, which the
old code had been discarding. The transmitted waveform was otherwise **bit-identical** — max
difference 0 LSB in steady state. But the 15 dB SNR loopback check went from 0.00000 to 0.00510
against a 0.005 threshold, because the shift moved which samples the noise landed on.

Measured over ten noise realisations instead of one, both versions score a mean BER of **0.00357**,
with 1–2 seeds in ten reaching zero. The old test had simply drawn a lucky seed on a threshold that
sits at the mean. It now averages over ten seeds, so it asserts on the modem rather than on the
draw.

## The tracking receiver

`softmodem/tracking.py` replaces the block receiver for decoding captures. The block chain had four
stages that each computed one answer for a whole window and applied it to all of it: a brute-forced
timing phase, a linear-fit frequency offset, one CMA tap set and one DD-LMS tap set. That is why
widening the window from 3 s to 5 s made it worse rather than better, and why every window paid its
own blind-acquisition transient. All four are now loops that run once per symbol.

### Structure

| Stage | What it does |
|---|---|
| Interpolating matched filter | SRRC as a 128-phase polyphase bank, read at any continuous time |
| Timing | Gardner detector on the 2-samples-per-symbol stream, PI loop steering the sample instants |
| Equaliser | T/2-spaced fractionally-spaced FIR, 21 taps, NLMS update every symbol |
| Carrier | rotator after the equaliser, decision-directed PI loop |

Three pieces of arithmetic make the front end exact rather than approximate:

- **The polyphase index depends only on the fractional delay.** Reading the matched filter at time
  τ = m + f, with the filter's half-span D and the window starting at m − D, makes tap k's argument
  (f + D − k)/SPS — a function of f alone. So one table of sub-phases covers every possible sampling
  instant, there is no separate resample-then-filter step to accumulate error, and SPS never has to
  be an integer (it is 13⅓).
- **The carrier tables are exactly periodic.** 8000/1200 and 8000/2400 are 6⅔ and 3⅓ samples per
  cycle, so three cycles take exactly 20 and 10 samples. Downconversion is a lookup on `n % period`
  with no accumulated phase error at all.
- **Gardner is exactly carrier-phase invariant.** Its error term is `Re{conj(mid)·(cur − prev)}`;
  multiplying every sample by a common e^{jθ} leaves `conj(mid)` picking up e^{−jθ} and the
  difference picking up e^{+jθ}. So it can run before the carrier loop knows anything.

### Acquisition is measured, not scheduled

There is no fixed handover point. The receiver is in `acq` (CMA, timing loop at 3× gain, no rotator)
or `dd` (decision-directed equaliser and carrier loop), and moves between them on measurements:
every 200 symbols during acquisition it asks whether the eye is open, and when it is, it takes the
scale, phase **and frequency** from that same measurement. If the running mean square decision error
later passes a threshold, it drops back to `acq`.

That last part is not optional. DD-LMS is driven by the very decisions that have gone wrong, so it
cannot re-acquire on its own: measured on a channel that changes mid-capture, it locked at 0.07,
broke at the change and never came back. CMA does not depend on decisions at all. This is a local
version of what V.22bis §6.4 does on the wire when a modem detects loss of equalisation.

### Five things that were wrong on the way, and how each was found

Each of these was diagnosed by measurement, and each is worth recording because none was guessable.

1. **Every decision was landing on a mid-point.** The half-symbol counter was incremented *before*
   the parity test, so the first "on-symbol" instant was τ₀ + SPS/2 and every symbol thereafter was
   sampled exactly between two symbols — the worst possible phase. Found by switching every loop off
   and comparing against a fixed-phase control that decoded perfectly: median lattice distance 0.86
   with all gains at zero is not a loop problem.
2. **CMA needs a scaled input.** Its error term `y(R2 − |y|²)` is cubic in amplitude, so at raw A-law
   sample scale (~10³) the very first tap update is order 10¹ and the equaliser diverges immediately.
   The block receiver hides this by normalising the whole window up front, which a streaming receiver
   cannot do — hence a short measured prologue that sets one input gain.
3. **The Gardner sign.** Measured both ways rather than reasoned about: with the sign the code now
   uses, the loop settles at 0.009 samples of accumulated correction on a clean signal; with the
   other it runs off to −8.5 samples and closes the eye.
4. **The frequency estimator aliases inside the legal offset range.** `arg(E[z⁴])/4` unwrapped modulo
   90° is only valid while the phase advances well under 45° within one block, which at 600 baud and
   a 48-symbol block is about 1.5 Hz. V.22bis §2.6 permits **±7 Hz**, which advances 201° per block:
   the fit reported 0.96 Hz for a true 7 Hz offset and the carrier loop never pulled in. The fix is a
   coarse search first, which costs almost nothing because de-spinning by f and maximising
   |Σ z⁴| *is* a periodogram of z⁴ evaluated at 4f. With 600 symbols that resolves f to a quarter of
   a Hz, well inside what the block fit then refines. Result: ±7 Hz now locks to within 0.001 Hz.
5. **The tracking step size was set for stability, not for tracking.** At `mu_dd` = 0.0025 the tap
   time constant is ntaps/mu = 8400 symbols (14 s), far too slow for a channel that moves in 4 s, and
   the loop lost lock twice. Raising it to 0.02 cost nothing measurable at 24, 20 or 17 dB SNR, where
   the median lattice distance is set by the additive noise rather than by gradient noise.

### A 45° bias that was never an ambiguity

`E[z⁴]` for this lattice is exactly **−68**, so `arg(E[z⁴])/4` is exactly **+45°** — a fixed property
of the constellation, not something the signal leaves undetermined. The block receiver's `align()`
does not subtract it, which is why `v22bis_rx.py` and `equalise_full()` have to try both 0° and 45°
and keep whichever decodes better. Subtracting the constant removes the guesswork entirely, and the
tracking receiver does.

### Offline tests

`test_tracking.py`, 20 checks, no hardware. Each impairment targets one loop.

| Case | Result |
|---|---|
| clean channel | BER **0** |
| static timing offset 3 and 7 samples | BER **0** |
| sample clock ±200 ppm (spec allows ±100) | BER **0** |
| carrier offset ±7 Hz (§2.6 allows ±7) | BER **0** |
| 3-tap and 5-tap multipath channels | BER **0** |
| channel drifting continuously | BER **0**, and **no retrain** |
| channel swapped abruptly halfway | loss of lock detected, re-acquires, BER **0** afterwards |
| 30 dB and 24 dB SNR | BER **0** |

The abrupt-swap case deliberately does not assert zero errors overall. A discontinuous channel change
costs data in any modem — V.22bis §6.4 clamps circuit 104 through a retrain for exactly this reason —
so what is asserted is that the receiver notices, re-acquires and is clean afterwards, not that the
change is free.

### On the real captures: one pass over the whole minute

Both minute-long captures decoded in a single continuous pass, no windowing:

| | Cirrus (`**1`) | Conexant (`**2`) |
|---|---|---|
| Symbols in one pass | 39 210 (65.3 s) | 42 113 (70.2 s) |
| Acquired at symbol | 800 | 5 400 |
| Carrier offset found | +0.039 Hz | −0.035 Hz |
| Median lattice distance | **0.014** | **0.019** |
| Characters scored | 11 667 | 13 945 |
| Correct | **100.0000 %** | **99.9355 %** (3 slips) |

Head to head against the block receiver on the same Cirrus capture:

| Receiver | Median lattice distance | Correct |
|---|---|---|
| Block, 3 s windows | 0.067–0.080 | 100 % of 3 555 chars, post-acquisition only |
| Block, 5 s windows | 0.224–0.339 | 88.5 % |
| **Tracking, one 61 s pass** | **0.014** | **100.0000 % of 11 667 chars** |

The tracking receiver is roughly **five times closer to the lattice** than the block receiver managed
on its best window size, over twenty times the span, and it scores more than three times as many
characters because nothing is discarded to per-window acquisition.

It also correctly reports the end of the call: on the Cirrus capture the far modem stops transmitting
at 65.07 s and the loss-of-lock detector fires at symbol 39045 (65.08 s), with median lattice distance
still 0.014 at symbol 38900 and 1.397 at 39000.

Two boundary guards are used when scoring, and both are derived rather than tuned: the DD tap time
constant is ntaps/mu = 21/0.02 ≈ 1050 symbols (measurement: median distance reads 0.098, 0.041,
0.069, 0.090, 0.099, 0.067, 0.037 over successive 200-symbol groups before settling to 0.013), and
the loss-of-lock detector averages with α = 0.01, i.e. a 100-symbol window, so it reports a drop about
that late.

**Runtime: 65.3 s of line time decodes in about 1.0 s wall** — 65× real time, in pure Python on one
core. That matters for the "streaming receive" item below: there is ample headroom to run this live.

### The descrambler's own transient

§5.2's descrambler is self-synchronising with polynomial 1 ⊕ x⁻¹⁴ ⊕ x⁻¹⁷, so its register is 17 bits
deep and its first 17 output bits are computed from contents that are not yet the real ones. Those
bits are wrong *by construction*. On a stream that starts mid-transmission they were the only thing
wrong: dropping exactly 17 bits took the Cirrus decode from 7 bad characters in 11 668 to **zero in
11 667**. Note that dropping 17 *input* bits does not work and is a different thing — the descrambler
then simply starts cold 17 bits later.

### Scoring: a single global phase is the wrong model

The Conexant capture first scored 92.9 %, which was a measurement artefact, not a decode failure.
The decoded text either side of the mismatch was visibly perfect; what had happened was that
**six characters went missing**, which shifts the phase of every character after it, and a
fixed-phase scorer then charges the entire remainder as wrong. A ±4-character resync search still
missed it — both events turned out to be exactly +6.

Scoring now walks the stream and re-aligns across all phases of the pattern, requiring 40 consecutive
exact characters to accept a resync, and **reports the slip count rather than hiding it**. On that
basis the Conexant capture is 99.9355 % of 13 945 characters with 3 slips, and only two windows out
of 139 hundred-character windows contain any mismatch at all.

The slips are consistent with brief transmit-buffer starvation at the far modem rather than with any
bit error: the symbol stream is clean straight through them (median 0.019), and in that run the Pi
fed the modem at exactly 240 byte/s against a 240 char/s line rate, leaving no margin for timing
jitter. This is also where the rate row in the table above was corrected a second time — the two
directions in that run were paced differently, 200 char/s out of the soft-modem with 12-bit framing
and 240 char/s out of the modem with 10-bit framing, and an earlier edit had wrongly set both to 200.

### Known limits left in the superseded block path

`equalise.py` is still used by `v22bis_rx.py` and by the hardware-capture check in `test_v22.py`, and
two limits found while building the tracking receiver apply to it. Neither has ever bitten on this
rig, where measured offsets are ±0.05 Hz, but both are real:

- `estimate_freq_offset` with its default 48-symbol block cannot represent an offset beyond about
  ±1.5 Hz, and fails silently rather than loudly — it returns a plausible small number.
- `align` leaves the 45° lattice constant in, so its callers must try both rotations.

## Live receive: the tracking receiver in the RTP callback

The receiver now runs during the call. `tracking.LiveRx` is fed the inbound
samples from `run_answer.py`'s `on_frame` and hands back characters, so the
caller's data is decoded in real time and scored when the call ends. Nothing is
decoded afterwards.

### The refactor that made it safe

The loop moved into `tracking.StreamRx`, which takes samples a frame at a time,
and `TrackingRx` became a thin wrapper that feeds a whole array through it in one
call. There is one implementation, not two, so the offline tests cover the live
code — and that equivalence is asserted rather than assumed:

| Check | Result |
|---|---|
| same symbol count from 160-sample frames | 5998 vs 5998 |
| symbols are bit-identical | max difference **0.000e+00** |
| same acquisition point and retrain count | dd_at 600/600, retrains 0/0 |

Three things make the streaming front end exact rather than merely close:

- **The sample buffer is absolutely indexed.** The carrier lookup is
  `lut[i % period]` on the absolute stream index, and since the period is 20
  samples for the low channel and 10 for the high, while an RTP frame is 160
  samples, every frame boundary lands on lut index 0. There is no phase to carry
  across frames and nothing to accumulate.
- **The prologue delays rather than discards.** A block receiver normalises the
  whole window before CMA can run; a streaming one cannot, so it measures 600
  half-symbols (about 0.5 s) to set one input gain, then rewinds to τ₀ and
  demodulates those same samples. The buffer is deliberately not trimmed while
  that is running.
- **The buffer is bounded.** It holds a sliding window from `floor(τ) − D`
  onward, not the call.

### Acquiring the character framing was the hard part

The DSP was the easy half. Recovering 8N1 framing from a stream joined in
progress took four attempts, and each failure is worth recording because each
looked reasonable:

| Attempt | Why it failed |
|---|---|
| Plain hunting — take the next zero as a start bit | At a wrong phase the stop bit is mark often enough that it rarely re-hunts. **1610 of 1644 characters wrong** on a stream with no idle bits. |
| Wait for an idle run first | A run of mark bits is not evidence of idle: a `0xFF` data byte is nine consecutive ones by itself. The framer locked inside the data and never recovered. |
| Score the ten framing offsets | Assumes characters are ten bits apart. The Cirrus sends ~12.6 bits per character and V.14 lets the gap vary, so there is no fixed stride. |
| *Validated* hunting — require N consecutive good frames | Better, but the hunt itself is data-driven: on a periodic pattern it cycles through a subset of phases and never visits the right one. **122 framing errors, zero locks.** |

What works is a systematic sweep. Every zero bit in the acquisition window is a
candidate frame start; for each, simulate forward — start bit, eight data bits,
stop bit, then *hunt for the next zero* — and count how many consecutive frames
validate. Take the best. Including the hunt step in the simulation is what makes
it indifferent to the sender's idle discipline: idle bits are simply skipped, so
one search locks a 10-bit back-to-back stream and a 12.6-bit idle-framed one.
Tested at every join phase across 0, 1 and 2 idle bits: zero bad characters in
all nine combinations.

One subtlety cost a character and is worth stating. A candidate starting one
position early produces a bogus first frame and then, because the following hunt
lands on a real start bit, **realigns** — so it validates just as many frames as
the correct candidate and wins by being earlier. The fix is to emit the evidence
and not the hypothesis: frames 2..n are hunted forward from a confirmed stop bit
and are self-consistent whatever the initial guess was, so only they are handed
on. That costs at most one good character and makes everything emitted
trustworthy. Before: exactly one wrong character, always at index 0, with a valid
stop bit and no framing error to show for it.

### Stopping cleanly when the far carrier dies

The first live call scored 99.936 %, and all eight wrong characters were the last
eight — the far modem hanging up. Two facts from that measurement:

- A level gate would not have caught them. During data the frame level is −23 to
  −27 dBFS and after the carrier dies it settles at −48 to −50 dBFS, a clean 20 dB
  separation — but the corrupt characters land in the ~100 ms decay, while the
  level is still high.
- The existing loss-of-lock detector did fire, just 167 ms late, because it
  averages the decision error over 100 symbols.

So there are now two windows on the same error signal, for two different jobs.
The slow one (100 symbols) still decides when to throw away a converged
equaliser, which should not be a hair trigger. A short one (10 symbols, ~17 ms)
gates only whether characters are handed on — which is what V.22bis §6.4 a)
describes when it says circuit 104 "may be clamped to binary 1" on loss of
equalisation. Replaying both minute-long captures through the live path with the
gate in place: **100.0000 %, zero wrong characters**, with 33 and 34 symbols
withheld at the carrier drop.

### A search grid coarser than its own resolution

Trimming the acquisition cost for the real-time budget produced a good lesson.
`assess`'s coarse frequency search was widened from a 0.1 Hz grid to 0.5 Hz,
which cut it from 7.7 ms to 2.1 ms and still locked ±7 Hz exactly — and broke
both sample-clock tests. A 200 ppm clock error drags the 1200 Hz carrier by
0.246 Hz, which is **exactly half a grid step**: acquisition slid from symbol 600
out to symbol 4800 and the eye stayed half shut.

The periodogram's own resolution over an N-symbol tail is BAUD/4N, which is
0.25 Hz for N = 600. Searching coarser than that silently misses the peak;
searching finer is wasted work. The step is now derived from the tail length
rather than chosen, which costs 3.7 ms and puts the failure out of reach by
construction.

### Results: both directions live, in one call

| | Cirrus (`**1`) | Conexant (`**2`) |
|---|---|---|
| Soft-modem → modem | **100.000 %** of 13 004 bytes | **100.000 %** of 13 001 bytes |
| Modem → soft-modem, **decoded live** | **100.0000 %** of 12 499 chars | **100.0000 %** of 12 645 chars |
| Slips / framing errors | 0 / 0 | 0 / 0 |
| Acquired at symbol | 1 200 | 5 000 |
| Carrier offset tracked | +0.043 Hz | −0.035 Hz |
| Retrains | 1 (the carrier drop at hang-up) | 0 |

Both figures come from the same 65-second call, the outbound one scored on the
modem's serial output and the inbound one decoded inside the RTP callback while
the call was still up. **`rules.md`'s bar is now met without any offline
processing.**

### Real-time margin

An RTP frame is 20 ms of audio, so the callback has 20 ms to do everything —
receive, demodulate, decode, and generate the outbound frame.

| | Cirrus call | Conexant call | Replay, no network |
|---|---|---|---|
| Mean per frame | 1.31 ms | 1.34 ms | 0.23 ms |
| Worst per frame | 17.5 ms | 19.0 ms | 4.2 ms |
| Frames | 3 509 | 3 749 | 3 471 |

The algorithmic worst case is the ~4 ms in the replay column: one `assess` call,
which happens at most once per 200 symbols during acquisition. The 17–19 ms seen
on the live calls is scheduling jitter on a two-core host that is also running
the SIP stack, the RTP pump and an ssh session — not the DSP. It is also
harmless: the 1:1 pacing rule is receive-driven, so a callback that returns late
simply emits its frame late and catches up on the next inbound packet, which the
socket has already buffered. Nothing is dropped; the only cost is a few
milliseconds of jitter on our own transmit.

## The DTE side: a pseudo-terminal and V.250

Up to here the modem had no user. Both directions of the line worked, but the
transmitted data was a fixed pattern compiled into the modulator and the received
characters ended up in a buffer. `softmodem/dte.py` supplies the other half of a
modem's job: something a DTE can open, configure and talk through.

### What had to change on the transmit side

The data phase was pre-rendered — `fsm.AnswerV22bis` built the whole payload into
its outbound stream at construction time, which is fine for a fixed pattern and
useless for a DTE, because nothing that happens during the call can influence it.
It is now driven a frame at a time from a callable:

```
20 ms at 2400 bit/s = 48 bits = 12 symbols at 600 baud = 160 samples
```

The handshake — S1, SB1 at 1200, then 400 ms of scrambled ones at 2400 — is still
rendered up front, because it is timing-critical and already validated. The data
phase continues with the *same* modulator, which works only because `shape()` was
made stateful and continuous when the Cirrus was fixed: the join needs no special
handling at all. The FSM gains a `DATA` state, entered where §6.3.1.1.2 d) says
circuit 106 is conditioned to respond.

The old pre-rendered path is kept, because every earlier measurement in this
document was made with it.

### The V.14 transmit converter

§4.1.2 says start-stop data "shall be converted in conformity with
Recommendation V.14 to a synchronous data stream". `dte.AsyncEncoder` does that:
frames each byte 8N1, and — the part that matters — **emits mark bits when the
DTE has nothing to send**, because the line runs at 2400 bit/s whether or not
there is data for it. `take(n)` always returns exactly n bits.

This is the same behaviour that was observed coming back *from* the hardware
modems and written up earlier as a slip. Now we generate it, and the count is
reported: in the 40-second run below, 11 796 idle mark bits while the DTE had
nothing to send.

### The interface itself

A pseudo-terminal. The slave path (`/dev/pts/N`) is a real character device with
real termios, so any serial program can open it — the test DTE does, and so would
`minicom -D /dev/pts/N`. The modem holds the master end and polls it once per RTP
frame; the poll never blocks.

Implemented from V.250: command mode and data mode, the result codes (`OK`,
`ERROR`, `RING`, `CONNECT 2400`, `NO CARRIER`), concatenated command lines,
`ATE`/`ATQ`/`ATV`/`ATZ`/`ATI`/`AT&F`/`AT&V`/`AT&C`/`AT&D`, `ATA`, `ATH`, `ATO`,
S-register read and write, `+GMI`/`+GMM`/`+GMR`, and the `+++` escape with the
§5.2.3 guard timing off `S12`. `ATD` is refused: this modem answers, it does not
dial. Anything unimplemented gives `ERROR`, which is what V.250 asks for — `AT%C1`
does, `ATZZZ` does not, because Z three times is a legal line.

**Two honest limitations.** A pseudo-terminal has no hardware modem-control
leads: there is no way to assert DCD (circuit 109), CTS (106) or DSR (107) on a
pty, so they are conveyed the way V.250 conveys them to software that cannot see
the leads either — as result codes and `AT&V` state. Circuit 106 is stood in for
by back-pressure: the modem stops reading from the pty once its transmit queue
reaches 4 kB, so the DTE's own `write()` blocks, which is what CTS off
accomplishes on a real interface. That is asserted in the tests rather than
assumed.

### The full chain, end to end

`orch_dte.py` runs three processes: the soft-modem with `--dte`, `dtechat.py`
attached to the pty it creates, and the hardware modem on the Pi dialling `**620`.
The path a byte takes is

```
dtechat.py -> pty -> V.14 encoder -> 16-QAM modulator -> RTP -> FRITZ!Box
           -> Cirrus modem -> its serial port -> v22data2.py
```

and back the other way through the tracking receiver. With `S0=0` the soft-modem
reports `RING` and waits, and the DTE's `ATA` is what answers the SIP call.

Observed sequence, 40 s of data each way:

```
DTE  | opened /dev/pts/1
DTE  | saw RING
SIP  | ATA from the DTE - answering
SIP  | [ 3.120] caller's S1 ended - circuit 112 on
SIP  | [ 4.120] training done - ready to transmit, streaming from the DTE (circuit 106 on)
DTE  | then CONNECT 2400
```

| | Result |
|---|---|
| DTE → hardware modem (scored at the modem's serial port) | **100.000 %** of 7 524 bytes |
| Hardware modem → DTE (scored in the DTE program) | **100.0000 %** of 7 493 bytes, 0 slips |
| The soft-modem's own live receiver | **100.0000 %** of 7 538 characters, 0 slips |
| V.14 idle fill | 11 796 mark bits |
| Callback cost | 1.30 ms mean, 18.0 ms worst over 2 196 frames |

The byte counts differ between the three views because the three processes' send
and receive windows do not align, not because anything was lost — the
slip-tolerant scorer reports **0 slips**, and a character lost mid-stream would
register as one.

A second run with the DTE's data window shorter than the call exercised the other
teardown path: `+++` escaped to command mode with the call still up, `AT&V`
answered while online, and `ATH` ended it — the soft-modem reported
`stopped by: dte-ath`, and the DTE's receive was 100.0000 % with 0 slips. (The
hardware modem read 99.584 % in that run: it kept reading for 27 s after the DTE
stopped sending, so its tail is V.14 idle followed by the carrier drop — the same
end-of-call artefact the receive gate handles on our side.)

### Tests

`test_dte.py`, 35 checks, no hardware and no SIP. Three layers, because they fail
differently: the encoder at bit level (including all 256 byte values through
encoder and framer), the whole transmit-to-receive chain at character level
(6 998 characters, zero errors, with the encoder deliberately running dry), the
FSM actually reaching its `DATA` state when fed a synthetic caller's S1, and the
pseudo-terminal driven the way a DTE drives it — opened by path, set raw, talked
to in V.250.

## Originating: the calling side of the handshake

`fsm.OriginateV22bis` implements §6.3.1.1.1, `run_call.py` places the call, and
`ATD` on the DTE now works. The soft-modem can dial a hardware modem and exchange
data with it, which is the last of the four roles — before this it could only be
called.

### What swaps, and what gets harder

§6.1.1 puts the calling modem's transmitter in the **low** channel and its
receiver in the **high** one, the mirror of the answering side, and the guard tone
is the answerer's job (V.22 §2.4) so we send none.

The hard part is that the calling side is the patient one. Every step is timed
from something it *detected* rather than from when the call started, and it has to
sit silent through an answer tone of unknown length — 2.2 s from our own answerer,
but measured at 3.9 s from the Cirrus and 8.4 s from the Conexant, because V.8 has
them wait out Te before giving up on a CM that never comes.

### Detecting USB1, and an arithmetic trap

§6.3.1.1.1 b) triggers on "155 ± 10 ms of unscrambled binary 1", so the caller has
to pick USB1 out of the silence after the answer tone. That it sits at 2250 Hz was
already on record in `sip-audio-path.md`; what was new here was needing to detect
it, and the arithmetic is easy to get wrong.

USB1 at 1200 bit/s makes every dibit `11`, which Table 1/V.22bis turns into a 270°
quadrant change — that is **−90° per symbol**, and at 600 baud −90°/symbol is
−150 Hz. So the tone sits at **2400 − 150 = 2250 Hz**. Reading the 270° as
270°/symbol instead gives −450 Hz and a prediction of 1950 Hz.

That is exactly the mistake made here, and the detector found **nothing at all**
at 1950 Hz. It surfaced immediately only because the calibration was run before
the detector was trusted — which is the habit this rig has repeatedly rewarded.

Both detectors are calibrated rather than assumed. Measured over 160-sample
frames on the high channel, normalised by frame power:

| signal | e2250 | e2400 | e2100 | e2700 |
|---|---|---|---|---|
| USB1 | **0.937** | 0.000 | 0.000 | 0.000 |
| S1 | 0.000 | **0.505** | **0.247** | **0.247** |
| SB1 | 0.036 | 0.109 | 0.018 | 0.010 |
| data | 0.083 | 0.106 | 0.052 | 0.039 |
| ANS | 0.000 | 0.000 | 1.000 | 0.000 |

S1 alternates `00` and `11`, so the phasor alternates between two points 90°
apart at 300 Hz: carrier plus sidebands at 2400 ± 300. Those are the same
thresholds `AnswerV22bis` already uses on the low channel, and the answer tone —
which is what a naive S1 test would trip on — puts nothing at 2400 at all.

### Back to back: our originator against our answerer

The centrepiece test needs no hardware and no SIP: the two state machines are run
against each other frame by frame, each hearing only what the other transmitted.
A timing error on one side then cannot hide behind a matching error on the other,
and the timings are asserted against the Recommendation's own tolerances.

```
orig [ 0.600] WAITUSB1  160 ms of unscrambled binary 1 detected
orig [ 1.060] GAP       -> S1TX (unscrambled double dibit 00/11, 100 ms)
orig [ 1.160] S1TX      -> SB1TX (scrambled binary 1 at 1200)
orig [ 1.340] SB1TX     answerer's S1 ended - circuit 112 on; 2400 bit/s in 600 ms
orig [ 1.940] SB1TX     -> TRAIN (scrambled binary 1 at 2400)
orig [ 2.340] TRAIN     -> DATA
ans  [ 1.200] USB1      caller's S1 ended - circuit 112 on
ans  [ 2.200] POST      -> DATA
```

| §6.3.1.1.1 | Spec | Measured |
|---|---|---|
| a) silent until the answerer is heard | — | asserted: no non-zero sample before USB1 |
| b) USB1 detected after | 155 ± 10 ms | **160 ms** |
| b) then silent for | 456 ± 10 ms | **460 ms** |
| b) then S1 for | 100 ± 3 ms | **100 ms** |
| d) 2400 bit/s after circuit 112 | 600 ± 10 ms | **600 ms** |
| e) circuit 106 on after | 200 ± 10 ms | **200 ms** |

Data both ways in that run: **4 963 / 4 963** and **4 924 / 4 924** characters,
zero framing errors, zero retrains. Repeated with a simulated two-wire hybrid
returning each side's own transmission at −20 dB and −12 dB: still error-free
both ways. That is a real test of the channel separation, not just of the logic —
the two bands are adjacent, 675–1725 Hz and 1875–2925 Hz at 75 % roll-off.

Both give-up paths are covered too: no answer at all times out of `WAITUSB1`, and
an answerer that sits on USB1 for ever times out of `SB1TX` rather than waiting —
USB1 is neither S1 nor scrambled data, so neither branch of §6.3.1.1.1 c) fires.
The other branch of that clause, the 1200 bit/s fallback, was unimplemented when
this section was written and is the subject of the last one.

### 200 ms of training was the minimum, not the right amount

§6.3.1.1.1 e) says that after 200 ± 10 ms of scrambled binary 1 at 2400,
circuit 106 is conditioned to respond. The first live call read that as "start
sending data now", and the Conexant delivered about half a second of garbage to
its DTE before its receiver converged.

Being *ready* to transmit is not the same as having something to transmit, and
nothing in §6.3.1.1.1 requires data to start the instant 106 comes on. The
answering side of this codebase holds scrambled ones for 400 ms and both hardware
modems lock onto that reliably, so the calling side now does the same: 106 is
reported at 200 ms, data starts at 400 ms. The garbage disappeared.

### Live: the soft-modem calls the hardware

`orch_call.py` starts the Pi-side answerer (`v22answer.py`, S0 set) and then
`run_call.py`. With `--dte` the number comes from the DTE's `ATD`, so the path is

```
dtechat.py --dial -> ATD**2 -> INVITE -> ... -> Conexant answers
```

| | Cirrus (`**1`), fixed payload | Conexant (`**2`), dialled by ATD from a DTE |
|---|---|---|
| Answer tone endured | 3.9 s | 8.4 s |
| Soft-modem → modem | **100.000 %** of 9 016 bytes | **100 %** of 7 250 bytes (see below) |
| Modem → soft-modem | **100.0000 %** of 8 066 chars, 0 slips | **100.0000 %** of 7 742 chars, 0 slips |
| Modem → DTE program | — | **100.0000 %** of 6 926 bytes, 0 slips |
| Acquired at symbol | 1 000 | 1 000 |
| Carrier offset tracked | +0.091 Hz | +0.041 Hz |
| Callback cost | 1.32 ms mean, 18.0 ms worst | 1.30 ms mean |

The Conexant's own single-phase score read 97.828 % of 7 411 bytes, and that is
worth unpicking rather than reporting. All 161 mismatches are the **last** 161
bytes, and their content is `MODEM2DTE  ` — the modem's own transmit pattern,
appearing in its receive buffer after `ATH` tore the call down. It is the modem
flushing itself to its DTE on carrier loss, not corrupted line data; bytes 0 to
7 249 are error-free. (The Pi-side script keeps reading past `NO CARRIER`, which
is what let that in.)

The first Conexant attempt also took **32.4 s** to report CONNECT, which was V.42
negotiation: with `+ES` at its default the modem tries LAPM, fails without a peer,
and falls back. `AT\N0` removes it and CONNECT came at 11.1 s.

### What ATD does now

`Dte(can_dial=True)` accepts `ATD`, takes the rest of the line per V.250 §6.3.1,
keeps the digits and `*`/`#` that this PBX's numbering plan uses, and answers with
a result code rather than `OK` — `CONNECT 2400`, or `NO CARRIER` for a 486 and
`NO ANSWER` for anything else. `run_answer.py` still refuses `ATD` with `ERROR`,
because an answer-only modem genuinely cannot place a call.

### Tests

`test_originate.py`, 17 checks: the detector calibration table above, every
§6.3.1.1.1 timing against its tolerance, the back-to-back data exchange, the two
echo cases, and both give-up paths.

## One program: `modem.py`

Answering and originating were two programs with the V.22bis data call written out
twice. A modem is not two programs — it is one device on a line, and which end
starts the call is the DTE's business. `softmodem/modem.py` registers once, opens
one pseudo-terminal, and then waits for whichever comes first:

```
ATD from the DTE    -> originate   (fsm.OriginateV22bis, 6.3.1.1.1)
INVITE from the PBX -> RING, then answer (fsm.AnswerV22bis, 6.3.1.1.2)
```

…runs the call, hangs up, and goes back to waiting. **Several calls per session,
in either direction**, which is the thing neither of the old programs could do:
each was a script that handled one call and exited.

### How the roles collapsed into one call runner

The whole of the unification is two attributes on the state machines:

| | `rx_carrier` | `rx_open` |
|---|---|---|
| `AnswerV22bis` | 1200 Hz (low) | `state in (POST, DATA)` |
| `OriginateV22bis` | 2400 Hz (high) | circuit 112 onward |

With those, `_data_call` never asks which role it is driving — one `on_frame`,
one live receiver, one report, one teardown. §6.1.1 is what makes the carriers
differ; everything else was already common.

`run_answer.py --v22bis` and `run_call.py` are now shims that map their flags onto
`modem.main()`. They are kept because every command recorded in this document
still has to run, but there is one implementation. `run_answer.py` keeps its own
V.8, V.32 and tone-probe modes, which are experiments rather than a modem.

### Hanging up from the answering end

Previously a local `ATH` on an *answered* call just stopped sending, leaving the
dialog for the PBX to time out — there was no code to originate a BYE in an
inbound dialog. There is now, and it needed no new SIP machinery: `sipmin.req`
always builds `From:` as our own identity, which is exactly what an in-dialog
request from the answering side wants — our tag as From, the caller's as To, and
their `Contact` as the request URI. So both roles share one teardown path, and
`ATH` on an answered call now tears the call down properly.

### Three bugs that only unifying could find

**A module-shadowing trap.** `sip_glue` *prepended* `testrig/tools` to
`sys.path`, and both that directory and `softmodem/` contain an `ansam.py`. So
`import ansam` resolved to the wrong one — except in `run_answer.py`, which
imports `ansam` at the top *before* `sip_glue` and so cached the right module.
`modem.py` imports in the other order, and `fsm.AnswerV22bis`'s lazy
`import ansam` then died on a missing `ans_samples`.

It surfaced as the **second** call of a two-call session vanishing, with the log
saying only "handled 1 call(s)" because the traceback went to a filtered pipe.
The fix is one word — append rather than insert, since the tools-only modules
`sip_glue` actually needs (`sipmin`, `sipcfg`, `answer`) have no namesakes. There
is now a test asserting `ansam` resolves inside `softmodem/`, with `sip_glue`
imported first on purpose.

**A failed call ended the session.** The exception above propagated straight out
of the idle loop. A modem does not go away because one call went wrong; failures
are now caught, recorded and stepped over, which is also what made the shadowing
bug visible on the next run instead of silent.

**A REGISTER storm.** Re-registration was timed from the last *success*, so a
registrar that stopped answering would be asked again on every idle tick — twenty
times a second. The stubbed test made it obvious: 24 REGISTERs in a two-second
run. It now schedules the next attempt whether or not the current one worked, with
a shorter retry on failure. Down to 3.

### Results

Every scenario re-measured through the unified program:

| Scenario | Soft-modem → far end | Far end → soft-modem |
|---|---|---|
| Answer, fixed payload | **100.000 %** of 9 019 bytes | **100.0000 %** of 8 370 chars, 0 slips |
| Answer, DTE-driven (`ATA`…`ATH`) | **100.0000 %** of 3 038 bytes at the DTE | **100.0000 %** of 3 488 chars, 0 slips |
| Originate to the Cirrus | **100.000 %** of 8 009 bytes | **100.0000 %** of 7 121 chars, 0 slips |
| Originate by `ATD` to the Conexant | **100.0000 %** of 5 016 bytes at the DTE | **100.0000 %** of 5 454 chars, 0 slips |

And the new capability, one process handling two calls in opposite directions:

```
SOFT | ATD **1 -> INVITE            (call 1, originate)
SOFT | *** CALL 1 (originate) ***   MATCH 100.0000% of 4205 characters, 0 slips
SOFT | idle: waiting up to 150s for ATD or an INVITE (2 calls)
SOFT | INVITE from "Telefon" <sip:**1@fritz.box>
SOFT | *** CALL 2 (answer) ***      MATCH 100.0000% of 4500 characters, 0 slips
SOFT | handled 2 call(s)
```

with the hardware modem scoring **100.000 %** on both (5 011 and 5 012 bytes).

### The Conexant's own score, and two test-harness fixes

The Conexant as answerer reported 47 % where everything else read 100 %, and it
was worth chasing rather than waving away. Two separate artefacts, both on the
Pi side of the link:

1. **It flushes itself to its DTE on hangup.** The mismatched bytes were always a
   contiguous block at the *end*, and their content was the modem's own transmit
   pattern — after a literal `NO CARRIER` in the stream. Bytes before that were
   error-free. The script now truncates at the marker and says how much it
   dropped.
2. **The obvious place for that check does not work.** Testing inside the send
   loop never fires: once the carrier goes the modem drops CTS, `s.write()` blocks
   under `rtscts`, and the loop stalls at the write until the deadline and then
   exits from the top without reaching the check. It has to be done after the
   loop.

What remains is **43 bytes at the head** — the Conexant's own receiver converging
after it has already asserted CONNECT, which is its business and not ours; we
give it 400 ms of scrambled ones first. Final figure 99.217 %, with the errors
located and accounted for rather than averaged away.

Its CONNECT timing is also its own: 11.1, 11.4, 12.1 and 25.7 seconds across
otherwise identical runs. The 25.7 s cases are not reproducible on demand.

### Tests

`test_modem.py`, 15 checks, no SIP and no hardware — the network is stubbed. Not
the DSP, which the other suites cover, but the plumbing unification introduced:
the role attributes, SDP round-tripping, an inbound INVITE run to completion, the
idle loop choosing between an `ATD` and an INVITE, containment of a call that
raises, and the shadowing regression.

## The 1200 bit/s fallback (§6.3.1.2)

The last unimplemented branch of the handshake, and the one that makes the modem
interwork with V.22 rather than only with itself. §6.3.1.2 opens by saying "the
following handshake is identical to the Recommendation V.22 alternative A and B
handshake", so this is also V.22 support.

### How the rate gets chosen

Both sides decide on the same evidence, and it is an *absence*: scrambled binary 1
at 1200 bit/s, for 270 ± 40 ms, **with no S1**.

| | Trigger | Then |
|---|---|---|
| Caller, §6.3.1.1.1 c) → §6.3.1.2.1 c,d) | SB1 in the high channel, no S1 | circuit 109 **on**, 112 **off**; data 765 ± 10 ms later |
| Answerer, §6.3.1.1.2 b) → §6.3.1.2.2 b,c) | SB1 in the low channel, no S1 | circuit 112 **off**, then its own SB1; ready after 765 ± 10 ms of it |

The originator also gained `v22_only`, which is §6.3.1.2.1 b) — the same sequence
without the S1, straight from the 456 ms gap to scrambled binary 1. That is worth
having for its own sake, and it is how the fallback gets tested against our own
answerer without writing a second implementation of V.22.

### Detecting a signal with no tone

USB1 and S1 are recognisable by their spectra — 2250 Hz, and 2400 ± 300 Hz. SB1
has neither: it is just a modulated carrier, so there is nothing to look *for*,
only something to look *at*. The test is that the far channel carries more power
than the near one, which works while transmitting because the two channels are
separated in frequency: seven Goertzel bins across each band, and a ratio.

Calibrated with our own transmission echoed back at −12 dB, far/near band power:

| far signal | SB1 | S1 | USB1 | ANS | silence |
|---|---|---|---|---|---|
| ratio | **16.2** | 56.1 | 56.1 | 752 | **0.007** |

A threshold of 1.0 sits a factor of 16 below the lowest positive case and 140
above the negative one. S1 and USB1 read higher than SB1 because they concentrate
their energy in-band while SB1 spreads it; they are excluded by their own
detectors anyway, and the fallback additionally requires that no S1 has been seen.

### A second constellation for the receiver

§2.5.2.2's 1200 bit/s constellation is the four "01" points of Figure 2, and the
tracking receiver had 16-QAM wired into it. It now carries a `Mode`:

| | 2400 bit/s | 1200 bit/s |
|---|---|---|
| points | 16, odd coordinates | 4, magnitude √10 at 18.43° + k·90° |
| bits per symbol | 4 | 2 |
| E[\|a\|²] | 10 | **10** |
| R₂ = E[\|a\|⁴]/E[\|a\|²] | 13.20 | 10.00 |
| arg(E[z⁴])/4 | 45° | 18.43° |
| CMA dispersion floor | 42.24 | **0** |

Both have mean power 10, which is why every scaling path in the receiver is
shared and only three constants change. The 1200 bit/s set is *constant modulus*,
so its dispersion floor is genuinely zero and CMA converges on a clean eye rather
than a compromise — the easy case, for once.

The differential decode collapses too: Table 1/V.22bis gives the quadrant change
from the first two bits either way, and at 2400 the last two name the point within
the new quadrant while at 1200 there is only ever the one point. So one method
serves both, with `bps` deciding whether those last two bits exist.

### The receiver has to be built late

Which constellation is needed is not known until the handshake has chosen a rate,
so `LiveRx` is now constructed on the first frame where `rx_open` is true, using
the state machine's `rx_mode`. Both paths leave the streaming receiver its
600-half-symbol (≈0.5 s) prologue before data starts:

- 2400 bit/s: `rx_open` at circuit 112, then 600 ms to training and 400 ms of it.
- 1200 bit/s: `rx_open` at circuit 109, then the 765 ms of §6.3.1.2.1 d).

### The answerer's modulator was already in the right place

The fallback stream has to join a USB1 run that gets cut off mid-flight, and it is
generated by the same modulator that pre-rendered `post`. That works for the same
reason the 2400 path does: the priming burst is 12 symbols and `post` is 600
(60 + 300 + 240), so the modulator sits at 612 symbols and 8160 samples — both
multiples of the 12-symbol, 160-sample frame grid. No phase step, no special case.

### Timings, back to back against our own answerer

```
orig [ 0.600] WAITUSB1  160 ms of unscrambled binary 1 detected
orig [ 1.060] GAP       -> SB1TX (scrambled binary 1 at 1200, no S1 - 6.3.1.2.1 b)
orig [ 1.620] SB1TX     280 ms of scrambled binary 1 at 1200 and no S1 - fallback
orig [ 2.380] SB1TX     -> DATA1200
ans  [ 1.340] USB1      280 ms of scrambled binary 1 at 1200 and no S1 - fallback
ans  [ 2.100] SB1TX     765 ms of scrambled binary 1 sent - ready both ways
```

| Requirement | Spec | Measured |
|---|---|---|
| §6.3.1.2.1 b) gap before SB1 | 456 ± 10 ms | **460 ms** |
| b) no S1 at all | — | asserted: `S1TX` never entered |
| c) SB1 detected for | 270 ± 40 ms | **280 ms** |
| d) caller ready 765 ms after circuit 109 | 765 ± 10 ms | **765 ms** |
| §6.3.1.2.2 c) answerer ready after its own SB1 | 765 ± 10 ms | **765 ms** |

Data both ways at 1200: **3 885/3 885** and **3 913/3 913** characters, zero
framing errors.

### Live, against a modem actually set to V.22

`AT+MS=V22,1,1200,1200` on the Cirrus, which makes it a genuine V.22 peer:

| Scenario | Soft-modem → modem | Modem → soft-modem |
|---|---|---|
| It calls us (answer role) | **100.000 %** of 3 313 bytes | **100.0000 %** of 4 811 chars, 0 slips |
| We call it (originate role) | **100.000 %** of 3 761 bytes | **100.0000 %** of 4 833 chars, 0 slips |
| Through the DTE (`ATA`…`ATH`) | **100.0000 %** of 1 550 bytes at the DTE | **100.0000 %** of 1 821 chars, 0 slips |

The DTE is told the rate: `CONNECT 1200`, not `CONNECT 2400`.

And the regression that matters most — a V.22**bis** peer must not be dragged down
by the new branch. With `AT+MS=V22B` the answerer still selects 2400, reports
`2400 bit/s (4 bits/symbol)`, and scores **100.000 %** of 8 013 bytes out and
**100.0000 %** of 7 496 characters in. The test suite asserts it too: with a
V.22bis caller, circuit 112 is never dropped and circuit 109 never comes on.

One observation worth recording: at 1200 bit/s with 12-bit framing the line
carries **100 characters a second**, and the Pi-side and DTE-side scripts pace at
about 190. So they are pushed back rather than throttled — the modem stops reading
its pty once the transmit queue fills, and `rtscts` does the same on the hardware
side. Nothing is corrupted by it (0 slips throughout), but the byte counts on the
sending side are much larger than what crosses the line.

### Tests

`test_originate.py` is now 31 checks: the 2400 bit/s path and its tolerances as
before, plus the fallback timings against §6.3.1.2, the constellation switch, data
both ways at 1200, and three checks that the 2400 path is *not* taken by accident.

## Not yet done

- **V.42/LAPM** is left disabled. With it enabled the line carries LAPM frames rather than start-stop
  characters, which is a different decode problem entirely.
- **Hardware modem-control leads.** The pty carries no DCD/CTS/DSR; see the limitation noted above.
  A real serial port, or a USB-serial gadget, would be needed to drive circuit 109 physically.
- **Two calls at once.** One RTP socket, one state machine, one pty: the modem is a single line.
- **V.32.** Nothing on this list any more: modulation, coding, both 9600 alternatives, the
  scramblers, §5.4 start-up, §5.5 retrain, trellis coding with a Viterbi decoder, and §7's V.14
  conversion are all built, and characters cross a real connection to both hardware modems in both
  directions. See `v32-ans-path.md`, which is now the long version of that story.
