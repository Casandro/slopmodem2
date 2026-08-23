# Echo cancellation

`v32-ans-path.md` ends with V.32 unable to hold 9600 with trellis coding against
either modem, and `v42-error-correction.md` works out why: the FRITZ!Box's
analogue hybrid returns our own signal 50 to 66 ms later at about 19 dB down, so our
transmit level sets our own receive noise floor. Turn it up and the far end can
decode us but we cannot decode it; turn it down and the reverse. A 32-point
constellation has 10 dB less minimum distance than QPSK at the same power and no
margin to spare either way.

Every real V.32 modem carries an echo canceller for exactly this reason — V.32 is
full duplex on a two-wire circuit — and V.8's ANSam phase reversals exist to
switch off the *network's* canceller precisely because the modems do it
themselves. This is ours: `softmodem/echo.py`.

## The shape of it

We know exactly what we transmitted, so the echo is a linear function of a signal
we hold. An adaptive FIR filter estimates that function and subtracts it. What
takes the care is everything around the filter.

**A bulk delay, then a short filter.** Covering 0 to 25 ms at 8 kHz would be 200
taps, and 200 taps times two operations times 8000 samples a second is beyond a
pure-Python budget. Instead the delay is *found* by cross-correlation and a
32-tap window is placed over it — 512k operations a second, affordable. The
hybrid's response is not a pure impulse, so the window starts a quarter of a span
early.

**The delay must be searched at full resolution.** The correlation between two
V.32 signals oscillates at the 1800 Hz carrier, so it has a period of 4.4
samples: a coarse lag grid aliases and can miss the peak completely.

**The delay cannot be under one RTP frame.** Our frame *k* is emitted only after
inbound frame *k* has been read, and whatever the box reflects has to be
packetised before it returns, so the earliest our own signal can reappear is in
inbound frame *k+1*. That is a property of the loop, not of the network, and it
has two consequences.

The first is that **nothing has to be held back.** To cancel inbound sample *j* we
need `tx[j - bulk]`; with `bulk >= 160` the newest sample required is `160k - 1`,
which is exactly what has been pushed by the time inbound frame *k* arrives. An
earlier version held one frame to be safe, which cost 48 T of latency and broke
5.4.2's turnaround (below). There was never anything to hold back for.

The second is that **a lag below one frame is not a short echo, it is noise.**
The search starts at 160 samples. The first version started at 8 and duly found a
strong peak at 77 samples — 9.6 ms — which would have the echo arriving before it
was transmitted, and which was then written into this document and used to justify
a search range. Fitting the filter to such a peak can only add to the residual.

**Where 9.6 ms came from.** Not the line: the capture files. `rtp.pump` emits a
priming burst of two frames before any inbound exists, and appends every emit to
`out_audio` while `in_audio` grows only when a packet arrives — the run logs say
`in=3018 out=3020`. So the transmit file leads the receive file by 320 samples and
every lag taken from those files was that much too small. The canceller's own scan
log was never affected, because it is fed inside the frame loop where its two
streams advance together.

Corrected, the delays observed are **397, 461, 493 and 525 samples — 50 to 66 ms**,
which is a far tighter spread than the 9.6-to-66 ms range this document previously
claimed. The variation between calls is real but modest; the apparent wildness was
two coordinate systems being averaged together.

Measured on the Cirrus's port, meanwhile, there is **no echo at all**: two runs,
best correlation 0.036 against a 0.051 threshold, no consistent lag. The
reflection happens at the hybrid the far modem is plugged into, so it is a
property of the port and not of the box.

## What it costs

One lag against a 16384-term window is about 16 000 multiplies, roughly 15% of a
20 ms frame in pure Python, so the scan takes one lag per frame and a full sweep
of 492 lags runs for ten seconds. That is slow, and it is the right trade: the
RTP pump has a hard deadline and the search does not. Correctness of the estimate
does not depend on finishing quickly; the pump's does.

The transmit-side normaliser is computed once per snapshot rather than once per
lag, which halves the inner loop and changes nothing — over a stationary window a
one-sample shift does not move the energy, and comparisons between lags are
unaffected either way.

Replayed against a real 70-second call, it locks at 9.1 s with its taps covering
165 to 196, which contains the true peak at 173.

## The step size is set by double talk

On a full duplex circuit the far end is always in the error term, and here it
arrives about 19 dB *above* the echo. NLMS misadjustment goes as the step times
that ratio, so the step has to be small: mu = 1e-3 predicts about −14 dB and
measures −12.5 dB. A large step tracks the far end's data instead of the echo.
The cost is a convergence time near span/mu = 32 000 samples, four seconds, so
there are two speeds — 1e-2 for the first second after locking, then 1e-3.

