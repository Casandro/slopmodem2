# Echo cancellation

`v32-ans-path.md` ends with V.32 unable to hold 9600 with trellis coding against
either modem, and `v42-error-correction.md` works out why: the FRITZ!Box's
analogue hybrid returns our own signal 9.6 ms later at about 19 dB down, so our
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

**And the delay is a property of the call, not of the rig.** The first
measurement gave 9.6 ms and the range was tuned around it, 8 to 200 samples.
Four consecutive calls then measured **77, 205, 461 and 525 samples** — 9.6,
25.6, 57.6 and 65.6 ms — because the loop includes the FRITZ!Box's jitter buffer
and whatever frame alignment the call happens to settle on.

The first tuned range missed the second call entirely, finding a weaker unrelated
peak at lag 45 and locking onto nothing useful. The second guess, 500 samples,
was cleared by 461 with 8% to spare — and then the very next call came in at 525
and would have been missed too. The range now runs to 800 samples, 100 ms.
Widening costs threshold margin only as sqrt(ln L) — 0.0495 to 0.0514 for double
the coverage — while guessing the range wrong costs the whole feature. Tuning a
search to one measurement of a quantity that varies per call is a mistake that
shows up on the *next* call, which is why it was made twice here.

**A constant delay, always.** The canceller runs one frame behind whether it is
adapting or not. Switching a delay on at lock would splice the sample stream, and
this codebase has paid for that twice already — the equaliser is frozen rather
than starved, and the V.14 framer is gated on its output rather than its input,
both for this reason.

## Stability when there is no echo

This is the property that decides whether a canceller is worth having, so it is
the one built for first.

**It does nothing until it has found an echo.** No lock, no adaptation, not one
tap moves; the output is the input, sample for sample. That is not a happy
accident of convergence, it is the default state.

**The threshold is a statistical claim, so it is derived rather than guessed.**
The first version compared the correlation peak against the median across lags and
demanded a factor of four. It locked immediately on an echo-free line at
rho = 0.131 — because the maximum of many noisy estimates *is* several times the
median by construction. For L lags and N terms the largest spurious peak is about
sqrt(2 ln L / N), which for 592 lags and 1024 terms is 0.11: the observed value,
almost exactly. The threshold is now `1.8 * sqrt(2 ln L / N)`, and the search
range was cut from 75 ms to 25 ms because every lag not searched buys margin.
With 792 lags and 16384 terms the null is 0.0285 and the threshold 0.0514,
against the 0.115 to 0.135 a real echo produced on the line — and the strongest
unrelated peak in that same call measured 0.042, below the threshold.

**And two scans must agree** on the lag unless the correlation is unambiguous —
either rho ≥ 0.5, which is what single talk gives, or twice the threshold, at
which point a spurious peak would need 3.6 times the null and 16384 terms make
that impossible. Chance does not repeat a lag either.

Tested against six independent echo-free signal pairs: no locks, peak rho 0.044
against a threshold of 0.064. One clean run would be luck; six is a property.

**Leakage** pulls the taps towards zero on a 60 s time constant, so a filter whose
echo has gone decays instead of injecting a stale estimate.

**A divergence guard** compares output power against input power over one-second
windows. Note what this can and cannot do: with the far end talking 19 dB above
the echo, both are dominated by the far end and the ratio sits at 0 dB even when
cancellation is perfect. It is a divergence detector, not a performance meter —
it fires when the filter is *injecting* noise, which is the failure that matters.

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

## What it does not do

It cancels one bulk-delayed reflection. The rig's secondary reflections at 61 and
79 ms are 29 dB down and are left alone. There is no double-talk detector: the
step size is simply chosen to survive permanent double talk, which is cheaper and
has no failure mode of its own. And a moving echo delay would need a re-search,
which only happens today if the guard notices the filter hurting.
