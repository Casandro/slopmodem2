# V.32: a soft modem, from the ANS path to two interoperating modems

What this document became is not what it set out to be. It began as a note on one
decision — send plain ANS, because the FRITZ!Box destroys ANSam — and grew into the
record of implementing V.32 whole: the modulation, both 9600 bit/s alternatives,
the start-up, the retrain, and the asynchronous conversion, ending with characters
crossing a real connection to two different hardware modems.

## Where it got to

| | |
|---|---|
| §2.4 coding | both 9600 alternatives, 4800, and the trellis code |
| §4 scramblers | GPC/GPA, allocated per §4.1.1 |
| §5.2, §5.3 | conditioning signal, rate signals, signal E |
| §5.4 start-up | both roles, end to end |
| §5.5 retrain | both roles, plus a self-triggered one |
| §7, V.14 | start-stop conversion both ways, Table 8 conformant |
| trellis coding | constellation, Table 2, Table 3, the 8-state nonlinear encoder, a Viterbi decoder |

Against hardware, in both directions, at 9600 bit/s:

- **Conexant** — `CONNECT 9600`, trellis coded: 482 repeats of our pattern at its
  DTE, 1791 characters recovered from it, 0 retrains.
- **Cirrus** — nonredundant: 700 repeats of our pattern in a 7000-character
  unbroken run, 551 characters back.
- 75 seconds of continuous data phase with **0 retrains** and **100.00%** of
  640 432 bits correct, which is `rules.md`'s minute in one direction, and
  characters both ways in the runs above.

Measured coding gain of the recovered trellis code: **3.98 dB** asymptotic,
**+2.97 dB** end to end at a bit error rate of 10⁻⁵.

`softmodem/v32.py`, `v32fsm.py` and the shared `tracking.py` are about 3300 lines;
`test_v32.py` and `test_v32start.py` run 161 checks between them, with no known
failures.

## How to read it

The body is **chronological**, and deliberately so: most of what is worth having
here is not the final code but the wrong turns, and a wrong turn only makes sense
in the order it happened. Sections that were snapshots at the time say so and point
forward. If you want particular things:

- **the trellis code, reverse-engineered** — "Figure 3/V.32, recovered and then
  confirmed" through "The encoder, read off the page"
- **the six threshold bugs hardware found and offline testing could not** — "Six
  bugs, none of which offline testing could have found"
- **the defect that was one frame long** — "The rate-signal eye, and one frame"
- **the conformance audits** — "The transmitter, audited against the
  Recommendation" and "The receiver, audited the same way"
- **why none of it was our fault in the end** — "It was V.42" and "The bridge test
  settles it"
- **the lessons, collected** — the last section

Companion to `v8-negotiation.md`, which establishes why V.8 is unavailable here:
the FRITZ!Box regenerates any tone near 2100 Hz as a clean unmodulated sine, so
ANSam arrives as plain ANS and the V.8 capability flag never crosses.

## The decision this document started from

**Send plain ANS and take the branch V.8 §8.1.1 prescribes for it** — "proceed in
accordance with Annex A/V.32 bis". That caps the link at **14.4 kbit/s** (V.32bis)
instead of the 33.6 k the hardware could otherwise reach, and gives up the whole
CM/JM menu: automatic modulation selection, call function, protocol category, PSTN
access and PCM-modem availability. In exchange it needs no signalling the network
destroys.

## The start-up signals

V.32 Table 4 defines the network-interaction signals as alternations of start-up carrier states,
where A and C are antipodal points of the constellation:

| Signal | Definition | Spectrum |
|---|---|---|
| `AA` | repetitive state A | pure 1800 Hz |
| `AC` | states ACAC..AC | 600 Hz + 3000 Hz, carrier suppressed |
| `CA` | states CACA..CA | same, opposite phase |
| `CC` | repetitive state C | 1800 Hz, antiphase to AA |

Alternating antipodal states at the 2400 baud symbol rate is a 180° phase reversal every symbol,
which puts the energy at 1800 ± 1200 Hz and suppresses the carrier — hence V.32 §5.4.2's reference to
"the three 200 Hz bands centred at 600 Hz, 1800 Hz and 3000 Hz". Verified on the generated signals:

| Signal | 600 Hz | 1800 Hz | 3000 Hz |
|---|---|---|---|
| AA | 0.000 | **1.000** | 0.000 |
| AC | **0.409** | 0.049 | **0.409** |
| CC | 0.000 | **1.000** | 0.000 |

`v32.py` generates these. The symbol rate is 2400 baud against an 8 kHz sample clock — 3⅓ samples
per symbol — so the symbol clock uses a fractional accumulator.

## The exchange, as it happens on the rig

V.32 §A.2.2 and §5.4.2 give the answer side; §5.4.1 gives the caller. `fsm.AnswerV32` implements the
answer side as far as the network-interaction phase goes:

```
[ 0.500] SILENCE  transmitting plain ANS (2100 Hz, no AM)
[ 1.800] ANS      AA detected (1800 Hz, purity 1.00, -25 dBFS) - caller took the
                  Annex A/V.32bis branch
[ 1.860] AC       128 symbols of AC sent and 1800 Hz held - switching to CA
[ 1.900] CA       phase reversal in incoming tone (2.34 rad) - caller switched
                  AA->CC; reverting to AC after 64 symbols
[ 1.940] CADELAY  -> AC2 (64-symbol delay elapsed)
[ 7.960] AC2      caller did not cease transmitting - it is waiting for the S
                  sequence, which is not implemented
```

Reading that against the specification:

- The caller hears plain ANS and transmits `AA`, exactly as §5.4.1 says. Confirmed independently in
  the captured audio: 1800 Hz at purity 0.98, with 600 Hz and 3000 Hz energy at 0.000–0.004, so it
  is `AA` and not `AC`.
- We transmit `AC` for ≥128 symbol intervals, then switch to `CA` (§5.4.2).
- §5.4.1 has the caller watch for "one of two incoming tones at frequencies 600 ± 7 Hz and
  3000 ± 7 Hz, and subsequently ... a phase reversal in that tone", and on seeing one, switch from
  `AA` to `CC` after 64 ± 2 symbol periods. **That transition is observable**: the captured 1800 Hz
  carries exactly one 180° phase step in 24 seconds, at t = 1.847 s, coincident with our AC→CA
  switch. `CC` is also pure 1800 Hz, so purity cannot distinguish it from `AA` — only phase can.
- We detect that reversal and revert to `AC` after the specified 64-symbol delay, which is the
  second reversal the caller is waiting for.
- The caller should then cease transmitting and wait for our `S` sequence. It never ceases: it holds
  1800 Hz for about 24 s and gives up. That is expected — the receiver-conditioning signal is not
  implemented, so there is nothing for it to train on.

## Measuring phase without fooling yourself

A 160-sample frame is exactly 36 cycles of 1800 Hz at 8 kHz, so phases measured frame to frame are
directly comparable and a 180° step appears as one. Any other window stride does not work: an early
attempt using a 16-sample step reported a ~144° "jump" on every single measurement, which was purely
the carrier advancing 0.6 of a cycle between windows. If a phase measurement shows a constant step
everywhere, it is measuring its own stride.

## What can be negotiated with plain ANS

Two separate questions, worth not conflating.

### 1. What the automode procedure selects unattended

V.32bis Annex A defines the automode set precisely: "Modems conforming with Recommendations V.22
(operating at 1200 bit/s only), V.22 bis, V.32 and V.32 bis could interwork with a dedicated
automode modem". Nothing else is in it.

The answer side discriminates as follows (V.32 A.2.2): transmit the V.25 answer sequence and listen
for `AA`. If `AA` arrives, go to V.32/V.32bis. If it does not, transmit `USB1` and listen in the low
band for `S1` (→ V.22bis) or `SB1` (→ V.22); if neither appears within Ta = 1500 ± 50 ms, fall
through to V.32 anyway.

| Mode | Rates | How it is reached with plain ANS | Both modems? |
|---|---|---|---|
| **V.32bis** | 14400, 12000, 9600, 7200 trellis; 4800 uncoded | `AA` detected, then R1/R2/R3 rate sequences | yes |
| **V.32** | 9600 trellis or 16-state uncoded; 4800 | same path, rate signals indicate V.32 only | yes |
| **V.22bis** | 2400 (1200 fallback) | no `AA`; `S1` seen in the low band | yes |
| **V.22** | 1200 | no `AA`; `SB1` seen in the low band | yes |

Within V.32/V.32bis the rate is not chosen by automode but by the rate-sequence exchange (V.32 §5.3,
Table 6), which the answer side drives: B4 = can receive 2400, B5 = 4800, B6 = 9600, B8 = trellis
available at the highest rate indicated, and per Note 1 "the combination of B4 equal one and B8 equal
one indicates V.32 bis operation", with B9–B14 extending it to the other V.32bis rates. We send R1,
the caller answers R2, and we select with R3 — so as answerer **we** pick the final rate, up to
14400.

**Ceiling: 14400 bit/s.** V.34 is unreachable because V.34 §11.1.1.1 mandates the V.8 procedure and
§11.1.1.3 sends a caller that heard plain ANS to Annex A instead; V.90/V.92 build on V.34's start-up
and additionally need a digital PCM endpoint, which this rig does not have (see `modem.md`).

### 2. What we can force, since we control both ends

Automode only matters for reaching agreement *without* configuring the far end. We own the hardware
modem over AT, so any mode both sides implement can simply be forced with `AT+MS=`. That widens the
list considerably, and puts the cheapest option first:

| Mode | Rate | In automode set? | Supported by | What we would have to build |
|---|---|---|---|---|
| **V.21** | 300 | no — force with `AT+MS=V21` | both | **already done** (`v21.py`) |
| V.23 | 1200/75 | no | both | FSK, asymmetric channels — small step from V.21 |
| Bell 103 | 300 | no — `ATB1` | Conexant only | FSK, same shape as V.21 |
| Bell 212A | 1200 | no — `ATB1` | Conexant only | 600 baud DPSK |
| **V.22** | 1200 | yes | both | 600 baud DPSK, guard tone — **core built and tested**, see `v22-modem.md` |
| V.22bis | 2400 | yes | both | 600 baud 16-QAM — blocked on the constellation figure, see `v22-modem.md` |
| V.32 | 4800 / 9600 | yes | both | 2400 baud, trellis coding, echo canceller, equaliser |
| V.32bis | up to 14400 | yes | both | as V.32 plus larger constellations and rate signalling |

The modem capability column comes from the `+MS=?` lists in `modem.md`: both units support V.21,
V.22, V.22bis, V.23, V.32 and V.32bis; only the Conexant offers Bell 103/212A, and those are selected
with `ATB1` rather than through `+MS`.

**The practical consequence is that V.21 at 300 bit/s is by far the cheapest route to a working data
phase**, because `v21.py` already does 300 bit/s FSK in both directions — it was built for the V.8
CM/JM exchange. It is not in the automode set, but that does not matter when we can put the far end
into V.21 with one AT command. V.23 is the next smallest step. Everything from V.22 upwards needs a
QAM/DPSK modem with carrier and timing recovery, and from V.32 upwards an echo canceller and adaptive
equaliser as well.

## What was not implemented when this section was written

Everything after the network-interaction phase: `S`/`SBAR` and `TRN` (§5.2), the rate signals and
their 16-bit coding (§5.3), and the data phase itself. So the work above reaches the first genuine
exchange of the V.32 handshake and stops.

**All of that except §5.4's sequencing is built now — see the last section of this document.** The
constellation, both scramblers, the 2400 baud modulator, a receiver that decodes 9600 and 4800 bit/s
at 100 %, the §5.2 conditioning signals and the §5.3 rate signals all exist and are tested. The echo
canceller turned out not to be needed on this rig, which is a measurement rather than a hope: 31.9 dB
of echo return loss. What remains is the start-up state machine and trellis coding.

Known deviation: `AC`/`CA` are generated with rectangular symbols and no V.32 spectral shaping, which
puts 18 % of the power in sidelobes rather than the 600/3000 Hz pair. It was evidently good enough
for the caller to lock onto and reverse, but it is not conformant.

## V.32 proper: modulation, coding and the data phase

The sections above reach V.32's network-interaction phase and stop. This section
is the modem: §2 (line signals and coding), §4 (scramblers) and §5.2–5.3 (the
receiver conditioning and rate signals), with a data phase that decodes at both
rates. `softmodem/v32.py` and `softmodem/test_v32.py`.

### The question that had to be answered first

V.32 is the first modulation on this rig that puts **both directions in the same
band** — 2400 baud on an 1800 Hz carrier, no frequency-division split. That is
why a real V.32 modem contains an echo canceller: on a two-wire circuit its own
transmitter lands on top of the far signal. Whether *this* rig needs one is a
measurement, not an assumption, and it decides whether the rest is a week's work
or a month's.

`echo_probe.py` answers a call, transmits a real V.32 data signal, and
cross-correlates the received stream against its own transmission — the right
tool, because the far end's signal is uncorrelated with ours. Measured over 20 s:

| | |
|---|---|
| transmitted | −29.2 dBFS |
| received | **−72.2 dBFS** |
| peak cross-correlation | 0.0255 at 12 samples (1.5 ms) |
| **echo return loss** | **31.9 dB** |

So the FRITZ!Box gives us what is effectively a four-wire path: our own signal
comes back 43 dB down in level, and the correlated part is 32 dB down. **No echo
canceller is needed here.** That is a property of this rig, not of V.32 — the
same code on a real two-wire line would need one.

### The constellation, which cannot simply be read

Table 3/V.32 lists the coordinates, but the PDF's OCR strips every sign from it:
it prints "−0" and repeats "−1 −1" for states that must differ. Figure 1's
*labels* survive, though, and its geometry is unambiguous — a 4×4 grid with axis
ticks at ±2, so the points are at odd coordinates:

```
Im=+3:  1011  1001  1110  1111
Im=+1:  1010  1000  1100  1101
Im=-1:  0001  0000  0100  0110
Im=-3:  0011  0010  0101  0111        columns Re = -3, -1, +1, +3
```

with the note that the binary numbers are Y1 Y2 Q3 Q4. That reading is then
checked four ways, all asserted in the test suite:

1. 16 distinct points, and the four sharing a Y1Y2 all fall in one quadrant.
2. A 90° rotation advances Y1Y2 and leaves Q3Q4 alone — the same label
   invariance V.22bis's Figure 2 has.
3. Table 1's Y outputs, transcribed independently, turn out to be **exactly** the
   rotations its phase column names: 00 → +90°, 01 → 0°, 10 → +180°, 11 → +270°.
   (That column reads "+190º" in the scan for two of the four blocks; the Y
   values are clean and settle it.)
4. All four rows of Table 3 match by **magnitude**, which the OCR did preserve.

§2.4.2's 4800 bit/s subset then falls out as A = (−1,−1), B = (1,−1), C = (1,1),
D = (−1,1) — and the 16 points are *the same set* V.22bis uses, differently
labelled.

### The scramblers, and a gift from §5.2.3

§4 gives V.32 **two** scramblers where V.22bis has one: GPC = 1 + x⁻¹⁸ + x⁻²³ for
the calling modem, GPA = 1 + x⁻⁵ + x⁻²³ for the answering one, each descrambling
with the other's polynomial (§4.1.1). Assuming V.22bis's arrangement here would
give a link that scrambles correctly and descrambles to noise.

§5.2.3 then publishes the first fifteen dibits *and* signal states of the TRN
segment for both polynomials. That is a ready-made test vector, and both
reproduce exactly:

```
GPC  11 11 11 11 11 11 11 11 11 00 00 01 11 11 11   C C C C C C C C C A A A C C C
GPA  11 11 10 00 00 11 11 10 00 00 11 10 01 11 11   C C C A A C C C A A C C A C C
```

One check covering the polynomials, the register convention, the bit order and
the A/C mapping rule at once. There is no 64-ones guard: §4 does not specify one,
and V.22's exists to avoid instigating a remote loop 2 that V.32 does not have.

### The arithmetic lines up again

```
160 samples (one RTP frame) = 48 symbols at 2400 baud
                            = 4 periods of the 1800 Hz carrier table
                              (1800/8000 repeats exactly every 40 samples)
and 48 symbols = 192 bits at 9600 bit/s, or 96 at 4800.
```

So the same frame-boundary continuity argument that let V.22bis cut a
pre-rendered stream short and continue it on demand holds here too.

### Where 2400 baud bites: the pulse has to be placed exactly