| | ERLE |
|---|---|
| double talk, near end 19 dB above the echo | **12.5 dB** |
| far end quiet | **21.3 dB** |

12.5 dB is about twice the ~6 dB the 9600 trellis problem needs.

## The bug that cost the most to find

Locking worked, and then the filter diverged on the very sample it was switched
on, tripped the divergence guard, unlocked, searched again, locked again — a loop
that looked exactly like a search unable to make up its mind, and printed
`bulk None` at the end as if it had never found anything.

The NLMS normalisation is the power in the tap window, kept as a running sum so
the update stays O(1). It was initialised to **zero** at lock and then
immediately had the outgoing sample's energy subtracted, so it clamped at zero
and the divisor fell back to its epsilon — turning the first update into an
enormous one. Priming the sum properly at lock fixes it. The lesson is the
familiar one: an incremental accumulator has to be *primed*, not merely zeroed,
and the symptom appeared three layers away from the cause.

## Measured, in the loop

Soft to soft, 9600 with trellis coding and V.42 carrying 6000 octets each way,
with a 13 dB echo injected in both directions — harsher than the rig's 19 dB:

| | completes | frames discarded on FCS |
|---|---|---|
| echo, canceller off | 27.8 s | 56 and 64 |
| echo, canceller on | **19.3 s** | **4 and 13** |
| no echo, canceller off | 10.2 s | none |
| no echo, canceller on | 10.8 s | none |

The 0.6 s the canceller costs on a clean line is the one frame of hold, and the
data is identical.

## On the line

At −24 dBFS the Conexant cannot decode our 32-point constellation and retrains
away from 9600 within about 14 s of the data phase opening. At −18 dBFS it can,
but without cancellation our own echo takes our receiver apart. With the
canceller, at −18 dBFS:

| | eye within 0.35 | median error | V.32 retrains | throughput |
|---|---|---|---|---|
| no canceller | 34.8% | 0.431 | 0, but 172 frames lost to FCS | 197 bit/s (2%) |
| **canceller** | **100%** | **0.089** | **0** | **7539 bit/s (79%)** |

It locked on its second scan at lag 461, 57.6 ms, and held 9600 with trellis
coding for the whole call — `retrains 0`, 532 frames with 34 discarded, and V.42
carrying 63 744 octets at 79% of the channel. That combination had not been
achievable on this rig at any transmit level.

The next call locked at lag 525 and produced the cleanest eye measured on this
rig — median error 0.042 against a decision boundary at 0.707, 100% within 0.35,
2 discarded frames out of 325 — but retrained once and finished at 4800 and 84%.
The forward direction is still marginal at 9600 from call to call; what the
canceller fixes is our own receiver, and there it is unambiguous.

## The delay is not free: 5.4.2's 64 T turnaround

Holding a frame to make the echo causally cancellable has a cost that took a
while to surface, because it only bites during the handshake and only against one
of the two modems.

5.4.1 and 5.4.2 pin the turnaround after a detected phase reversal at **64 ± 2
symbol intervals**, and they measure it at the line: "the time delay between the
reception of this phase reversal at the line terminals and the transmitted CA to
AC transition appearing at the line terminals shall be 64 ± 2 symbol periods".
The canceller's one-frame hold puts 160 samples — 48 T — between the line and the
state machine. Scheduling 64 T of pad *after* a detection that is already 48 T
late produces about 112 T on the line, against a tolerance of two.

The Conexant tolerates that. The Cirrus does not: it completed its AA, its CC and
its reversal, then ceased transmitting and waited for a handshake that never
arrived on its schedule, and eventually hung up. Three dial-in attempts in a row
stalled in R1TX with the caller silent — the received level sat at −69 dBFS, the
noise floor, from 3.5 s onwards.

The diagnosis came from a number in the log that had been sitting there all
along: MT read **192 T with the canceller off and 240 T with it on**, and the
difference is exactly 48 T. A latency that shows up as a constant offset in a
measured interval is latency in the measurement path.

So the pad is now `64 T - rx_delay_T()`, which is 16 T with the canceller and 64 T
without, and the line sees 64 T either way. The same correction applies to the
call side's AA-to-CC turnaround. With it, the first attempt reached the data phase
at 9600 non-redundant and **90% of the channel**, where the three before it had
stalled.

Worth noting what made this hard to see: the canceller was doing its job, the
protocol logic was right, and the failure was in neither — it was in the *time
reference* the two share. Any latency added between the line and a state machine
has to come out of the intervals the state machine is asked to honour, not be
added to them.