The receiver was parameterised by symbol rate rather than duplicated — the
polyphase matched filter, the timing loop and the frequency search all take
`sps` and `baud` now, defaulting to V.22's, and all six older suites still pass.
But the first V.32 link produced a badly smeared constellation: median distance
0.335 from the lattice at a known-good sampling phase, against 0.05 for V.22bis.

That was ISI put there by the transmitter. Rounding each symbol's centre to the
nearest sample costs up to half a sample of timing error, which is 3.75 % of a
symbol at V.22's 13.333 samples per symbol — measured harmless there, and
documented as such — but **15 %** at V.32's 3.333.

The fix is exact, not approximate, because the fractions are: SPS is 10/3, so
symbol *k* sits at 10k/3 samples and its fractional part is (10k mod 3)/3, which
is 0, ⅓ or ⅔ for ever. A three-phase tap table is therefore not an approximation
to the ideal filter — it *is* the ideal filter. With it:

| | m4 concentration | median distance to the lattice |
|---|---|---|
| rounded to nearest sample | 0.4905 | 0.335 |
| exact three-phase | **0.5151** (ideal 0.5152) | **0.038** |

### Results

| | 9600 bit/s (16-point nonredundant) | 4800 bit/s (ABCD subset) |
|---|---|---|
| Characters | 23 292 | 11 644 |
| Correct | **100.0000 %** | **100.0000 %** |
| Framing errors | 0 | 0 |
| Acquired at symbol | 600 | 600 |
| Receiver cost | 0.61 ms/frame | 0.53 ms/frame |

Noise: 9600 bit/s is error-free down to **18 dB SNR** against measured signal
power and fails to acquire at 15 — a sharp cliff rather than a slope, and close
to the ~17 dB that 4 bits/symbol needs in theory. The 4800 bit/s subset is
constant modulus, so its CMA dispersion floor is 0 rather than 42.24.

Transmitted spectrum per §2.2: 600 Hz down 4.8 dB and 3000 Hz down 4.6 dB against
the maximum between them, both inside the 4.5 ± 2.5 dB window. Level: the label
follows `v22.Mod`'s convention (actual RMS plus 10·log₁₀(SPS)), so V.32 at −24
puts −29.25 dBFS on the wire — the same power as the V.22bis runs at −18, which
is what both hardware modems locked onto.

### §5.2 and §5.3 signals

Implemented and asserted: segment 1 (256 alternations of A and B), segment 2 (16
of C and D), TRN (§5.2.3, A/C from the first bit of each dibit for 256 symbols
then Table 5 direct, differential coding disabled), the 16-bit rate sequence
(Table 6) and signal E (Table 7). Table 6's fixed bits are recoverable despite
the OCR: B0–B3 = 0000, B7 = B11 = B15 = 1, with B4–B6 the receive-rate
capabilities, B8 trellis availability, and B9–B14 = 001000 for "no special
operational modes". Signal E is the same shape with B0–B3 = 1111. The seven
fixed bits are the sync pattern of §5.3.1, and flipping any one of them is
rejected.

### Not built yet, at this point in the story

- **§5.4's start-up sequencing.** Built next; see the following section.
- **Trellis coding** (§2.4.1.2), the other 9600 bit/s alternative: the
  convolutional encoder of Figure 2, the 32-point Figure 3 mapping and a Viterbi
  decoder. "Table 3's trellis column is the part of the OCR that is beyond repair,
  so those coordinates would have to come from elsewhere" — which was true of the
  OCR and wrong about the document. All of it was eventually read straight off the
  rendered page. That took a long detour first, and the detour is the interesting
  part.
- **V.32bis** rates (7200, 12000, 14400), which Note 1 to Table 6 reaches by
  B4 = 1 together with B8 = 1. Still not built.

## §5.4 start-up: both sides, end to end

`softmodem/v32fsm.py` and `softmodem/test_v32start.py`. The two state machines
run against each other frame by frame, each hearing only what the other
transmitted, and **both reach the data phase at 9600 bit/s in 3.36 s** with
circuits 107 and 109 asserted.

```
ans  [ 0.600] ANS     V.25 answer sequence done            -> AC
ans  [ 0.680] AC1     1800 Hz held 96T after 144T of AC    -> CA, timer on
call [ 0.700] AA      first phase reversal                 -> CC in 64T
ans  [ 0.700] CA      reversal; MT = 48T                   -> back to AC in 64T
call [ 0.740] CC      second phase reversal; NT = 96T      -> cease transmitting
ans  [ 0.800] AC2     amplitude drop -> 16T silence        -> S, S-bar, TRN
ans  [ 1.440] RC1     -> R1
call [ 1.480] WAITS   R1 received: [4800, 9600]            -> S for NT
call [ 2.160] RC1     107 on; R2 offering [4800, 9600]
ans  [ 2.200] HUNT2   R2 received                          -> S, S-bar, TRN
ans  [ 2.840] RC2     R3: selecting 9600 from [4800, 9600]
call [ 2.880] R2TX    R3 received: 9600                    -> E
ans  [ 2.900] R3TX    incoming E: 9600                     -> E, then B1
ans  [ 2.940] B1TX    128T of scrambled ones sent          -> DATA, 106/109
call [ 3.380] B1TX    128T after the incoming E            -> DATA, 106/109
```

Rate negotiation is real, not scripted: an answerer offering only 4800 gets
4800, and so does a caller offering only 4800.

### Locating a phase reversal, and why the obvious way fails

§5.4 hangs the round-trip measurement on phase reversals in a tone, and wants the
turn-round on line 64 ± 2 symbol intervals after one. A symbol is 0.417 ms, so
this needs sub-frame resolution.

Triggering on a **phase jump** does not work. The carrier does turn through 180°,
but a pulse-shaped signal turns gradually: measured across a real AC→CA
transition with a 40-sample window stepped by 5, the largest change between
consecutive windows was **0.8 rad**. A 2 rad threshold never fires, and a
threshold low enough to fire also fires on noise. Three successive attempts —
40-sample steps, stride-compensated overlapping windows, absolute-referenced
phase — all landed at a −7 symbol bias with an 8-symbol spread, which no single
correction fixes.

The **amplitude null** is the right feature. A window straddling a 180° reversal
has its two halves cancelling, so the magnitude collapses — 10002 to 1940 in that
measurement, better than 5:1 — and then recovers. Taking the minimum inside the
dip is symmetric about the event, so it is unbiased, and its resolution is the
step size. Measured over 90 reversal positions: **90/90 found, offsets +3.5 to
+4.5 symbols, a spread of one symbol.** The constant part of that offset is the
far transmitter's pulse-shaping group delay (4.8 symbols for our own shaper),
which no receiver can separate from channel delay and which cancels in the
difference between two reversals — which is what the round-trip estimate is.

Two things had to be added on top:

- **Arming, and making it sticky.** The *onset* of a tone also goes low-to-high,
  so it reads as a reversal; the detector is armed only once the tone is
  established. But gating that frame by frame threw away exactly the frame that
  mattered, because a reversal's own null disturbs the tone test — the second
  turn-round null was missed every time. Arming latches on and only clears on
  silence.
- **A buffer that spans frames.** Without one, a window straddling a frame
  boundary is never evaluated and a reversal landing there is missed outright:
  one case in eight.

### "An even number of symbol intervals" is not decoration

§5.4.2 says AC and CA are each transmitted for an even number of symbol
intervals. That turns out to be load-bearing: AC is A,C,A,C… and CA is C,A,C,A…,
so switching on an **odd** boundary makes the two concatenate seamlessly and
leaves the far end no reversal to find at all. Measured: 90/90 reversals found on
even boundaries, **0 of 3 on odd ones** — not missed, absent.

The same clause resolves how the CA→AC turn-round is built. "After transmitting a
state A, revert to alternate A and C": an even CA segment already ends on A and AC
begins on A, so the two together give exactly one doubled A, which is the
discontinuity. Inserting a further explicit A gives three in a row and no clean
reversal — which is what the first attempt did.

### Four bugs in the rate-signal exchange, each a misread clause

1. **E was never detected.** §5.3.1's "two consecutive identical 16-bit
   sequences" is the minimum for detecting a *rate signal*; signal E is a single
   sequence (§5.3.2). Requiring two meant E never arrived.
2. **…but accepting a lone E while still hunting is worse.** A chance pattern
   inside TRN satisfied the seven sync bits and locked the scanner to the wrong
   phase for the rest of the call. E is only accepted once alignment has been
   established by two identical rate sequences, which is true to the protocol
   because E always follows one.
3. **Alignment must be sticky within a rate signal but not across the gaps.**
   Re-searching bit by bit on every sequence let the scanner slide into E and
   misread it; holding alignment across the silence and TRN between R1 and R3
   left a stale phase and R3 was never seen. Each hunt starts fresh, and stays
   aligned within itself.
4. **The receiver must not switch constellation too early.** The caller switched
   to the 16-point data mode when it sent its own E — and then could not read the
   answerer's E, which is sent at 4800 bit/s like every other rate sequence. It
   now switches on that E, not before.

### The 7 dB step, and where the receiver is built

The whole handshake runs **7 dB below the data phase**, because §5.2 trains on the
A/B/C/D subset whose mean power is 2 against the 16-point set's 10 —
10·log₁₀(10/2) = 7.0 dB, and the measurement agrees (−37.5 dBFS against −30.4).
The receiver rescales its taps at that boundary rather than being surprised by it.

It is also built on the first frame that actually carries signal. Built eagerly,
its 600-half-symbol gain prologue runs on the silence before the far end starts
its conditioning signal, and it then spends the rest of the call with a gain
measured from nothing — which is exactly what stalled the first working
handshake in HUNT2.

### The E "alignment" bug, found

It was never an alignment bug. Three separate defects, and the diagnosis only came
out by instrumenting the caller's eye rather than its bits.

**1. Signal B1 masqueraded as signal E.** `parse_rate` validated only the seven
sync bits of §5.3.1 — B0-B3, B7, B11, B15 — and **all sixteen bits set satisfies
every one of them**. B1 is scrambled binary ones, which descrambles to exactly
that. So the first 16 bits of B1 parsed as a valid E, and the rate was read off
scrambled ones. Table 6 gives B9-B14 = 001000 for "absence of special operational
modes", with Note 2 assigning the other values to V.32bis, so a V.32-only receiver
must reject anything else — and it has to, precisely because of this.

**2. The real E was corrupted, because the eye was barely open.** Capturing the
bits either side showed the answerer sending
`…0000001100010001 | 1111001100010001 | 1111111111111111…` (last R3, E, then B1)
and the caller recovering `…0000001100010001 | 1111001110110100 …` — the last R3
intact, E's first eight bits right and its last eight wrong. Not a shift: four bad
dibits in the middle of a 16-bit sequence.

Measuring the caller's symbols showed why: median distance to the four-point set
of 0.5-1.3 against a decision radius of 1.0. And §5.3.1's "two consecutive
identical sequences" is exactly the protection that a rate signal has and **E,
being a single sequence, does not** — so E was the first thing to break. The rule
is an error check, not just a sync rule.

**3. The receiver was being trained on the wrong signals.** Per-`assess` logging
showed the caller's medians at 0.39-0.77 against a 0.30 threshold, and
`dd_reached` false for the entire call: it never left blind acquisition, so it
never got a carrier rotator.

The cause is that it was opened on the answerer's `AC`. `A` and `C` are
*antipodal*, so AC has constant modulus and tells CMA nothing — any rotation
satisfies the cost function, including ones that never open a four-point eye — and
`S` and `S-bar` are two-point as well. §5.2.3 says outright what the training
signal is: "segment 3 is intended for training the adaptive equalizer in the
receiving modem". The receiver now opens at TRN, detected via §5.2.2's own
"well-defined event" — S and S-bar share a magnitude spectrum, so what is
detectable is the end of them.

**And a fourth, introduced while fixing the third.** Gating the receiver's *input*
splices its sample stream, and the equaliser sees a discontinuity: with that, the
caller handed over correctly at 1.06 s, lost lock, and could never re-acquire.
`StreamRx` grew a `frozen` flag instead — the loops keep running, so the sample
clock, carrier phase and symbol grid stay continuous, but the taps are not updated
and nothing accumulates towards an acquisition decision.

**Residual at the time.** The caller's lock was still marginal enough that R3 took
11.3 s to be admitted rather than arriving promptly, because with occasional bit
errors §5.3.1's two-identical requirement rarely got two clean windows in a row.
One negotiation case showed the same symptom: B5 and B6 are not sync bits, so a
single bit error in R2 silently changes the advertised rates, and a caller
offering only 4800 could end up at 9600. Recorded as a known failure rather than
hidden — and **since fixed**; see "The rate-signal eye, and one frame" below.

Worth noting for later: the *mechanism* named here (B5/B6 unprotected) was right,
but naming a mechanism is not finding a cause. What actually put the bit error
there took a further round of measurement, and it was nowhere near the rate
signal.

### Tests

`test_v32start.py`, 18 checks: the three tone detectors against generated
signals, reversal location over 90 positions, the parity requirement, onset
rejection, the full §5.4 sequence clause by clause, and rate negotiation from
both directions.

### Not built yet, at this point in the story

- **Trellis coding** (§2.4.1.2). Recovered and built later; the whole middle of
  this document is that work.
- **Live interop of our own modem.** The soft-modem start-up has not been run
  against the hardware. Two things argue it can be: the echo return loss is
  31.9 dB, and a real exchange between the two modems — captured through the
  bridge — has the same structure and timescale as ours. It took six threshold
  bugs to actually get there.
- **§5.5 retrain**, which reuses §5.4 from its third paragraph. Built later.

## 5.4.2 The answerer that missed the caller's S

A 9600 call with no trellis coding died at R1TX: the answerer sent its rate
signal and then nothing happened until the modem gave up with NO CARRIER. The
first guess was the rate signal itself — wrong table, wrong bits for a
nonredundant offer — and it was wrong. Table 6's encoding was correct: B5 and B6
set for 4800 and 9600, B8 clear because trellis was not on offer.

The evidence was two lines earlier in the same log:

    [ 4.163] RC1   -> R1TX (rate signal R1)
    [ 4.588] R1TX  receiver opened at TRN

"Receiver opened at TRN" fires on `saw_S and not is_S(e)` — it *requires* having
seen S and then seen it stop. So S had been observed. But R1TX's own transition
tested `is_S(e)` against the current frame, and by the time R1TX looked, S was
over. The answerer therefore never ceased transmission, never waited MT, never
went looking for R2, and the caller — which sends R2 only after reading R1 —
timed out.

Which side of the RC1/R1TX boundary the caller's S lands on is pure luck of the
relative timing, a few hundred milliseconds either way. Every earlier call had
been lucky: the healthy logs show S arriving 99 ms *after* R1TX was entered.
Nothing about 9600 nonredundant caused this; that run simply drew the short
straw, and reporting it as a rate-signal problem was a guess from the state name
the machine stopped in.

The fix is to act on a latch rather than a level. `far_S` is set by the frame
loop the moment S is seen, scoped to the current conditioning phase so a 5.5
retrain starts its hunt afresh, and R1TX proceeds on the latch. With one
qualification: not before `R1_MIN` = 128 symbols of R1 have gone out, because
Table 6's detection rule needs two identical sequences and it is the far end's
R2 we are about to go looking for. Ceasing the instant we arrive with S already
latched could leave the caller nothing to lock onto.

**A near-miss worth recording.** Chasing this, MT looked like the culprit: the
failing run measured MT = 48T where healthy ones showed 192T, and 48T is exactly
one frame, which smells like a stale detection consumed before the interval
began. Grepping MT out of every saved run killed that theory in one command —
48T appears in plenty of *successful* calls, including the offline suites. The
distribution across runs was 48, 96, 144, 192 and 240T. A suspicious number is
not a fault until something shows it does harm.

### And then MT really was wrong

With the latch in, the next call got past R1TX — "incoming S (seen earlier,
during RC1), ceasing after 162T of R1" — and died anyway, further along. The log
shows why:

    [ 3.380] AC1 -> CA
    [18.200] CA   phase reversal at 42228T ... MT = 35568T
    [22.480] R1TX ceasing transmission, waiting MT = 35568T
    [35.800] WAITMT MT elapsed - hunting for R2

CA ran for **14.8 seconds** waiting for the caller's phase reversal, where a
healthy segment is about 80 ms, and MT is measured as exactly that interval — so
the answerer then sat silent for 13.3 s. By then the caller had sent its S, its
conditioning signal and its R2 and given up.

The note to Table 4/V.32 settles what MT can legitimately be: MT and NT are
"round-trip delays ... including 64T ± 2T modem turn round delay". Fourteen
seconds is not a round-trip delay on any path, least of all this one, so that is
a failed measurement being spent as if it were a real interval. MT is now bounded
to 16..1200T — 0.5 s, far beyond any round trip here — and the clamp is logged
rather than silent.

The other half is that CA has no timeout in 5.4.2, because the clause assumes the
reversal arrives. The caller does not wait: it works to its own schedule, so a CA
segment measured in seconds means the reversal was missed and the rest of the
handshake has already gone past. After `CA_MAX` = 2400T, one second, the answerer
takes MT = 64T — the turn-round delay alone — and carries on rather than waiting
for something that is not coming.

Both of these are the same shape as the R1TX bug and as several earlier ones in
this document: a clause that describes what happens when things go right,
implemented literally, with no answer for the case where the event never arrives.

### The S we heard was our own

With MT bounded the call reached HUNT2 in 4.4 s and then sat there for the
remaining 52 seconds, never seeing R2. And the log still said "incoming S (seen
earlier, during RC1)" — on a call where the caller had sent nothing at that
point, because during RC1 the caller is silent and listening.

`is_S()` normalises by mean square, so it measures *shape* and says nothing about
level. During RC1 we transmit S ourselves, and what comes back 19 dB down is our
own echo, which has exactly the spectrum of S because it *is* S. The latch fired
on our own reflection, ceased R1 early, and the caller — still waiting for an R1
it had barely seen — never replied.

The frame loop already had the answer sitting one line below: `live = e[0] >
QUIET_FRAC * lvl_peak`, the test that guards opening the receiver. The far end's
signals arrive within 10 dB of `lvl_peak`; an echo 19 dB down is 0.016 of it, well
under the 0.1 threshold. Gating the latch on the same test excludes our own echo,
and R1TX now uses the latch alone — which, unlike a bare `is_S()` in the state
handler, carries that level test with it.

**Then the whole thing worked**, first try, at the rate that had never completed:

    [ 7.635] HUNT2  R2 received: rates [4800, 9600] trellis False
    [ 8.271] RC2    R3: selecting 9600 bit/s
    [ 8.413] DATA   V.42 detection phase begins

513 frames with **none discarded**, and V.42 carrying 64 992 octets in 56.6 s —
9183 bit/s, **96% of the channel**, the best figure measured anywhere in this
project.

### 9600 nonredundant is the better mode on this rig

Which is worth stating plainly, because it is not what the trellis work would
lead you to expect. Normalised for equal transmitted power the 16-point
constellation's minimum distance is 0.632 against the trellis set's 0.447 — 3 dB
more raw margin. The code buys about 4 dB of coding gain back, but on a line whose
dominant impairment is our own echo the extra margin wins outright: 96% against
79%, with zero discarded frames against 34.

The trellis path is not wasted — it is what V.32bis's rates are built on, and the
Cirrus needs it — but on this rig, at 9600, the plain 16-point mode is the one to
reach for.

## The slop bridge, and Figure 3 measured

`softmodem/bridge.py` answers a call from one hardware modem, places a call to the
other, and forwards the audio between them without touching it. The modems
negotiate with each other and we get both directions of a real handshake on tape.
No transcoding — both legs are PCMA, so payloads are forwarded byte for byte.

It is the instrument this project had been missing. Reading a modulation out of a
Recommendation whose figures are figures and whose tables have lost their signs to
OCR is guesswork checked against structure; watching two modems that already
implement it is measurement. And because each leg is captured separately, each
file is **one modem's transmission in isolation** — no self-contamination, which a
single-modem capture can never give.

First run, both modems in V.22bis: they connected through the bridge and exchanged
**100.000 %** in both directions (7 548 and 7 972 bytes).

### A real V.32 start-up

Both modems set to `AT+MS=V32,1,9600,9600`. Classifying each 20 ms frame with the
detectors calibrated in `v32fsm.py`:

| Answering modem (leg B) | | Calling modem (leg A) | |
|---|---|---|---|
| 3.26–4.68 s | ANS (2100 Hz), 1.4 s | 0.00–4.58 s | quiet |
| 4.74–5.02 s | AC/CA, with breaks at **4.88** and **4.96** | 4.58–5.22 s | AA/CC, with a break at **4.90** |
| 5.04–5.14 s | S / S-bar (100 ms; spec 113) | 5.24–8.60 s | **quiet for 3.36 s** |
| 5.14–8.68 s | TRN + R1 | 8.62–8.88 s | S / S-bar |
| 8.82–8.92 s | S / S-bar again | 8.88– | TRN + R2 → data |
| 8.92–12.38 s | TRN + R3 | | |
| 12.48– | data | | |

Those three breaks in the tone segments are the AC→CA, CA→AC and AA→CC
transitions — the very amplitude nulls the reversal detector looks for, visible in
a real exchange. The calling modem going silent for 3.36 s after its second
reversal is §5.4.1 exactly: "on detection of a second phase reversal … cease
transmitting". And data starts at 12.5 s, against 11.3 s for our own two state
machines talking to each other: the same structure, the same order of magnitude.

### Figure 3/V.32, recovered and then confirmed

Table 3's trellis column is the part of the scan that is beyond repair — it prints
"−0" and repeats coordinates that must differ. But its **magnitudes** survived,
and every fragment of them — (1,0) (0,1) (1,2) (2,1) (0,3) (3,0) (2,3) (3,2) (1,4)
(4,1) — has **Re+Im odd**.

The integer points with Re+Im odd and |z|² ≤ 17 number **exactly 32**:

| \|z\|² | 1 | 5 | 9 | 13 | 17 |
|---|---|---|---|---|---|
| points | 4 | 8 | 4 | 8 | 8 |
| radius | 1.000 | 2.236 | 3.000 | 3.606 | 4.123 |

with mean power **exactly 10** — the same as the 16-point nonredundant set, which
is what equal transmitted power at both alternatives requires. That is a strong
reconstruction on its own: the count, the power and every readable magnitude all
land.

The bridge then settled it by measurement. Running the tracking receiver over the
calling modem's data phase:

| constellation | locks? | median distance to the lattice |
|---|---|---|
| 16-point nonredundant | no | **0.874** |
| 32-point trellis, as reconstructed | **yes**, carrier +0.071 Hz | **0.041** |

A factor of twenty, and a lock where the other alternative has none. The modems
are using trellis coding, and Figure 3's constellation is the 32 integer points
with Re+Im odd. This is the same technique that settled Figure 2/V.22bis: read the
figure, cross-check the table, then confirm against hardware.

The labelling followed, from the same capture.

### The labelling, and a nonlinear gate

The capture gives 86 792 hard decisions with a median distance of 0.042 to the
lattice and **100.0%** of them within 0.35 of a point, all 32 points used within
4.8% of uniform. Every claim below is a measurement on those symbols, and all of
them are re-checked in `test_v32.py` against a 6000-symbol slice kept as
`softmodem/ref/v32_trellis_symbols.txt`.

**The partition is forced.** Ungerboeck's chain for two dimensions is
Z² ⊃ D₂ ⊃ 2Z² ⊃ 2D₂ ⊃ 4Z². All 32 points lie in the odd coset of D₂; inside it the
cosets of 2D₂ number four of eight points, too coarse for three coded bits, and
the cosets of 4Z² number **eight of four points** — exactly what a rate-2/3
encoder needs. So a point's subset is `(Re mod 4, Im mod 4)`. The capture agrees:
the eight subsets are used at 12.4–12.7%, and the four points inside each at
24–26%, which is what uncoded Q3Q4 should look like.

**The coded bits are forced too.** A 90° rotation maps each subset *wholly* onto
another — it must, or differential encoding could not work — and the eight subsets
fall into exactly **two rotation orbits of four**:

| orbit | subsets, in +90° order |
|---|---|
| A | (0,3) (1,0) (0,1) (3,0) |
| B | (1,2) (2,1) (3,2) (2,3) |

Rotation has to preserve Y0 and advance Y1Y2, so the orbit *is* Y0 and the
position within it is Y1Y2. Nothing is chosen here except names.

**Y0 is deterministic.** Measured on the capture, Y0 is an exact function
(2048 of 2048 contexts observed, zero error) of Y1Y2 over four symbols and Y0 over
the previous three — so the encoder is systematic and recursive, which is what
Figure 2 should be.

**And it is not linear.** An exact GF(2) *linear* fit fails by 1280 of 2048
contexts, under all 96 candidate labellings. That is not a labelling problem: a
trellis code invariant under 90° rotations of a two-dimensional constellation
*cannot* be linear — Wei, 1984 — and V.32 is rotationally invariant, so Figure 2
must contain a nonlinear gate. Adding the 55 pairwise products gives a consistent
system at full rank with **zero** mismatches, and the six surviving products are
every pair drawn from {Y2[n−1], Y2[n−2], Y0[n−1], Y0[n−2]} — that is, one second
elementary symmetric polynomial:

```
Y0[n] = Y2[n] + Y1[n-1] + Y1[n-2] + Y2[n-2] + Y2[n-3]
              + Y0[n-1] + Y0[n-2] + Y0[n-3]
              + e2(Y2[n-1], Y2[n-2], Y0[n-1], Y0[n-2])          (mod 2)
```

where `e2` is the pairwise-product sum, 1 exactly at weight 2 or 3. Over the
86 789 usable captured symbols this is wrong **0 times**. Drop the `e2` term and
it is wrong 61% of the time, so the gate is not decoration.

**Eight states.** The recursion reads eight bits of delay line, so it has 256 raw
states; minimising by partition refinement collapses it to exactly **8**, each
with four distinct successors and four distinct predecessors. That number was not
fitted to anything — it fell out of the measured recursion — and V.32's Figure 2
is an 8-state code, which is the first independent confirmation that the fit is
the real code rather than a formula that merely happens to fit. Walking the
captured symbols through the 8-state table, exactly **1 of the 8 initial states**
reproduces every Y0, which is the uniqueness check.

The table, `TRELLIS_TABLE[state][2*Y1+Y2] = (Y0, next)`:

| state | Y1Y2=00 | 01 | 10 | 11 |
|---|---|---|---|---|
| 0 | 0/0 | 1/1 | 0/2 | 1/3 |
| 1 | 0/3 | 1/2 | 0/1 | 1/0 |
| 2 | 1/5 | 0/4 | 1/7 | 0/6 |
| 3 | 1/4 | 0/5 | 1/6 | 0/7 |
| 4 | 1/7 | 0/6 | 1/5 | 0/4 |
| 5 | 0/2 | 1/3 | 0/0 | 1/1 |
| 6 | 0/1 | 1/0 | 0/3 | 1/2 |
| 7 | 1/6 | 0/7 | 1/4 | 0/5 |

Read down it and the output rule is just **Y0 = Y2 ⊕ p(state)**, with p = 1 for
states 2, 3, 4 and 7. The state, by contrast, is *not* a linear image of the delay
line: of the 255 linear functionals of those eight bits exactly one,
Y2[n−1] + Y0[n−1], is constant on the equivalence classes, so two of the three
state bits are irreducibly nonlinear. Same rotational invariance, seen from the
other side.

### One measurement that was wrong, and why

A first attempt to count states asked how many distinct "input → Y0 behaviours"
the observed 9-bit histories showed, and reported **2**. That is an artefact: the
signature was keyed on the *set* of (Y1Y2 → Y0) pairs actually seen per history,
and most histories only ever saw a subset of the four inputs, so distinct states
collapsed for want of data rather than for want of distinction. Three delay
elements cannot give a 2-state code. The figure is not quoted anywhere above; the
8 comes from minimising the machine, which does not depend on which inputs
happened to occur.

The general lesson is the one this project keeps relearning: an estimator has to
be calibrated before it is believed. A number that arrives without a check on its
own construction is not a measurement.

### What the labelling still does not settle

- **Table 2**, the differential rule Q1Q2 → Y1Y2. The orbits fix Y1Y2 up to a
  relabelling; which of the ~24 candidate rules V.32 actually specifies does not
  affect the trellis and is not observable from a scrambled stream.
- **The ordering of Q3Q4 within a subset.** Uncoded and scrambled, so the capture
  says only that all four are used equally.
- Both are naming freedoms for interop, not for correctness of the code. Pinning
  them wants known plaintext, and V.14 fill at 9600 — 960 char/s of line capacity
  against a 190 byte/s DTE feed — makes the bit-exact plaintext unpredictable.
- **A Viterbi decoder** — now built, see below.

### The Viterbi decoder, and a number that had to come out right

`v32.Viterbi` decodes the recovered code. Its shape is the standard one for a
trellis-coded constellation, and the reason it is cheap is the partition: three of
the four bits per symbol are coded and pick the subset, the fourth pair is uncoded
and picks within it. So the receiver first reduces the 32 points to **8 numbers** —
for each subset, the nearest point in it and how far — and the recursion then
never touches all 32. Eight states, four branches each: 32 add-compare-selects per
symbol. Metrics are renormalised every symbol so they cannot grow over a long
call, and because a receiver joining a call does not know the encoder's state,
every state starts at metric zero and the survivors converge by themselves.

**The number that had to come out right.** Both constellations sit at mean power
exactly 10, so the ratio of the coded free distance to the uncoded minimum
distance *is* the asymptotic coding gain. Searching the recovered trellis for its
shortest error event as a shortest path over pairs of states:

| | squared distance |
|---|---|
| recovered 32-point trellis code, free distance | **10.000** |
| uncoded 16-point set, minimum distance | 4.000 |
| ratio | **3.98 dB** |

V.32's trellis code is a 4 dB code. That was not fitted to anything and not read
out of the Recommendation — it is a property of a labelling recovered from two
hardware modems, and a wrong partition or a wrong trellis would not have produced
it. Of all the confirmations in this document, this is the one that would have been
hardest to fake.

Two more distances fall out, and both are structural. Points inside a subset are
d² = **16** apart, which is why the uncoded pair can ride unprotected — it is the
easiest decision in the system. Different subsets come as close as d² = **2**,
which is why a single symbol cannot be decided on its own at all: that is the
whole reason a decoder is needed.

**Measured gain, not asserted gain** (`softmodem/v32_gain.py`, 400 000 symbols per
point, same rate and same power both ways):

| target BER | coded needs | uncoded needs | gain |
|---|---|---|---|
| 10⁻² | 13.85 dB | 14.32 dB | +0.48 dB |
| 10⁻³ | 14.97 dB | 16.78 dB | +1.81 dB |
| 10⁻⁴ | 15.79 dB | 18.40 dB | +2.62 dB |
| 10⁻⁵ | 16.29 dB | 19.64 dB | **+3.35 dB** |

The gain grows towards the 3.98 dB asymptote as the error rate falls, which is
what a trellis code is supposed to do. It is also worth recording the other end of
the curve honestly: **below about 13.5 dB the coded system is worse than the
uncoded one** — 9.3 × 10⁻² against 3.8 × 10⁻² at 12 dB. That is not a defect. Below
threshold the error events stop being isolated and start merging, and a decoder
that trusts a path over many symbols is then trusting a wrong one. The first run of
this measurement showed exactly that crossover and it looked like a bug; the check
that settled it was that at 12 dB the *subset* decisions were still better than
plain nearest-of-32 (0.216 against 0.310) and the uncoded pair had zero errors, so
the decoder was working and the code was simply past its threshold.

**On real symbols.** Over 40 000 captured symbols from the hardware connection the
decoder takes **6.6 µs each — 1.6% of realtime at 2400 baud**, so it fits inside
the RTP callback with room to spare, and it disagrees with the hard decisions
**zero** times. That is the expected answer for a clean capture, and it is worth
having as a null result: a decoder that "improved" a signal already 100% within
0.35 of a lattice point would be inventing corrections.

To see it actually correct something, single symbols in the captured stream were
displaced by a fixed offset. Theory says an isolated error is safely correctable
while its squared distance from the truth stays under d²free/4 = 2.50:

| displacement d² | trellis recovers | hard decision recovers |
|---|---|---|
| 0.98 | 400/400 | 68/400 |
| 1.62 | 400/400 | 68/400 |
| 2.00 | 398/400 | 68/400 |
| 2.42 | 394/400 | 45/400 |
| 2.88 | 376/400 | 45/400 |
| 3.92 | 354/400 | 45/400 |
| 6.48 | 127/400 | 45/400 |