## What it does not do

It cancels one bulk-delayed reflection. The rig's secondary reflections at 61 and
79 ms are 29 dB down and are left alone. There is no double-talk detector: the
step size is simply chosen to survive permanent double talk, which is cheaper and
has no failure mode of its own. And a moving echo delay would need a re-search,
which only happens today if the guard notices the filter hurting.

## Transparency before lock, which it did not have

With `HOLD = 0` the pad correction above is moot — `rx_delay_T()` returns 0 and the
turn-round is a plain 64 T again — but the Cirrus dial-in was still failing with
the canceller in the path. Eight interleaved 45-second runs, four per arm:

| | reached DATA | stalled in R3TX | dial failed |
|---|---|---|---|
| canceller off | 3 | 1 | 0 |
| canceller on | 1 | 2 | 1 |

The canceller-off arm *also* stalled, which is what made the stall findable — it
was a separate bug, in `_start_r3`, written up in `v32-ans-path.md`. But after
fixing that the arms separated cleanly, 3/3 against 0/3, while the canceller
reported `|w|max 0.0000` and `0 resets`: it had never locked, never adapted, and
by construction was passing every sample through untouched. A filter of all zeros
cannot change a handshake, so something else about having it in the path did.

It was not the filter. It was the padding around it. `step()` pads a short inbound
frame up to a whole frame before calling `feed()`, because the canceller lines its
two streams up by absolute sample index and a frame with no inbound audio would
otherwise advance transmit without advancing receive — a silent, permanent offset
that simply stops the search finding anything. That part is right. What was wrong
is that the padded frame was then passed on to the receiver.

`rtp.pump` primes two frames before any inbound exists. So with the canceller in
the path the receiver was fed **320 samples of invented silence** that the
canceller-off path never sees, and every received symbol index came out 96 T out
of step with our own transmit clock — across a turn-round that §5.4 specifies as
64 ± 2 T. The fix is one line: slice the invented samples off `feed()`'s return
before anyone looks at them. The canceller keeps the zeros internally, where they
belong, and the receiver sees exactly what arrived.

An offline A/B on a captured call now reports **identical event logs and zero
differing transmitted samples** between canceller-off and canceller-on, which is
the property that should have been asserted from the start: before it locks, the
canceller must be provably invisible.

The same experiment measures the cost: **0.98 ms per frame without, 2.36 ms with**,
worst case 9.8 ms against a 20 ms budget. The scan is most of that, and it ran on
every frame of the handshake.

## The scan does not belong in the handshake

Transparency was necessary and not sufficient. With the samples proven identical
the arms still separated:

| | reached DATA |
|---|---|
| canceller off | 9 / 9 |
| canceller on, scanning | 1 / 9 |
| canceller on, `--echo-budget 0` | 4 / 4 |

Scanning against not scanning is p ≈ 0.003. Since the filter never locked in any
of those calls and the sample streams were bit-identical, the only thing the scan
could have been spending was time — and it could not have earned it back. A full
pass is 640 lags at one lag per frame: **12.8 s, longer than the whole
handshake**. It never reaches a verdict before the phase it is running in has
ended, and start-up is the part of the call with the tightest timing — §5.4's
64 ± 2 T turnarounds, and signal E's eight-symbol window.

Correlating start-up would be a poor bargain even if it were free. A
cross-correlation delay estimate wants one long stationary stretch; §5.4 is a
dozen short segments — answer tone, AC, CA, S, S-bar, TRN, rate sequences — each
with its own spectrum. The data signal is scrambled and stationary, which is what
the estimator was built for.

So the search now runs only in the data phase. `defer_search()` drops any
part-finished scan and sets a floor on how old a sample may be to enter a
correlation, so the first data-phase scan is not half made of handshake tones; the
state machine calls it whenever the state is not `DATA`, which also re-suspends it
across a retrain.

Seven interleaved pairs afterwards:

| | reached DATA |
|---|---|
| canceller off | 4 / 7 |
| canceller on | 6 / 7 |

Indistinguishable (p = 0.56), where before the gate it was 9/9 against 1/9. The
canceller is not disabled by this, only deferred: those calls still completed
three searches each inside the data phase. And the residual failures are now
symmetric — whatever the remaining third is, both arms have it, which is what a
line problem looks like and what a code problem does not.

The general lesson is the one this rig keeps teaching in different costumes: a
measurement that cannot complete inside the window it is measuring is not a
cheap measurement, it is a pure cost.