Perfect below 2, decaying from about 2.4, collapsed by 6.5 — the predicted 2.50
sits exactly where the curve turns. Decay starting a little early is expected
rather than a discrepancy: these are real symbols that already carry their own
residual noise, so the effective displacement is slightly larger than the injected
one.

### The missing names were in a different Recommendation

Everything above was recovered from hardware because V.32's own copy is
unreadable. Two things were still missing: **Table 2**, the differential rule
Q1Q2 → Y1Y2, and the **ordering of Q3Q4** inside a subset. Both are naming, not
structure, and no amount of scrambled capture can supply a name.

They were not missing from the *library*. **V.32 bis** describes 9600 bit/s in
§2.3.3 with the same words as V.32 — differentially encode Q1Q2 into Y1Y2 per its
Table 1, feed a systematic convolutional encoder for Y0, map (Y0,Y1,Y2,Q3,Q4) to a
point — and its scan is clean:

| what V.32 lost | where it is readable |
|---|---|
| Table 2 (differential quadrant coding) | **Table 1/V.32 bis**, all 16 rows |
| Figure 3 + Table 3 (the 32-point mapping) | **Figure 2-3/V.32 bis**, all five bits printed at all 32 points, signs intact |
| Figure 2 (the trellis encoder) | **Figure 1/V.32 bis**, including the truth table of its nonlinear element |

Figure 2-3 flattens to text as a diamond of binary labels, and it can be read
geometrically rather than trusted: the label counts per row come out
2, 3, 4, 5, 4, 5, 4, 3, 2, which is exactly the number of constellation points at
Im = 4, 3, 2, 1, 0, −1, −2, −3, −4, so the row assignment is forced by counting
alone, and the column positions fall on a uniform grid that fixes Re. That gives
all 32 labels with no interpretation.

**The two documents then check each other.** Table 3/V.32 kept its rows and lost
only its signs, so its 32 magnitudes are still readable — and **every one of the 32
agrees** with Figure 2-3/V.32 bis. One document with the signs destroyed and one
intact, in agreement everywhere.

### What the measurement got right, and what it got wrong

The recovered code and V.32's are the same code. The measurement was right about
every structural claim: the partition into cosets of 4Z², the uncoded pair riding
inside a subset, eight states, the necessity of a nonlinear gate, free distance 10
and 3.98 dB.

It was wrong about one thing, and the error is instructive. The provisional
labelling assumed **Y0 is the rotation orbit** of a subset, on the reasoning that
rotation must preserve Y0 and advance Y1Y2. Figure 2-3 says otherwise: a 90°
rotation *changes* Y0, and Y0 = 0 covers subsets from both orbits. The assumption
was not forced by the geometry after all — it was a consistent choice, and because
any consistent bijection between the eight subsets and the eight triples yields an
isomorphic code, the fitted encoder reproduced 86 789 real symbols perfectly while
carrying the wrong names. Structure is measurable; names are not, and a fit cannot
tell you it has been handed the wrong ones.

What rotation *does* preserve is **Q3Q4**, and that is the property that matters:
rotating every point of Figure 2-3 by 90° leaves Q3Q4 untouched and shifts Y1Y2 by
exactly Table 2's Q1Q2 = 11 row. So a 90° phase ambiguity the receiver never
resolves adds a constant to the differential input, and Table 2 cancels a constant.
That is what rotational invariance *is*, and it is why the code cannot be linear.

### The encoder, read off the page

Figure 1/V.32 bis draws the encoder, and text extraction flattens it into
unreadable rubble. That is a limitation of `pdftotext`, not of the document: the
figure is vector art and it **renders**, so

```sh
pdftoppm -r 400 -f 5 -l 5 -png ITU-T_V.32bis_1991-02.pdf out
```

produces a page the circuit can simply be read from. Two gate symbols share one
truth table:

| a | b | s1 | s2 |
|---|---|---|---|
| 0 | 0 | 0 | 0 |
| 0 | 1 | 1 | 0 |
| 1 | 0 | 1 | 0 |
| 1 | 1 | 0 | 1 |

s1 = a ⊕ b drawn as a circled plus, s2 = a · b drawn as a D shape — a half-adder.
The AND is the gate Wei's theorem demanded, printed in the Recommendation.

And the wiring is legible too. Three delay elements T1, T2, T3 sit in a row with
four adders between them, and reading it left to right:

| element | what it injects |
|---|---|
| in(T1) | **Y0n** — a long wire from the output at the far right back to the left end |
| A1 | Y1n ⊕ Y2n, from the lone adder above the row |
| A2 | Y0n · N, from the first AND gate |
| A3 | Y2n, from a tap on the Y2n line |
| A4 | Y1n · Y0n, from the second AND gate |
| N | the node between A3 and A4, tapped upward to feed the first AND |
| Y0n | out(T3) |

That feedback wire from the output back to T1 is what makes the encoder recursive,
and three delays is where the eight states come from. Y0n being a delay output
makes it a **Moore** machine: the redundant bit belongs to the state and does not
depend on the current input.

`v32.TrellisFSR` is that circuit, node for node, and it is what `TrellisEncoder`
runs. Expanding it algebraically gives, term for term, the relation the GF(2) fit
had already produced from the captured symbols — four linear terms and three AND
terms (the provisional labelling had needed eight linear terms and a four-variable
e2):

```
Y0[n] = Y2[n-1] + Y1[n-2] + Y2[n-2] + Y0[n-3]
      + Y1[n-1]·Y0[n-1] + Y1[n-2]·Y0[n-2] + Y0[n-1]·Y0[n-2]
```

Zero mismatches over 86 789 captured symbols; minimises to 8 states; free distance
still 10. Drop the three AND terms and it is wrong 38% of the time.

**Two independent routes, one answer.** The circuit came off a printed page; the
relation came out of a GF(2) fit to 86 789 symbols of real modem traffic. They
agree exactly, on random input and on the capture. And the drawn circuit gives a
sharper check than the algebra can: because its state feeds back from its own
output, a wrong start state never recovers, so it must be started correctly — and
of the eight possible contents of T1, T2, T3, **exactly one** reproduces all
86 792 captured symbols.

One thing that check exposed on the way: driving the circuit from an arbitrary
start state gave 49 509 mismatches out of 86 784, which looks like a wiring error
and is not one. The fitted recursion is evaluated against the *captured* Y0
history, so it self-corrects; the circuit runs on its own Y0 and cannot. Same code,
different question — and the second question is the more demanding one.

### The whole chain, end to end

With the names in place the data path closes, and it can be tested the only way
that really counts. `v32.TrellisEncoder` takes four data bits and emits a point;
`v32.TrellisDecoder` takes symbols and emits four data bits. Offline, 39 996 bits
round-trip with **zero** errors — and the same bits come out under a 90°, 180° or
270° rotation of the entire stream, byte for byte identical. The phase ambiguity is
invisible to the data, which is the whole point of Table 2.

Then the real one. Taking the raw A-law audio the bridge captured from the calling
modem and running the complete chain — tracking receiver, Viterbi, differential
decode, GPC descrambler, V.14 framing:

```
..AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB ...
```

**7237 characters, 100% printable, 904 unbroken repeats of the pattern, 3 framing
rejects in 86 792 symbols.** `AAA2BBB ` is exactly what `v22data2.py` was sending.
Three further things fall out and all three agree with something known
independently:

- the character rate is **200/s**, which is the DTE pacing the Pi-side script was
  configured for;
- **GPC** (the call-mode polynomial) descrambles to text while **GPA** descrambles
  to noise, confirming both the direction and §4.1's assignment of polynomials;
- the gaps between characters are continuous mark, which is what V.14 fill looks
  like when a 190 byte/s feed meets a 960 char/s line.

This is the first time in the project that customer data has come out of a real
V.32 connection. Nothing in the chain was tuned to make it happen — the
constellation was reconstructed from magnitudes, the labelling read out of a
different Recommendation, the encoder fitted over GF(2), and the descrambler and
framer were already there for V.22bis.

### Measured coding gain, end to end

`softmodem/v32_gain.py`, 400 000 symbols per point, driving the real path
(differential coding, trellis, channel, Viterbi, differential decode) against the
uncoded 16-point set at the same rate and the same mean power:

| target BER | coded needs | uncoded needs | gain |
|---|---|---|---|
| 10⁻² | 14.12 dB | 14.32 dB | +0.21 dB |
| 10⁻³ | 15.20 dB | 16.78 dB | +1.58 dB |
| 10⁻⁴ | 16.06 dB | 18.40 dB | +2.35 dB |
| 10⁻⁵ | 16.67 dB | 19.64 dB | **+2.97 dB** |

(the 10⁻⁵ coded point needed its own finer sweep — 1.2 million symbols per point
between 16.6 and 17.2 dB — because at 400 000 symbols the coded curve reaches zero
errors before it crosses 10⁻⁵, and an interpolation through a zero is not a
measurement)

Climbing towards the 3.98 dB asymptote as the error rate falls, which is what a
trellis code does. These are a little below the figures measured on the coded bits
alone, and that is expected rather than a discrepancy: the differential decode
reads Q1Q2 from a *pair* of symbols, so one Y1Y2 error corrupts two consecutive
outputs. It is the honest end-to-end number, and it is the one a modem lives with.

The other end of the curve is worth recording too: **below 14 dB the coded system
is worse than the uncoded one** — the two curves cross almost exactly at 14.0 dB,
where they measure 1.249 × 10⁻² and 1.247 × 10⁻². That is not a defect. Below threshold the
error events stop being isolated and start merging, and a decoder that trusts a
path across many symbols is then trusting a wrong one. The first run of this
measurement showed exactly that crossover and it looked like a bug; what settled it
was that at 12 dB the *subset* decisions were still better than plain nearest-of-32
(0.216 against 0.310) and the uncoded pair had zero errors — the decoder was
working, the code was simply past its threshold.

### Wired into the data phase

`v32fsm` now negotiates and carries trellis coding, and getting there turned up
two real bugs — both invisible until the data phase produced bits to check.

**Negotiation.** B8 in Table 6 is "availability of trellis coding/decoding at the
highest data rate indicated in B4-6", so it is a capability, not a selection: R1
and R2 advertise, and §5.4.2's R3 *calls for* the coding, which the caller adopts
along with the rate. Both `AnswerStartup` and `OriginateStartup` take a
`trellis=` capability; R3 asserts B8 only when the answerer has it, R2 offered it,
and the selected rate is 9600. "Highest rate in B4-6" also means a 4800-only offer
never carries B8, since 9600 is the only rate we have the coding for.

There is also a small clause worth honouring: both §5.4.1 and §5.4.2 say the
encoder's delay elements "should be set to zero" where the trellis transmission
begins, so `Mod.reset_trellis()` is called at B1. (One clause says Figure 2 and
the other Figure 3; Figure 2 is the encoder.)

**The data phase now decodes.** Before this it only accumulated symbols — for
*either* coding — so nothing downstream had ever been checked. `_Rx` now runs the
symbols through the negotiated coding, descrambles, and accumulates bits. Since
§5.4 has both ends transmit continuous scrambled binary ones, the descrambled
stream must be all ones, which makes the data phase self-checking without a
payload.

| negotiated | answerer | caller |
|---|---|---|
| 9600 trellis coded | 100.00% ones | 100.00% ones |
| 9600 nonredundant (R3 early) | 100.00% | 100.00% |
| 4800 | 100.00% | 100.00%, after 15 s |
| 9600 nonredundant (R3 late) | 100.00% | **50.4%** |

**Bug 1: the equaliser taps were rescaled at the constellation switch, and should
not have been.** The handshake does run 7 dB below the data phase — §5.2 trains on
the A/B/C/D subset, mean power 2 against the data set's 10 — and the obvious
inference is that the taps need dividing by √5. They do not: the transmitter
scales both constellations by the same 1/√10, so the wire amplitude *also* rises by
√5 at the switch, and the two factors cancel. The same taps already give the right
output scale.

The error survived because the answerer's decision-directed loop reconverged from
it. The caller's did not: it sat at 0.519 of the correct scale with a median
distance of 0.496 to the lattice and decoded to noise. Two cancelling factors, one
side that hides the mistake — that is a bug that could only be found by measuring
the bits.

**Bug 2: a known constellation change should force a blind re-acquisition.** There
was already a retrain path back to CMA, but it is threshold-driven, for a channel
that drifts out from under a working receiver. A constellation switch is the other
case: the decision-directed solution is invalid *by construction*, and waiting for
an error threshold is not equivalent. On the 16-point set a stale equaliser can sit
at a median distance of 0.86 against a decision half-distance of 1.0 — every
decision a coin flip, the error just under the retrain threshold, stalled
indefinitely. `tracking.StreamRx.reacquire()` drops to blind acquisition keeping
timing and carrier, and `to_data` calls it when the constellation actually changes.

That "actually changes" clause is the second half of the lesson: firing it
unconditionally cost the 4800 case about 14 s, because at 4800 the data phase uses
the same four points as the handshake and there was nothing stale to discard. A fix
applied one step too widely is still a bug.

**What was still wrong, and what it was not.** At this point the caller could
enter the data phase badly conditioned, and the severity tracked how late R3 was
admitted:

- R3 at 2.96 s → the caller is clean from the first block, in either coding;
- R3 at 24.4 s (4800) → it reconverges, but only after 15 s of data phase;
- R3 at 11.3 s (9600 nonredundant) → it never reconverges.

The decisive comparison was the middle row of the table against the last: the
*same* 9600 nonredundant mode, the same code path, succeeding when R3 was early
and failing when it was late. That located the defect upstream of the coding and
upstream of the constellation, and it is what the next section then went and
fixed — after which the table's last row reads 100% and all three of these KNOWN
FAILs became real passes. Recording the convergence *time* rather than a
pass/fail is what made the pattern visible: three cases, one monotone
relationship, one cause.

### The bridge, re-run with V.42 left on

Worth doing again now that V.42 exists on our side: put both modems in a call
with each other through the bridge, leave error correction alone, and watch two
real implementations negotiate it.

Both forced to `AT+MS=V32,0,9600,9600`, compression off (`%C0` and `"H0` on the
Cirrus; `S46=136` is an ERROR there, as documented, and applies to the Conexant).
The Conexant reports **`+ER: LAPM`** at its DTE, so the two of them agree on V.42
between themselves.

| | A: Cirrus to Conexant | B: Conexant to Cirrus |
|---|---|---|
| delivered | 12 376 bytes | 11 778 bytes |
| correct | **100.000%** | **100.000%** |
| captured | `ref/br2_a.raw`, 81.4 s, −24.4 dBFS | `ref/br2_b.raw`, 81.4 s, −25.4 dBFS |

Byte-for-byte relaying, no transcoding, 4069 frames one way and 4068 the other.

### What they negotiated: 9600 with trellis coding

The modems do not say. `CONNECT 115200` is the DTE speed, and `ATW1` on a second
run produced `+ER: LAPM` but no `+MCR`/`+MRR`. So it had to come out of the
capture, and the first attempt failed in a way worth recording: replaying the
B leg under 4800, 9600 non-redundant and 9600 trellis gave a collapsed eye for
all three, 10 to 19% of symbols within tolerance, which reads like "none of these
is the mode".

It was not. The mode was among them and the *start times were wrong* — a guess of
`--trn 14.6 --data 16.0`, when the rate signal actually runs from 14.44 to 16.14 s
and TRN is back around 11 s. So the equaliser was being trained on the rate signal
instead of on the segment 5.2.3 provides for exactly that purpose, and a
receiver that never converges says nothing about the constellation it was pointed
at. "None of the candidates fits" and "the harness was misconfigured" produce the
same collapsed eye.

Decoded properly, both ends state it themselves. Sweeping the acquisition point so
the equaliser trains on TRN, and descrambling each leg with the generator its
sender uses — GPC for the calling modem, GPA for the answering one:

| | rate signal | signal E |
|---|---|---|
| A, Cirrus, GPC | `[9600]`, trellis, not V.32bis — **509 consecutive identical parses** | 16.14 s |
| B, Conexant, GPA | `[9600]`, trellis, not V.32bis | 16.20 s |

The V.32bis readings that appeared earlier — 7200/12000/14400 and the like — turn
up only when a leg is descrambled with the *wrong* generator, three to seven
parses that disagree with each other. Self-consistency across 509 of them is the
difference between a measurement and a misread.

And then the data phase confirms it end to end. Switching to the 32-point set at
signal E + 128T:

| | eye median | within 0.35 | HDLC frames | bad FCS | pattern recovered |
|---|---|---|---|---|---|
| A: Cirrus to Conexant | **0.042** | **100.0%** | 1224 | 21 | **952 x** `SLOPBRIDGE-A ` |
| B: Conexant to Cirrus | 0.528 | 23.6% | 269 | 1824 | 197 x `SLOPBRIDGE-B ` |

So: **V.32 at 9600 bit/s, trellis coded**, and the DTE pattern of a real
modem-to-modem LAPM link read back out of the air through our own deframer.

### The rate I measured was the rate I had asked for

The run above was set up with `AT+MS=V32,0,9600,9600` — modulation V.32, automode
off, minimum 9600, maximum 9600. That caps the rate. So "what did they negotiate"
had the answer written into the question, and 509 self-consistent parses of
`[9600] trellis` measured my own setup string. The decode was sound; the
conclusion drawn from it was not a finding about the line.

Reset and asked again. `AT&F` on both, then `AT+MS=?`:

| | after `AT&F` | supported |
|---|---|---|
| Cirrus CL-MD56xx | `V90,1,0,0` | `AT+MS=?` returns ERROR, but writes are accepted |
| Conexant CX93001 | `V92,1,300,48000,300,56000` | `B103,B212,V21,V22,V22B,V23C,V32,V32B,V34,V90,V92,ALM1,ALM2` |

Both default to V.90/V.92 automode, and the V.32bis token is `V32B`. Both accept
`AT+MS=V32B,0,4800,14400` and read it back.

### At 14400 the Conexant's transmitter gives out

With the cap lifted they negotiate the top rate, and the Cirrus's signal E says so
outright:

| | rate signal | signal E | data phase at 14400 |
|---|---|---|---|
| A, Cirrus, GPC | `[14400]` V.32bis, 503 consecutive identical parses | 16.22 s | median **0.122**, 90th 0.222, **100.0%** inside d_min/2 = 0.707 |
| B, Conexant, GPA | offers `[4800, 7200, 9600, 12000, 14400]` | none seen | **would not decode** |

At the modems' own DTEs, with error correction unavailable: 98.9% correct in the
A-to-B direction and **47.3%** in the B-to-A direction.

So the line does carry 14400 — one way. The Cirrus's 128-point signal arrives with
every symbol inside its decision boundary and a median error a sixth of the way to
it. The Conexant's does not arrive usably at all: not at its peer's DTE, and not
through our own receiver either.

That also explains `+ER: NONE`, which persisted even with `\N3` set on both
modems and `&Q5`/`S48=7` accepted by the Conexant. V.42's detection phase is an
exchange — ODP one way, ADP the other, as asynchronous characters. One direction
running at half its characters correct cannot complete it, so both ends give up on
error correction and hand the corruption straight to the DTE.

The usable configuration on this rig is therefore **9600 with V.42**, which
delivered 100.000% in both directions, and not 14400, which negotiates and then
cannot be relied on. That is a property of the Conexant's transmitter, not of the
rate: the same 14400 in the other direction is clean.

### Checking that claim properly: swap the legs and repeat

The claim above rested on one call, and it was confounded. Each direction of a
bridged call uses *one leg's receive path and the other leg's send path*, so "the
Conexant's capture will not decode" is equally consistent with the Conexant's
transmitter being weak and with our leg-B receive being noisy. And the modems'
own DTE figures cannot settle it either, because every pairing changes the
transmitter *and* the receiver at once: "Conexant to Cirrus is bad" fits a weak
Conexant transmitter exactly as well as a weak Cirrus receiver.

Two things fix the experiment. Swap which modem sits on which leg, and judge the
transmitted signal with a *fixed* third-party receiver — ours — which sees only
the transmit side. Three runs per configuration:

| | on leg A | on leg B | clean eyes |
|---|---|---|---|
| **Cirrus transmit** | 0.122/100%, 0.587/72%, 0.189/100% | 0.561/76%, 0.082/100%, 0.086/100% | **4 of 6** |
| **Conexant transmit** | 0.581/74%, 0.581/74%, 0.588/73% | no signal E, no signal E, 0.844/69% at 12000 | **0 of 6** |

The Conexant never once puts a cleanly decodable 14400 signal on the line: 0.581
± 0.004 across three leg-A runs, undecodable twice on leg B, and in one run it had
already fallen back to 12000. The Cirrus manages a clean eye four times out of
six, with medians down to 0.082. The ordering holds within each leg taken
separately, so it is not the leg assignment.

So the claim stands — **the Conexant's transmitter is the weaker one** — but it
took a swapped 2x2 with replicates to earn it, and the single-run version was not
evidence.

Two things the same data says that the claim does not. The Cirrus is poor in two
of its six runs, so the path contributes variance of its own and the Conexant's
transmitter is not the only impairment here. And at the DTE the numbers are
beautifully reproducible in one direction — 58.86% ± 0.04 across three runs, and
98.87% ± 0.01 across three others — while the opposite direction of the same
configuration scatters from 30% to 65%. Reproducibility to four significant
figures on one side of a link says nothing about the other.

### The Conexant's transmitter is the weaker one, measured cleanly

The asymmetry in that table is not a timing artefact — the B leg was decoded from
its *own* signal E, and the two legs arrive 1 dB apart (−24.4 and −25.4 dBFS).
A median error of 0.042 against 0.528 is a factor of twelve, through the same
receiver, on the same modulation, over the same box.

This corroborates an attribution made earlier by a different route, where the
12.9% residual at 12000 was put down to the Conexant's transmitter because the
Cirrus's signal read 1.3% through the same receiver. A bridge capture is the
cleaner instrument for that claim: neither signal has passed through *our*
transmitter, so what is being compared is two modems' transmitters and nothing
else.

## The rate-signal eye, and one frame

Every remaining caller-side defect turned out to be one frame long.

**The measurement.** Instrumenting the caller frame by frame through the
handshake, rather than looking at its bits, gives the whole story in eight lines:

```
  t(s) ansState callState | inE(band0) frozen dd lockerr fastErr |w|
  1.46 R1TX     STX_NT    | 257443.9  False  True  0.0189  0.0022 0.993
  1.48 HUNT2    STX_NT    | 255470.8  False  True  0.0139  0.0057 0.993
  1.50 HUNT2    RC1       |   2250.0  False  False 0.0000  1.9316 3.373
  1.52 HUNT2    RC1       |      0.0  True   False 0.0000  1.9316 3.373
```

At 1.50 s the answerer enters its R2 hunt and **stops transmitting**. The frame in
which that happens is part signal and part nothing: 2250 against the 255 471 of
the frame before it, 20 dB down. The freeze gate was `e[0] <= 100.0`, an absolute
floor, and 2250 is 22× over it — so the frame counted as live, one frame of
decision-directed adaptation ran on a signal that had vanished, and the taps went
from |w| = 0.993 to **3.373** as the equaliser tried to amplify nothing.

CMA never climbed back out. `acq_med` sat between 0.74 and 0.87 against a 0.30
threshold for the remaining ten seconds of handshake and straight through into the
data phase. Everything downstream followed from that one frame.

**The fix is a relative gate.** An absolute floor cannot catch a transition,
because the transition frame is not quiet — it is half of each. What is
detectable is that the level has fallen far below what we have been tracking. So
`_Base.step` keeps a slow reference of the in-band level on frames it adapts on,
and freezes when a frame falls below half of it.

The threshold was measured rather than picked. Over a whole handshake, on frames
judged live:

| | ratio to the tracked reference |
|---|---|
| minimum on a legitimately live frame | **0.757** |
| 1st percentile | 0.821 |
| median | 1.000 |
| maximum | 4.206 |
| the collapse frame this must catch | **0.009** |

0.5 sits with a 1.5× margin below the smallest legitimate frame and 55× above the
collapse. The gate is deliberately one-sided: that 4.206 maximum is the legitimate
7 dB rise into the data phase, and a symmetric gate would freeze exactly where the
receiver most needs to adapt.

**Results.** Every negotiation case, from 12 dB of level range and both ANSam
durations:

| | before | after |
|---|---|---|
| data phase reached, 9600 | 11.32 s | **2.96 s** |
| data phase reached, 4800 | 24.42 s | **2.96 s** |
| R3 as received | `[2400, 9600]` | **`[9600]`** |
| signal E as received | spuriously "trellis" | correct |
| caller's data bits | 50.4% correct | **100%** |
| caller offering only 4800 | picked 9600 | **picks 4800** |

All three KNOWN FAILs in `test_v32start.py` are now real passes, including the
B5/B6 one. The R2 bit error was never a rate-signal problem at all — it was this
frame, ten seconds earlier, and the rate signal was merely where the damage first
became visible.

**A near miss worth recording.** A second candidate fix was tried in parallel:
make a retrain actually re-run training, resetting the taps to a centre spike
instead of keeping the diverged ones. It works, in the sense that the caller
regains dd and the data phase goes to 100%. But R3 still arrived at 11.32 s and
still read `[2400, 9600]`, and E still read "trellis". It repairs the receiver
after the damage instead of preventing it, and the handshake had already been
misread by then. Two fixes, both producing a clean data phase; only one of them
addressing the cause. The bits alone would not have told them apart — the
frame-by-frame trace did.

## Live interop: our V.32 against a real modem

`v32answer.py` answers a call and drives `AnswerStartup` frame by frame in the
RTP callback; `orch_v32.py` starts it and then has the Pi force a modem off
automode (`AT+MS=V32,0,9600,9600`) and dial. Both modems have to be forced —
left to themselves they pick V.34 or V.90 and never reach §5.4.

### The result

Against the Conexant (`**2`), the full §5.4 start-up completes and the modem
prints **`CONNECT 9600`**:

```
[ 3.294] ANS     V.25 answer sequence done
[ 3.381] AC1     1800 Hz held for 192T after 144T of AC - timer on, switching to CA
[ 3.414] CA      phase reversal at 8064T - timer stopped, MT = 96T
[ 3.515] AC2     amplitude drop - silence for 16T, then the receiver conditioning signal
[ 4.153] RC1     -> R1TX (rate signal R1)
[ 4.231] R1TX    incoming S - ceasing transmission, waiting MT = 96T
[ 4.380] HUNT2   receiver opened at TRN
[ 7.594] HUNT2   R2 received: rates [4800, 9600] trellis True
[ 8.236] RC2     R3: selecting 9600 bit/s from [4800, 9600]
[ 8.336] R3TX    incoming E: 9600 bit/s trellis
[ 8.378] B1TX    128T of scrambled ones sent - 106 enabled, 109 on
```

Every clause of §5.4.2 in order, against hardware, with **trellis coding
negotiated in both directions** — the modem's R2 asserts B8 and our R3 selects
it. Then a 75-second call:

| | |
|---|---|
| data phase | 160 133 symbols, 66 s |
| retrains | **0** |
| decision-directed | held throughout |
| descrambled data bits | 640 432 |
| correct | **100.00%** |

The far end is a V.32 modem in direct mode with an idle DTE, so it sends
continuous mark; 100.00% ones is that mark recovered through the tracking
receiver, the Viterbi decoder, Table 2's differential decode and the GPA
descrambler. `rules.md` asks for a minute of data-phase traffic; this is 66
seconds of it, error-free, in one direction.

### Six bugs, none of which offline testing could have found

Every one is the same shape: a threshold that is only correct when the far end is
a loopback.

**1. The amplitude drop was tested absolutely.** §5.4.2 waits for "an amplitude
drop in the incoming tone" and the code asked `e[0] < 1.0` — near-perfect silence,
true only of a transmitter that emits exact zeros. The Conexant's cessation
measured a mean square of **5926** where its tone had been **3 700 000**: 28 dB
down, and called "still transmitting". The answerer never left AC2, never sent the
conditioning signal, and the modem gave up and retried six seconds later. Now
relative, at a tenth of a tracked reference.

**2. That reference has to be anchored to the expected signal.** The first
relative version latched onto the V.25 answer tone and then read the *legitimate*
step down to AC as a cessation — a full-amplitude sine against the four-point
constellation at mean power 2 scaled by 1/√10 is **12.3 dB**, measured 4 272 571
against 248 779 — and having declared "quiet" it stopped updating and never
recovered. The tracker now only runs while the signal it is measuring is present
(`is_tone1800` for the answerer, `is_pair` for the caller).

**3. An absolute floor opened the receiver on silence, and the receiver crashed.**
`e[0] > 100.0` is satisfied by 5926 of idle noise, so the receiver opened on
nothing, measured its input gain from nothing, and CMA's cubic error term
**overflowed a double** and took the process down. Two fixes: the open test is now
relative to a peak hold, and `StreamRx` gained a divergence guard — beyond 16×r2
it applies the gain CMA is trying to reach directly instead of taking a gradient
step towards it. A modem should not be able to crash on a bad input level.

**4. The gain at the constellation switch was assumed, and the assumption was
backwards.** Our transmitter scales every constellation by 1/√10, so its wire
level rises 7 dB from the handshake to the data phase and the taps need no
correction. The Conexant does the opposite: **-25.0 dBFS through S, TRN and every
rate signal, -24.6 dBFS in the data phase** — constant line power, no step. That
left our equaliser output √5 too small for a power-10 constellation, and the
decision-directed loop spent **nine seconds** walking the taps up by 2.24×, its
eye at a median distance of 0.48 with 33% of symbols inside 0.35. `rescale_to()`
now measures instead of assuming: 200 symbols, then one gain correction. Recovery
went from 9 s to 1.5 s, and it is right under either convention.

**5. Our own 7 dB step had to go too.** Symmetry: the far end's equaliser trains
on our TRN exactly as ours trains on its, and ours stepped from **-36.2 to
-29.3 dBFS** at the data phase. The Recommendation is ambiguous — its coordinates
are relative and TRN really is a subset of the data constellation — so hardware
decided it. `Mod.SCALE_4` puts the four-point signals at the same power as the
data phase.

**6. The rate signal was read before the equaliser had converged.** The Cirrus's
R2 was accepted **0.13 s** after the receiver opened, reading `[9600]
trellis=False`. Decoding the same signal offline once locked gives `[9600]
trellis=True` — **10 747 times over**. §5.3.1's "two consecutive identical
sequences" protects against noise, not against a receiver that is wrong the same
way twice, and B8 is not one of the seven sync bits, so nothing in the sequence
objected. This is the same defect that once sent a 4800-only caller to 9600, and
the same lesson: an unprotected payload bit is only as good as the receiver under
it. The scanner is now gated on `dd`, which is also §5.2.3's own order of
operations — TRN is "intended for training the adaptive equalizer", so let it.

One thing was *removed*: the blind re-acquisition on a constellation change. It
was added when the caller could enter the data phase with a wrecked equaliser,
which turned out to be the frozen-frame defect, and once that was fixed it made no
measurable difference offline. Live it is actively harmful — the carrier loop only
runs in decision-directed mode, so dropping to CMA stops tracking the carrier, and
the first live call held its radius while the constellation rotated underneath it.

### Open at this point, and how each closed

- **The Cirrus (`**1`)** reaches R2 and stalls at R3TX: it keeps transmitting and
  never sends E. *Closed:* its R2 was being read before the equaliser had
  converged, and B8 is not a sync bit. Gating the rate scanner on `dd` fixed it —
  see "The rate-signal eye, and one frame".
- **One observation, not a finding.** A 75-second call with an idle DTE was
  flawless; one with 1112 bytes of DTE data flowing was dropped at 31.5 s. Written
  down as n=1 against n=1 rather than as a cause. *Closed:* it was V.42 — the
  modem was negotiating LAPM and had nothing to do with how much data flowed. The
  discipline of not promoting it to a finding is what stopped a wrong cause being
  built on.
- **The rig.** The Conexant's ACM port wedged after about ten calls: the DTR ioctl
  itself blocks, and neither an in-band abort nor a USBDEVFS_RESET clears it. It
  needs a physical replug. Worth knowing before a long session.

## §7: the V.14 converter

§7.1.2 and §7.2 are the two ends of the same thing: start-stop characters from the
DTE become a synchronous stream for the modulator, and back again. Both halves
already existed for V.22bis — `dte.AsyncEncoder` and `tracking.AsyncFramer` — but
neither was wired into V.32, and neither implemented the part that makes V.14 more
than framing.

**Table 8 is the requirement, and it is a number.** At 9600 bit/s the DTE may
present 9600 to 9696 bit/s in the basic range, 9600 to 9821 extended; at 4800,
4800–4848 and 4800–4910. The line is synchronous and slower, so the converter has
to absorb the difference, and V.14's mechanism is deleting stop bits — never data.

The arithmetic is worth writing down because getting it wrong sends you looking in
the wrong place. Deleting one stop bit takes a character from 10 bits to 9, which
is a 10% saving *on that character*, so deleting a fraction p of stop bits lets the
DTE run at 10/(10 − p) times the line rate. The basic limit therefore needs
**p = 0.1** and the extended limit **p = 0.226** — not the 1% and 2.3% the rate
figures suggest at a glance, which is what I first assumed and then had to correct
against the measurement.

**Two things had to be built.** The transmit converter gained deletion under
overrun, and the receive framer gained the other half: a zero where the stop bit
belongs is a deleted stop bit, and that zero is the next character's start bit.

**And the first two attempts at deletion were wrong in instructive ways.**

- *Threshold too low.* Triggering at a queue depth of 2 deleted essentially every
  stop bit, because the modem polls once per 20 ms RTP frame and that is 48
  characters at 9600 — the queue is always deep at the moment of emission. Worse,
  a stream with no mark bits left in it is one the far framer can never *acquire*
  on, so nothing came out at all. The threshold has to sit above the caller's
  natural burstiness: 128.
- *Bang-bang deletion.* Deleting on every character while above the threshold
  produced runs of hundreds of consecutive deletions. The framer reads a run of
  them as lost framing — which is exactly how it still catches a genuine slip, and
  worth keeping — so it resynchronised in the middle of them and lost characters.
  Deletion is now never applied twice in a row, which caps p at 0.5, twice what
  the extended range needs, and leaves the run-based slip detection meaningful.

Measured against Table 8, byte-identical output at every limit:

| | deleted | stop bits restored | resyncs | |
|---|---|---|---|---|
| 9600, at the line rate | 0.0% | 0 | 0 | identical |
| 9600, basic limit (9696) | 0.3% | 23 | 0 | identical |
| 9600, extended limit (9821) | 11.1% | 1001 | 0 | identical |
| 4800, extended limit (4910) | 9.5% | 856 | 0 | identical |

**End to end through a real call.** `AnswerStartup.put()` queues characters and
`received()` takes back what the far end sent. Over a 9600 trellis-coded loopback,
1600 characters each way arrive exact to the end with **zero framing errors** in
either direction, and the 4800 path likewise. An idle converter emits mark, which
*is* §5.4's "continuous scrambled binary ones", so an idle link behaves exactly as
before.

**One regression, and why the fix is a flag.** Switching deletion on globally broke
three suites: the V.22bis paths queue a block up front rather than pacing it, which
looks identical to a DTE running fast, so stop bits started disappearing from
streams whose deframers had no reason to expect any. Those paths are verified
against two real modems and changing them silently would be the wrong trade, so
deletion is opt-in (`delete_stops=True`) and only V.32's data phase asks for it.

A small honest note on what comes out: the first byte or two after the data phase
opens can be junk, because the descrambler needs 23 bits to synchronise and the
framer discards the first character of its acquisition run by design. It is
bounded and it is at the start only.

### Against the hardware

Run against the Cirrus, with its DTE fed `AAA2BBB ` after CONNECT:

```
7.2 V.14: 750 characters recovered from the far end's DTE
   framer: 3 locks, 736 good, 2 bad, 17 stop bits restored
   .....Q.0.L.y+..Z.9........d..T.... ..)_.......x.F.q.b.AAA2BBB AAA2BBB AAA2BBB AA
   A2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AAA2BBB AA
```

Quantified rather than eyeballed: the pattern first appears at character 54, and
from there it is **exact for 696 characters to the end of the call — 87 repeats,
zero character errors**. The 54 characters before it are the descrambler
synchronising, the framer acquiring, and the modem's own idle; the two framing
errors are in that same stretch, before any data.

The line worth singling out is **17 stop bits restored**. The Cirrus deletes stop
bits — it is doing V.14 on its side — so the receive half built in the previous
section is not merely passing its own tests, it is being exercised by hardware
doing the thing the Recommendation describes. That was not visible before, because
before this the receive path stopped at bits and a deleted stop bit was just a
framing error nobody counted.

For contrast, the ad-hoc "hunt for a start bit" framing used for earlier captures
in this document reports 879 characters with **184 rejects** over the same bits.
The streaming framer gets 750 with 2. Both are looking at the same data phase; the
difference is entirely in the deframer.

The transmit direction is unchanged: the Cirrus still cannot decode our data phase,
so nothing we send arrives at its DTE. The V.14 work does not touch that — but it
does mean the receive direction is now complete from the wire to characters, and
the asymmetry is now sharply framed as *one* direction of *one* layer.

## §5.5 retrain

§5.5 is two short paragraphs and they map almost exactly onto states we already
had. Each side detects the *other* side's carrier state and resumes §5.4 partway
through:

| | trigger | transmits | resumes at |
|---|---|---|---|
| answer (§5.5.2) | 1800 Hz for > 128T | alternate A and C, even, ≥ 128T | §5.4.2 ¶3 = our `AC1` |
| call (§5.5.1) | 600 or 3000 Hz for > 128T | state A repetitively | §5.4.1 ¶3 = our `AA` |

Both also turn circuit 106 OFF and clamp 104 to binary one, and §5.5.2's note and
§5.5.3 keep 107 and 109 ON throughout — a retrain is not a disconnection.

§5.5 also permits a retrain on "unsatisfactory signal reception", left to the
implementation. Ours is the equaliser losing its decision-directed lock in the
data phase for a sustained second, guarded by a two-second settling window after
the constellation switch and capped at four consecutive attempts so two modems
cannot retrain at each other forever.

**The bug this turned up, which was already written down elsewhere in this
document.** §5.4.2 wants the AC segment to be an *even* number of symbol
intervals, and the reason is recorded above: on an odd boundary AC..CA
concatenates seamlessly and the far end has no reversal to find. The parity was
being computed from the *absolute* symbol counter, which is the same thing as the
segment length exactly once — on the first pass. On a retrain the counter is in
the tens of thousands, the pad was applied backwards, the AC segment came out odd,
and the caller sat in AA forever waiting for a reversal that was never transmitted.
Knowing a rule is not the same as applying it to the right quantity.

**Measured, soft to soft.** A retrain initiated from either side recovers in about
2.4 seconds — 6.0 s to 8.4 s — renegotiates 9600 with trellis coding, and returns
an error-free data phase in both directions. The far end joins off the carrier
state alone, so each side ends with exactly one retrain counted. And with the link
genuinely damaged — additive noise at 0.8x the signal RMS for three seconds — the
answerer calls its own retrain, "our own receiver has lost lock", and recovers
once the noise stops. A level cut does *not* trigger it, and should not: the
equaliser simply follows a level change, and an 18 dB cut produced no retrain at
all. Damage is not the same as change.

## Both modems, interoperating

With the retrain in place and the `dd` gate on the rate scanner, the Cirrus
completes the handshake too — it had been stalling at R3TX because its R2 was
being read before the equaliser converged.

| | Conexant `**2` | Cirrus `**1` |
|---|---|---|
| result code | `CONNECT 9600` | `CONNECT 115200` |
| negotiated | 9600, trellis | 9600, trellis |
| call length | 75 s | 60 s |
| data symbols | 160 133 | 120 293 |
| retrains | 0 | 0 |
| eye, median distance | 0.409 | **0.041** |
| within 0.35 of a point | 37.8% | **100.0%** |
| data bits recovered | 640 432 | 481 072 |
| correct | **100.00%** | **100.00%** |

Two different modems, two different chipsets, both at 9600 bit/s trellis-coded
V.32, both decoded without a single bit error for a minute or more. The Cirrus's
eye is ten times cleaner than the Conexant's, which is worth noting only because
*both* decode perfectly: 0.409 median sounds bad but sits well inside a decision
radius of 0.707, and the trellis code closes the rest. The eye statistic was
nearly a red herring — the bits are the measurement.

### The asymmetry, stated precisely

Neither modem decodes *us*. The Cirrus passes 50 159 bytes of noise to its DTE;
the Conexant, in an earlier run, asked for a retrain instead. Everything we can
check about our transmitted data phase says it is correct:

- our own receiver reads it back at a median distance of **0.041** with 100% of
  symbols inside 0.35, continuous, no gaps;
- trellis-decoded and descrambled with GPA it is **99.9973%** binary ones, which
  is what continuous scrambled ones should be — and that check runs the
  polynomial recursion directly rather than our own `Scrambler`, so it tests the
  scrambler rather than agreeing with it;
- §4.1.1's allocation is confirmed independently: we descramble *their* stream
  with GPC and get 100.00% ones, which also confirms our bit order and our
  labelling, since descrambling is order-sensitive and a permuted quadbit would
  descramble to noise.

So the receive direction validates the tables, the scrambler and the bit order,
and the transmit direction uses the same ones. What is left is a convention we
could have wrong *identically at both ends* — which is exactly the class of error
self-testing cannot see.

### Chasing it with the bridge capture, and what that ruled out

The intended experiment was a bridge with our transmitter standing in for one
modem. **It was not run: the Conexant's port is wedged and a bridge needs two
modems.** What follows is what could be done without it, including one refuted
hypothesis, because a refuted hypothesis is worth more than an untested one.

**The scope of the question is narrower than it looked.** Leg A of the earlier
bridge capture — a real *calling* modem — decoded through our tables to its own
plaintext. Those tables are role-independent: Figure 2-3's mapping, the trellis
recursion, Table 2's differential rule and the bit order within a quadbit are the
same whichever end you are. So leg A already validates them for the answer role
too, and the only role-specific thing in the data phase is the scrambler
polynomial, which §4.1.1 spells out and which our 100% recovery of *their* stream
confirms from the other side. There is very little room left in the data phase.

**`ref/v32_b.raw` is the real answering modem**, the direct reference for our own
role, and it is on disk from the same bridged call. The decode did not complete:
its data phase sits at **-34.5 dBFS**, 9 dB below leg A, and a receiver started
blind on a 32-point constellation never reached decision-directed mode
(median distance 0.555, 20.7% of symbols inside 0.35). Training it on leg B's own
conditioning signal first needs the S-to-TRN boundary located in that leg, which
this attempt did not manage. The 44.7% "trellis mismatch" it produced is a
statement about an unlocked receiver, not about the code, and is not evidence of
anything.

**One real discrepancy, measured.** The same capture shows the real answerer's
conditioning-plus-rate-signal phase running **3.2 s to 8.8 s — 5.6 seconds** —
against our **0.64 s**. We send §5.2.3's bare minimum of 1280 TRN symbols, and
§5.2.3 says outright that TRN "may be extended in order to ensure a satisfactory
level of echo cancellation". A far end that cannot train on 0.64 s of TRN but
trains fine on its own 5.6 s would explain the asymmetry exactly.

`TRN_MIN` is now a parameter (`--trn`) rather than a constant, which is worth
having regardless. But **the hypothesis is refuted**: at 7680 symbols — 3.2 s, five
times the minimum — the Cirrus still passes 36 469 bytes of 37.7%-printable noise
to its DTE, and it no longer reports `CONNECT` at all, so the longer TRN is if
anything worse. Our own side is untouched by the change: dd held, 0 retrains,
median distance 0.043, 100.0% of symbols inside 0.35, **100.00% of 345 516 data
bits correct**.

### The transmitter, audited against the Recommendation

Rather than guess further, the transmitter was checked clause by clause against
V.32's own text — and the decisive move was one that should have come much
earlier: **`pdftotext` mangles V.32's figures and tables, but the pages render.**
Everything below was read off a rendered page, not reconstructed.

| clause | requirement | ours | |
|---|---|---|---|
| §2.1 | carrier 1800 ± 1 Hz | 1800.000, LUT period 40 samples exactly | ✓ |
| §2.2 | 600 and 3000 Hz attenuated 4.5 ± 2.5 dB below the in-band peak | **3.57 and 3.65 dB** | ✓ |
| §2.2 | transmitted power per V.2 | flat −24.2 to −24.5 dBFS across all phases | ✓ |
| §2.3 | 2400 baud ± 0.01% | exactly 48 symbols per 160-sample frame | ✓ |
| §2.4.1.2 | coding | V.32's own wording is V.32bis §2.3.3 verbatim | ✓ |
| Figure 2 | the encoder | `T + + T + + T`, feedback delays labelled Y2n−1 and Y1n−1 | ✓ |
| **Table 2** | differential rule for trellis | **all 16 rows read off the rendered page; identical to ours** | ✓ |
| **Figure 3** | the 32-point mapping | **all 32 labels read off the rendered page; identical to ours** | ✓ |
| §4.1.1 | answer mode scrambles GPA, descrambles GPC | as implemented | ✓ |
| §5.2.1/2 | S and S-bar alternate A/B and C/D "**as shown in Figure 1**" | Figure 1's (±1,±1) | ✓ |
| **§5.2.3** | printed TRN vector, answer mode: `C C C A A C C C A A C C A C C` | **exact match**; call mode too | ✓ |
| §5.2.3 | differential encoding disabled during TRN; Table 5 after 256 symbols | as implemented | ✓ |
| §5.2.3 | segment 3 between 1280 and 8192 symbols | 1280 default, settable | ✓ |

Two of these are worth singling out because they are the Recommendation's only
*printed conformance data* for a transmitter, and we had never checked them:

- **§5.2.3 prints the first fifteen TRN signal states for each mode.** One line,
  and it exercises the scrambler, the "first bit occurring in time in each dibit"
  rule and the A/C mapping together. Both modes match exactly.
- **§2.2 is a measurable spectral requirement.** Ours comes out at 3.57 / 3.65 dB
  against a permitted 2.0 to 7.0. The first attempt to measure it reported 15.8 dB
  and "failed" — but it also failed the *Cirrus*, which was the clue: a
  single-frequency correlation on a random signal is a two-degree-of-freedom
  estimate, and taking a maximum over such a spectrum biases it upward. Averaged
  over 400-sample blocks it is unambiguous. An estimator has to be calibrated
  before it is believed, again.

And two questions that looked like leads and were closed:

- **Figure 3 marks A, B, C and D on the trellis constellation**, whose title says
  they are "used at 4800 bit/s and for training". Since the 32-point set contains
  no (±1,±1) point, that suggested the conditioning signal should use different
  states — which would have been fatal, because a 45° rotation is harmless to a
  four-point receiver and to differential coding but not to a 32-point one. §5.2.1
  and §5.2.2 close it: both say "as shown in **Figure 1**", so the training states
  are Figure 1's and Figure 3 is marking where they sit relative to the trellis
  points.
- **The level step.** A first measurement said ours still stepped +5.2 dB into the
  data phase after the `SCALE_4` fix. It does not: the averaging window straddled
  the silent stretch where we cease transmission waiting for MT. Measured in 200 ms
  blocks it is flat to within 0.3 dB, which is what the real answering modem does
  (−0.1 dB).

**Where that leaves it.** The transmitter conforms to every clause of V.32 that can
be checked, including both printed vectors and the measurable spectral limit, and
its output decodes back through our own receiver at a median distance of 0.041 with
99.9973% of the bits correct. The far end decodes our S, TRN, R1, R3 and E — it
negotiates 9600 with trellis coding with us — and then produces random bits at the
full line rate from our data phase. So its receiver locks, trains and demodulates
our four-point signals correctly, and only the 32-point phase fails.

That is a much narrower statement than "our transmitter might be wrong somewhere",
and it is the useful outcome of the audit. What it does not do is name the fault.

### The receiver, audited the same way

| clause | requirement | ours | |
|---|---|---|---|
| **§2.1** | "operate with received frequency offsets of up to ± 7 Hz" | **±7 Hz, both roles, 100.00% of bits**; still works at ±12 | ✓ |
| §3.7 | 109 transitions "solely in accordance with the operating sequences in 5"; thresholds and response times "inapplicable" | driven by the §5.4 sequence, no level threshold | ✓ |
| §4 | received sequence multiplied by the generating polynomial | as implemented, and it recovers two real modems' streams | ✓ |
| §4.1.1 | answer mode descrambles with GPC | as implemented | ✓ |
| §5.2.2 | the S-to-S-bar step "may be used for generating a time reference" | used the other way round: S ending is what opens the receiver at TRN | ✓ |
| §5.3.1 | two consecutive identical sequences, B0–3/B7/11/15 conforming | as implemented, plus a `dd` gate — see below | ✓ |
| §5.3.2 | signal E is one 16-bit sequence, Table 7 | accepted on one occurrence, but only once aligned | ✓ |
| §5.4.1 | detect one of two tones at 600 ± 7 and 3000 ± 7 Hz, then reversals | tested at both offset extremes | ✓ |
| §5.4.2 | detect 1800 ± 7 Hz for 64 symbol periods, then a reversal, then the amplitude drop | tested at both offset extremes | ✓ |
| Note 1 to §5.4 | proceed on the 600/3000 pair even with no 2100 Hz detected | implemented | ✓ |
| **Note 3 to §5.4** | a far end may precede its conditioning signal with an echo-canceller sequence, defined only spectrally | such a signal reads as none of S, the pair, or the tone, so it cannot open the receiver or arm a detector | ✓ |
| Note 5 to §5.4 | the answerer *may* disconnect if no 1800 Hz follows AC, but not within 3 s | we never disconnect on this — more conservative than required | ✓ |
| §5.5 | tone for more than 128 symbol intervals | implemented, granularity one frame (48T) | ✓ |
| Table 5 | 00→A, 01→B, 11→C, 10→D, states "shown in Figure 1" | matches | ✓ |
| §7.2 | decode per §2.4, descramble per §4, then V.14 to regain start-stop characters | wired; see below | ✓ |
| Table 8 | intracharacter signalling rate range | tested at both limits, 9600 and 4800 | ✓ |

**§2.1 is the one real receiver requirement with a number in it**, and it had never
been tested end to end. `Mod` now takes a carrier frequency so the offset can be
injected the way a real one arrives — the far end transmitting off-frequency —
rather than by post-processing. The stimulus is verified before the result is
believed: a held state A is a pure tone at the carrier, and it measures 1807.00 and
1793.00 Hz. At both extremes, both roles reach 9600 trellis-coded and decode
**100.00%** of the data phase, and the carrier loop reports the offset back to
within 0.01 Hz. There is margin: ±12 Hz also passes.

Three things are worth drawing out of that table.

**§3.7 is a clause that tells you not to build something.** "Thresholds and
response times are inapplicable because a line signal detector cannot be expected
to distinguish wanted received signals from unwanted talker echoes." A carrier
detector with a level threshold is the obvious thing to write and the
Recommendation explicitly says not to; 109 comes from the sequence.

**Our §5.3.1 is stricter than the letter of the spec, deliberately.** Two identical
sequences with conforming sync bits is the printed minimum, and it is not enough:
the Cirrus's R2 was accepted 0.13 s after the receiver opened and read B8 wrong,
twice identically. Requiring the equaliser to have converged first is not in
§5.3.1, but it is in §5.2.3 — TRN is "intended for training the adaptive equalizer
in the receiving modem" — so the ordering is the Recommendation's own.

**§7.2 was a real gap**, now closed — see the next section.

**What the audit did not find.** Nothing that would explain the interop asymmetry.
The receiver meets every clause and is tested at the frequency-offset limits in
both roles; the transmitter meets every clause including both printed vectors and
the spectral limit. Between them that is the whole of what V.32 says about a
modem's signals. The fault was therefore either outside what the Recommendation
states, or not in either end of our modem.

It was outside the Recommendation. See below.

### It was V.42

A second pass over the transmitter closed the last unread source. **Table 3/V.32
renders**, signs and all — it is the authoritative numeric mapping, where Figure 3
is dots on a diagram, and its sign-stripped OCR is what sent this whole
investigation to V.32 bis in the first place. All 32 rows of its trellis column and
all 16 of its nonredundant column agree with ours exactly.

Then the direct test: take our own transmitted data phase off the wire, hard-decide
it, label it with **Table 3's own labels**, and ask whether the sequence obeys
Figure 2's recursion. **0 mismatches in 104 097 symbols.** What we put on the line
is a valid path through V.32's trellis, judged by the Recommendation's own table.
And decoding that same capture the whole way through gives back the characters we
sent — `SLOPMODEM ` 354 times, zero framing errors.

So the transmitter was never the problem, and the next question was why a modem
receiving a correct signal produced nothing useful. The answer was in its own
profile:

```
AT\N1  ->  ERROR
AT&V   ->  ACTIVE PROFILE:
           ... &K0 &Q5 ...  S46:138  S48:7 ...
```

**`&Q5` is error-corrected mode, `S48:7` enables LAPM negotiation, `S46:138`
enables V.42bis compression.** The modem had been trying to negotiate V.42 with a
soft modem that speaks only V.14, for every call in this document. V.32 says
nothing about error correction — which is exactly why an exhaustive audit of V.32
found nothing wrong.

Two wrong turns getting to the right setting, both worth recording because the
obvious command is not the one that works here:

- **`AT\N1`** is the usual way to ask for direct mode and it returns **ERROR** on
  this modem. It had been in the dial script since the first live call, silently
  failing, which is why every run until now had V.42 enabled.
- **`AT&Q0`** is direct mode and it is accepted — but direct mode ties the DTE port
  speed to the line rate, and the Pi opens the port at 115200 against a 9600 line.
  The call connected and the DTE still saw nothing.
- **`AT&Q6`** — normal mode, speed buffered, no error correction — is the one that
  fits, with `S48=128` and `S46=136` to switch off LAPM and V.42bis.

### Both directions, against hardware

With that, one 50-second call carries characters both ways at 9600 bit/s
trellis-coded:

| | |
|---|---|
| the modem's DTE prints | `SLOPMODEM SLOPMODEM SLOPMODEM …`, unbroken |
| characters we handed to the V.14 converter | 8336 (8332 framed) |
| characters we recovered from its DTE | 709 |
| framing errors, best clean run | **0** (1 lock, 648 good, 100.0% printable) |
| retrains | 0 |

That is `rules.md`'s bar met in both directions on a real V.32 connection, at the
character level rather than the bit level.

One correction to the earlier reading. When the far end produced garbage or
nothing, this document inferred that it "cannot decode our data phase". That was
the wrong inference from a true observation: it *could* decode it, and was
withholding the data from its DTE while it waited for a LAPM partner that was
never going to answer. The observable — nothing useful at the far DTE — was
consistent with both, and I chose the explanation that pointed at our own
transmitter. The `AT\N1 -> ERROR` line had been in the logs the whole time.

And one harness bug worth naming, because it looked like a modem fault: the
runner's `put()` sliced `pattern[i:][:4]`, which drops the wrap-around, so the far
DTE printed `SLOPMODEM OPMODEM SLOPMODEM OPMODEM`. That is the test program losing
characters, not the modem.

### The Cirrus, and the one thing that is genuinely different

The Cirrus needs an entirely different command set, and the lesson from the
Conexant — read the profile, do not assume the command took — paid immediately:

```
AT&Q6      -> ERROR      ATS48=128 -> ERROR      ATS46=136 -> ERROR
AT&V       -> ... \A3 \J0 \K5 \N3 \Q3 ... %C1 ... "H3 ...
```

`\N3` is auto-reliable — LAPM with fallback — and `%C1`/`"H3` are its V.42bis
controls. So it too had been negotiating error correction all along, by a
different set of knobs: `\N0` for no error correction, `%C0` and `"H0` for no
compression. The orchestrator now offers the union of both modems' commands and
prints which were accepted, because neither takes the other's and an ERROR is the
expected answer half the time.

**At 9600 nonredundant, it works both ways.** `SLOPMODEM` appears at its DTE **700
times in a 7000-character unbroken printable run**, while we recover 551
characters from it in the same call.

**At 9600 trellis-coded, it does not.** We decode *its* trellis data phase
flawlessly — median distance 0.042, 100.0% of symbols inside 0.35, 556 characters
recovered — and its DTE gets about 38%-printable noise at the full line rate,
meaning its descrambled stream is random.

| | our receive of it | its receive of us |
|---|---|---|
| Conexant, trellis | 1791 chars | **482 × `SLOPMODEM`, 4837-char run** |
| Cirrus, nonredundant | 551 chars | **700 × `SLOPMODEM`, 7000-char run** |
| Cirrus, trellis | 556 chars, median 0.042 | noise |

That is as far as the evidence takes it, and it is worth being careful about where
it points. Three things say our trellis transmit is not the fault:

- the **Conexant decodes it**, both directions, in the same session;
- the sequence we put on the line is a valid path through V.32's trellis judged by
  **Table 3's own labels**, 0 mismatches in 104 097 symbols;
- the recursion it runs was originally fitted to **the Cirrus's own transmitted
  symbols**, 86 789 of them with zero mismatches — so its encoder and ours agree
  about the code.

An encoder that agrees with ours, a decoder that rejects ours, and another
manufacturer's modem that accepts ours: the remaining asymmetry looked like the
Cirrus's trellis receiver. That was an attribution from three pieces of evidence
rather than a demonstration, so it was settled with the bridge.

### The bridge test settles it

Both modems on a trellis call with each other, error correction off on both, each
sending its own pattern:

| direction | correct |
|---|---|
| Cirrus → Conexant | **8776 / 8813 = 99.580%** |
| Conexant → Cirrus | **1929 / 9635 = 20.021%** |

The Cirrus's trellis receiver fails against a real modem too, and fails the same
way it fails against us — bursts of correct pattern separated by garbage:

```
...CONEXNT>CONEXNT>Äû´Ä;.ârøü..þCONEXNT>CONEXNT>C³t¥.9Qùþ...
```

That closes the 2×2, and the pattern is unambiguous. Every failure has the
Cirrus's trellis receiver in it; every transmitter — the Cirrus's, the Conexant's,
ours — is decoded correctly by a receiver that works:

| transmitter | Conexant receiving | Cirrus receiving | us receiving |
|---|---|---|---|
| Cirrus | **99.6%** | — | **734 × pattern, 0 framing errors** |
| Conexant | — | **20.0%** | 100.00% of bits (direct calls) |
| ours | **482 × pattern** | garbage | — |

Our transmitter is accepted by the modem whose receiver works and rejected by the
one that also rejects another manufacturer's. Trellis coding was the *optional*
alternative at 9600 and implementations varied; this one is weak at receiving it.

A third independent validation of our own receiver falls out of the same capture:
the Cirrus's trellis transmission decodes to `CIRRUS>>` **734 times with zero
framing errors**, median distance 0.043, 97.5% of symbols inside 0.35 — a signal
we had no hand in generating.

One figure from this capture is deliberately **not** quoted. Blind-decoding the
Conexant's leg gave a median distance of 0.555 and never reached
decision-directed mode, which would suggest its transmission is poor. But that
receiver was started blind on the 32-point constellation, which this document has
already established is unreliable, and the same modem's data phase decoded to
100.00% of bits in direct calls where the receiver trained on its TRN first. The
number is an artefact of the method, not a property of the modem, and nothing
above depends on it.

**One spec-ambiguous choice was found and changed along the way.** §5.4 says to
zero "the delay elements of the convolution encoder" where the trellis
transmission begins, and says nothing about the differential encoder of Table 2 —
which §5.3 initialised from TRN's final symbol and no clause resets again.
`reset_trellis()` had been rebuilding the whole encoder, zeroing the differential
state too. It now carries across, which is what the text actually says. Measured:
it does not fix the Cirrus and does not break the Conexant, so it is a correctness
change rather than an interop one, and it is recorded as such rather than dressed
up as a fix.
- **V.32bis rates** (7200, 12000, 14400), whose constellations are Figures 2-1,
  2-2 and 2-4/V.32 bis. All three are in the same clean scan and Figure 1 already
  shows the taps for Q5n and Q6n, so they are transcription rather than research.

A note on method, since it cost a detour: **`pdftotext` failing on a figure does
not mean the figure is lost.** These Recommendations are vector PDFs. Rendering a
page to PNG and reading it recovered the encoder in minutes, after the labelling
had been reverse-engineered from 86 792 symbols of captured traffic. The
measurement was worth doing — it is what confirmed the constellation, and it is
what now verifies the figure rather than merely trusting it — but the cheap thing
should have been tried first.

## What this cost, and what it taught

### The score

A precise count is not available and would be false precision — several defects
were compound, the E "alignment" bug alone turning out to be four separate things
and the even-parity rule being got wrong twice in different places. What is worth
recording is *what found them*:

| found by | examples |
|---|---|
| reading the Recommendation properly | the four rate-signal clause misreads; §5.2.3's "even number of symbol intervals"; §4.1.1's polynomial allocation |
| offline measurement | pulse rounding at 3.33 samples per symbol; the reversal detector, after three wrong approaches; CMA divergence; the E "alignment" that was four things |
| real hardware | six thresholds, every one of which only worked against a loopback |

And two that were not ours at all: the modems' V.42, and one modem's trellis
receiver.

The distribution is the point. Offline testing found the arithmetic and the
algorithms. Reading the text found the protocol. **Only hardware found the
thresholds**, and it found all of them.

### Absolute thresholds are loopback thresholds

This is the single strongest pattern. Six times, code that passed every offline
test failed against hardware for the same reason: a constant with a unit in it,
calibrated against a transmitter that emits exact zeros.

| test | worked because | broke because |
|---|---|---|
| `e[0] < 1.0` — amplitude drop | loopback silence is 0 | real silence measured **5926** against a 3 700 000 tone |
| `e[0] > 100.0` — open the receiver | as above | 5926 is 22× over it, so the receiver opened on silence and CMA overflowed a double |
| freeze gate floor | as above | the frame where the signal *stops* is part signal, part nothing — 2250, and one frame of it drove the taps from 0.99 to 3.37 |
| tap rescale at the constellation switch | our transmitter steps 7 dB | a real modem does not step at all |
| our own 7 dB step | symmetric with the above | the far end's equaliser was 7 dB out where ours had been |
| `hiwater = 2` for stop-bit deletion | a paced caller | the modem polls per 20 ms frame, which is 48 characters |

The general form: **a threshold is a measurement, and a measurement needs a
reference.** Every one of these was fixed the same way — track what the signal has
actually been doing and compare against that. The thresholds that survived are the
ones expressed as ratios: `is_S`, `is_pair`, `is_tone1800`, and the relative gates
that replaced the absolute ones.

### Calibrate the estimator before believing it

Also six times, and it cost more than the bugs did:

- The frequency estimator reported **0.96 Hz for a 7 Hz offset** — aliasing inside
  the legal range.
- A 15 dB SNR loopback test read **0.00000** against a 0.005 threshold: passing by
  luck, and a 40-sample shift made it 0.00510.
- §2.2's spectral limit "failed" at **15.8 dB** — and failed the *Cirrus* too,
  which was the tell: a single-frequency correlation on a random signal is a
  two-degree-of-freedom estimate and taking a maximum over it biases upward.
- A state-count probe returned **2** for an 8-state code, because it keyed on the
  inputs each history happened to see.
- The +5.2 dB level step that was an averaging window straddling a silent stretch.
- Blind-decoding a 32-point constellation gave median 0.555 and "poor
  transmission" for a modem that decodes at 100.00% when the receiver trains on
  its TRN first.

Two of those — the spectral limit and the blind decode — were caught only because
the estimator *also* failed a known-good reference. Having a second signal you
already trust is worth more than any amount of care.

### A figure that does not survive OCR is not a lost figure

The largest single detour in this document was reverse-engineering the trellis code
from 86 792 captured symbols: the coset partition, the two rotation orbits, a GF(2)
fit, the discovery that no linear fit can work because 90° invariance in two
dimensions forbids it, a nonlinear fit, minimisation to eight states, and a
free-distance search to check the answer.

All of it was correct. All of it was also unnecessary. `pdftotext` mangles V.32's
figures and tables; `pdftoppm` renders the same pages perfectly. Figure 2, Figure
3, Table 2 and Table 3 were all readable the whole time, and Table 3 — the
authoritative numeric mapping, the one whose sign-stripped OCR started the whole
thing — was the last to be looked at.

The measurement was not wasted: it is what *verifies* the figures rather than
trusting them, and it is why the labelling could be stated with confidence rather
than transcription hope. But the cheap thing should have been tried first, and
"the scan is beyond repair" was a claim about a tool, asserted about a document.

### The Recommendation's own conformance data

V.32 contains exactly three pieces of directly checkable data for an
implementation, and none of them was being used until late:

- **§5.2.3's printed TRN vectors** — fifteen signal states per mode, which
  exercise the scrambler, the "first bit of each dibit" rule and the A/C mapping
  in one line.
- **§2.2's spectral limit** — 4.5 ± 2.5 dB at 600 and 3000 Hz, measurable in
  minutes.
- **§2.1's ±7 Hz** — the only numeric receiver requirement, and it needed a
  transmitter that could be detuned to test at all.

All three now run in the suite. They would have caught nothing, as it turned out —
which is itself the useful result, because it is what let the search move outward
to V.42.

### Two lessons about attribution

**A true observation with two explanations is not evidence for either.** "Nothing
useful arrives at the far DTE" was read as "the far end cannot decode our data
phase", and used to justify auditing our own transmitter twice. It could equally
mean the far end was withholding data, which is what was happening. The
`AT\N1 -> ERROR` line had been in every log since the first live call.

**An asymmetry needs a 2×2 before it names a culprit.** The Cirrus rejecting our
trellis while the Conexant accepted it supported three hypotheses. Putting the two
modems on a trellis call with each other — 99.6% one way, 20.0% the other —
reduced it to one, and made it a demonstration instead of an inference.

## V.32bis: three more rates on the same code

§2.3.1 to §2.3.4 of V.32bis are the same four sentences four times with a
different bit count: differentially encode Q1Q2 per Table 1/V.32 bis, feed the
systematic convolutional encoder of Figure 1 for Y0, map (Y0,Y1,Y2,Q3..Qn) onto a
constellation. The differential rule and the encoder are shared with V.32's 9600
trellis alternative — both already verified against V.32's *own* Table 2 and
Figure 2 — so **each added rate is a constellation and nothing else.**

| rate | points | mean power | data bits/symbol | d²free | d²free / power |
|---|---|---|---|---|---|
| 7200 | 16 | 10 | 3 | 20 | 2.0000 |
| 9600 | 32 | 10 | 4 | 10 | 1.0000 |
| 12 000 | 64 | 42 | 5 | 20 | 0.4762 |
| 14 400 | 128 | 41 | 6 | 10 | 0.2439 |

The free distances alternate because the lattices do: 7200 and 12 000 sit on the
odd × odd lattice, where the nearest neighbours are 2 apart, and 9600 and 14 400
on the Re + Im odd lattice, where they are √2 apart.

Unlike V.32's 9600 — where the Recommendation gives *both* alternatives and the
coding gain is therefore computable exactly at 3.98 dB — V.32bis specifies only
the trellis-coded form at these rates. There is no in-spec uncoded reference to
compare against, so no gain figure is quoted here; inventing a reference
constellation to divide by would be arithmetic dressed as a measurement. The
normalised free distance above is the comparable quantity.

### Reading three figures, three different ways

- **Figure 2-4 (7200)** and **Figure 2-2 (12 000)** flatten to clean grids in the
  text layer, 4×4 and 8×8. Read from there.
- **Figure 2-1 (14 400)** does not. Its rows are offset by half a column, so the
  text layer's spacing is irregular — fitting the tick line by least squares left
  residuals up to **0.79 of a unit**, which is useless for 128 points. Rendering
  it confirmed the geometry but eyeballing pixels is no better.

  What worked was `pdftotext -bbox`, which gives every label's bounding box in PDF
  points. Fit the two axes against the tick labels by least squares — 20.269 pt
  per unit horizontally, 16.158 vertically, so the figure is *not* square — then
  fit the one constant offset between a label and the dot above it. All 128 labels
  land on the integer lattice with an **rms deviation of 0.048**, all with Re + Im
  odd, mean power exactly 41.

  Three tools for the same job, and the third one should have been first. That is
  the same lesson as the whole trellis detour, learned again one level down: the
  text layer is a rendering of the document, and there is a layer beneath it that
  is not.

### Validated structurally, not by transcription care

A transcription can be checked without a second copy to compare against, because
the mapping has to have a shape. For each of the four rates:

- 8 subsets of equal size, so three coded bits choose a subset and the rest choose
  inside it;
- closed under 90° rotation;
- rotation preserves the **uncoded** bits;
- and the shift it induces on Y1Y2 is exactly **Table 1's Q1Q2 = 11 row**.

All four pass all four. A mis-parsed figure does not accidentally acquire that
signature — and the 9600 set, rebuilt through the same generalised code path,
reproduces the mapping already verified against V.32's own Table 3 exactly.

### One encoder, one Viterbi

`TrellisSet` holds a rate's mapping, subsets, branch table and power; the encoder
and the Viterbi take one. The coded bits are always three, so the trellis and the
add-compare-select are unchanged from 9600 — only the subset-decoding step widens,
from 2 points per subset at 7200 to 16 at 14 400. All four rates encode and decode
bit-exact, and all four give identical data under a 90°, 180° or 270° phase error,
which is Table 2 doing its job at every rate.

### Table 5 and Table 6/V.32bis

The rate signal is the same 16 bits, the same seven sync bits, the same
two-identical detection rule. What changes is that **B4 and B8 stop being
capability bits and are both forced to 1** — which is exactly what Note 1 to
Table 6/V.32 means by "the combination of B4 equal one and B8 equal one indicates
V.32 bis operation". The three new rates go in bits V.32 required to be zero: B9
(7200), B10 (12 000), B12 (14 400).

The neatest confirmation of that reading is a diff. A V.32bis rate signal offering
only 4800 and 9600, against a V.32 one offering the same:

```
bis  0000111110010001
v32  0000011100010001
differing bits: [4, 8]
```

Bit-identical except for the two bits that say "V.32bis". Note 1 to Table 5/V.32
bis closes the loop from the other side: if either arrives as zero, interworking
proceeds under V.32 alone.

Note 2 is worth implementing as written rather than as expected — B13 and B14
"shall be set to zero when transmitting and **ignored** during the reception of a
rate signal". A receiver that requires them to be zero would reject a conforming
future extension, so `bis_parse_rate` does not check them.

### Wired into the FSM

Negotiation needed no new mechanism, because **Note 1 to Table 6/V.32 *is* the
negotiation**: B4 and B8 both set means V.32bis. So a modem with the extra rates
emits Table 5's sequence, one without emits Table 6's, and a single
`v32.parse_any` picks the table by looking at two bits. There is nothing to agree
on first.

The rest fell out of making three things rate-derived rather than rate-specific:
the constellation, the data bits per symbol, and the trellis set the encoder and
Viterbi use. V.32bis's rates are all trellis coded, so the coding follows from the
rate rather than from a capability bit — only V.32's 9600 has an alternative to
choose between.

One consequence worth stating: the transmit scale is now `1/sqrt(mean power)` of
whichever constellation is in use, because the four sets have mean powers 10, 10,
42 and 41 and the line level must not change with the rate. At 9600 that is
exactly the old constant, which is why nothing below 12 000 moved.

| rate | constellation | data bits/symbol | data phase at | eye median | p90 | decision radius |
|---|---|---|---|---|---|---|
| 4800 | 4-point | 2 | 2.96 s | 0.009 | 0.016 | 1.000 |
| 7200 | 16-point | 3 | 3.04 s | 0.020 | 0.036 | 1.000 |
| 9600 | 32-point | 4 | 3.04 s | 0.020 | 0.036 | 0.707 |
| 12 000 | 64-point | 5 | 3.04 s | 0.042 | 0.075 | 1.000 |
| 14 400 | 128-point | 6 | 3.04 s | 0.045 | 0.084 | 0.707 |

All five negotiate, and characters cross in both directions with the settled tail
exact — around 10 000 to 11 400 characters each way per call, `v32bis_rates.py`.
One end without V.32bis falls back to V.32 at 9600, which is Note 1 to Table 5
working.

### Three defects at 128 points, and one of them was mine twice

Everything up to 12 000 worked on the first attempt. 14 400 did not, and the
reason each time was that a threshold or a window calibrated for 32 points is
wrong for 128.

**1. The deframer reported its own confusion as data.** At 14 400 the caller
emitted **14 186 characters where 640 had been sent**. The bits were right the
whole time — 0 trellis-recursion mismatches on the received symbols, 100% ones
once settled — so this was not a demodulation failure. `AsyncFramer`'s acquisition
sweep emits up to `cap` buffered characters each time it locks, and it was
re-acquiring **388 times** through the settling period. Nothing should be framed
before the eye is open, and `StreamRx` already keeps the estimate that says when it
is.

**2. Gating the framer's input spliced the bit stream** — which this document had
already written up for the sample stream, three sections earlier, as the reason
`frozen` exists instead of withholding the input. A deframer handles a splice
exactly as badly as an equaliser does. Feed it always, gate the *output*, and
rebuild it on the transition so it acquires on clean bits. Making the same mistake
twice in one codebase, with the first one written down, is worth recording.

**3. A one-shot gain correction is enough for 32 points and not for 128.** The
caller's eye sat at a median distance of **0.561** against a decision radius of
0.707, with **823 of 4000** symbols outside it, while the answerer's sat at 0.040
with none. It was a scale error: the output measured 17 to 27% under the
constellation. The window at the constellation switch can straddle the boundary
while the far end is still sending the 4-point rate signal, and the contamination
scales with the power ratio — 10 against 2 at 9600, but **41 against 2** at 14 400.

Widening the window in proportion to the constellation fixed 12 000 and not
14 400. What fixed 14 400 was giving up on one-shot: if the eye stays shut, measure
again. A decision-directed loop starting from a 20% scale error on 128 points has
nothing to work with — every decision is wrong, so the error it adapts on is
noise — and no amount of patience helps. Three or four re-measurements and it
converges to 0.045.

The pattern across all three is the same one this document keeps finding: **a
constant with a unit in it is a measurement, and it was calibrated against
something.** 200 symbols, `cap` characters, one correction — each was right for the
constellation it was written against.

### Against the hardware

The Conexant lists `V32B`, so `AT+MS=V32B,0,14400,14400` forces it. It offers all
five rates:

```
[ 7.565] HUNT2   R2 received: rates [4800, 7200, 9600, 12000, 14400] (V.32bis)
[ 8.200] RC2     R3: selecting 14400 bit/s from [4800, 7200, 9600, 12000, 14400]
[ 8.301] R3TX    incoming E: 14400 bit/s (V.32bis)
[ 8.341] B1TX    -> DATA
```

**Negotiation works at every rate tried**, and so does the transmit direction:
`V32BIS! V32BIS! V32BIS! …` arrives at the modem's DTE at 14 400, at 12 000 (a
585-character run) and at 7200 (a **4142**-character run). A real modem decodes our
V.32bis data phase at every rate.

**The receive direction stops at 9600**, and the reason is one number. Our
receiver's residual on this line is a constant fraction of amplitude — the median
distance to the lattice divided by the constellation's rms radius:

| measured | median distance | rms radius | residual |
|---|---|---|---|
| 9600 | 0.409 | 3.162 | **12.9%** |
| 12 000 | 0.834 | 6.481 | **12.9%** |
| 12 000, again | 0.868 | 6.481 | **13.4%** |

The same 13% every time, which says it is intersymbol interference and noise in
proportion to the signal rather than anything rate-specific. What changes with the
rate is how much room there is for it:

| rate | rms radius | min distance | decision margin |
|---|---|---|---|
| 4800 | 1.414 | 2.000 | **70.7%** |
| 7200 | 3.162 | 2.000 | **31.6%** |
| 9600 | 3.162 | 1.414 | **22.4%** |
| 12 000 | 6.481 | 2.000 | **15.4%** |
| 14 400 | 6.403 | 1.414 | **11.0%** |

A 13% residual fits inside 22.4% with 1.7× to spare, sits *inside* 15.4% by only
1.19× — which a distribution with a tail does not survive — and does not fit 11.0%
at all. That is the whole story, and it predicts the observed cut exactly: 4800,
7200 and 9600 work; 12 000 and 14 400 do not.

So the limit is **our receiver**, not the channel, and the proof is that the
Conexant's receiver handles the same channel at 12 000 and 14 400 — it decodes our
transmission there. Ours needs a lower residual to go further: a longer equaliser,
or the precoding real V.32bis modems use. That is a bounded piece of work with a
number attached to it, which is a better place to be than "14 400 does not work".

One thing was checked rather than assumed. The gain re-measurement added for the
loopback's 128-point case could plausibly have been making a noisy channel worse —
re-measuring corrects nothing when the eye is shut for want of signal-to-noise, and
disturbs a loop that was merely struggling. Made settable and A/B'd on the
hardware at 12 000: median 0.834 with it, 0.868 without, 7 retrains against 8. It
is neutral there, and it is what fixed the loopback. Kept.

### The longer equaliser, and why it was not built

The previous section ended with "ours needs a lower residual to go further: a
longer equaliser, or the precoding real V.32bis modems use. That is a bounded
piece of work with a number attached to it." The number was 12.9%, against a
15.4% margin at 12 000, and it looked like our equaliser's.

It was not. The equaliser was never the limit, and the honest record of this is
worth more than the code would have been.

**What the sweeps said.** On a hardware capture of the Conexant at 12 000, in a
window checked to contain only data:

| | residual |
|---|---|
| 21 taps, default step | 13.8% |
| 41 taps | 13.0% |
| 61 taps | 12.6% |
| 41 taps, step ÷4 | 13.2% |
| 41 taps, step ÷20 | 12.2% |
| carrier loop ÷16 | 12.6% |
| timing loop ÷16 | 13.6% |
| both loops ÷4 | 12.9% |

Tripling the equaliser buys 1.2 points. Twentieth the step buys 1.6. Narrowing
either loop buys nothing. And the error's autocorrelation at lag 1 is **0.026** —
it is white, which is what intersymbol interference is not. Everything pointed
away from the receiver, and I was still reading the table as "the equaliser needs
to be longer".

**What settled it was a second transmitter.** The same receiver, the same default
**21 taps**, the same channel, the same rate — with the Cirrus at the far end
instead of the Conexant:

| far end at 12 000 | median distance | residual | characters | framing errors |
|---|---|---|---|---|
| Conexant | 0.834 | **12.9%** | 0 | link never settled |
| Cirrus | **0.084** | **1.3%** | 547, 99.5% printable | **0** |

A factor of ten, with nothing changed on our side. Our receiver does 12 000 at
1.3% and it always could. The 12.9% was the **Conexant's transmitter**, and there
was nothing for a longer equaliser to fix.

So the longer equaliser was not built. The sweeps above are kept because a
measured negative on a plausible hypothesis is worth having, and because the
sweep was pointing at the answer — white error, no response to span, no response
to loop bandwidth — for three experiments before I took the hint.

**Where the rates actually stand against hardware:**

| rate | our transmit | our receive | notes |
|---|---|---|---|
| 4800, 7200, 9600 | decoded by both | clean from both | works |
| 12 000 | decoded by the Conexant (585-char run) | clean from the **Cirrus** (1.3%, 0 framing errors); fails from the Conexant | works, with the right far end |
| 14 400 | decoded by the Conexant | fails from the Conexant | does not hold with either: the Conexant's transmitter is too noisy for us, the Cirrus's receiver cannot decode our trellis at all |

Neither remaining failure is ours. That is a different and better answer than "our
receiver needs work", and the only way to get it was to stop measuring against one
modem.

**The lesson, for the third time in this document.** "Nothing useful at the far
DTE" was read as our transmitter being wrong; it was V.42. The Cirrus rejecting our
trellis was read as our transmitter being wrong; it was its receiver. A 12.9%
residual was read as our equaliser being short; it was the far end's transmitter.
Each time the diagnosis needed a **second reference** — a second modem, a bridged
call, another transmitter — and each time the single-reference reading pointed at
our own code. One measurement against one peer cannot tell you which end owns a
defect, however carefully it is made.

## Still not built
- **V.32bis §8, rate renegotiation.** The rates, the coding and the rate signal
  are built; the procedure for changing rate mid-call is not. It has its own
  preamble and initialises the differential encoder and scrambler differently
  (§5.3), so it is a real piece of work rather than a parameter.
- **V.32bis §6/§7's own start-up and retrain text.** The FSM negotiates and
  carries all five rates through V.32's §5.4 and §5.5, which V.32bis largely
  restates; the places where its §6 and §7 differ in detail have not been read
  clause by clause the way V.32's were.
- **V.42/LAPM.** Not part of V.32, but both modems default to it, and until it is
  implemented every hardware call needs error correction switched off by hand —
  with a different command set per modem.
- **A longer equaliser.** Measured not to be the limit; see above. If a rate above
  12 000 is ever wanted against a *clean* far end, this is where to look again —
  but with a second transmitter to compare against from the start.
- **An echo canceller.** Note 3 to §5.4 permits a training sequence for one and
  §5.2.3 says TRN trains it. Our measured echo return loss is 31.9 dB, so it has
  never been needed on this rig; it would be on a two-wire circuit.
- **§6's V.54 test loops.**
