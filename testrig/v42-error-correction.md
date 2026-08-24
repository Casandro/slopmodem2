# V.42: error correction, and why it was needed

Every live call in `v32-ans-path.md` had to have error correction switched off by
hand, with a different command set per modem, because both default to V.42 and a
modem negotiating LAPM with a soft modem that speaks only V.14 delivers nothing to
its DTE. That detour is written up there. This is the start of not needing it.

Two layers so far, both testable without a line, and both checkable against
something the Recommendation *prints* rather than against our own arithmetic.

## 8.1.1 Frame structure

Ordinary HDLC: the flag `01111110`, a 0 inserted after every five contiguous 1s,
address (one or two octets), control (one or two — §8.2.2 makes it two for the
frame types with sequence numbers), information, and a 16- or 32-bit frame check
sequence.

**§8.1.1.6 gives away a conformance value.** It states the receiver's residue in
the absence of errors: `0001 1101 0000 1111 (x15 through x0)`, and the equivalent
for 32 bits. That is a printed number to test against instead of a property to
derive, and it caught a bit-order question immediately: a table-driven CRC holds
the register reflected, where the same value reads **0xF0B8**. They are one number
in two conventions.

That is worth doing carefully rather than settling by trial. Both constants are
kept — `FCS16_SPEC_GOOD = 0x1D0F` as printed, `FCS16_GOOD = 0xF0B8` as the
register holds it — and the test asserts the *relationship* through an explicit
bit reversal, so it cannot pass by having picked whichever constant happened to
work. The 32-bit pair is 0xC704DD7B and 0xDEBB20E3, checked the same way.

The other thing worth testing is what stuffing is *for*: no flag pattern can occur
inside a frame. Fed eight octets of all-ones — the worst case — the stuffed body
contains zero occurrences of `01111110` in 112 bits.

§8.1.3 says invalid frames are discarded "without further action", which is easy
to implement and easy to make undebuggable. The deframer counts what it dropped
and why: bad FCS, too short, aborted, oversize. "The far end is silent" and "the
far end is sending frames we are throwing away" look identical from the outside,
and this project has already spent a long time on one such confusion.

## 7.2.1 Detection phase

This is how each end finds out whether the other speaks V.42 at all, and it is
neatly designed: the patterns are **asynchronous characters on a synchronous
link** — start bit, seven data bits low-order first, parity, stop bit — so they
can be recognised by a DCE that has no idea what a frame is.

| | pattern | characters |
|---|---|---|
| ODP, originator | `0 1000 1000 1 11…11 0 1000 1001 1 11…11` | DC1 even parity, DC1 odd parity |
| ADP, V.42 supported | `0 1010 0010 1 11…11 0 1100 0010 1 11…11` | (E) then (C) |
| ADP, no error correction | `0 1010 0010 1 11…11 0 0000 0000 1 11…11` | (E) then (Null) |

Separated and followed by 8 to 16 ones. T400, the detection timer, is 750 ms.

**The patterns are transcribed as the literal bits the Recommendation prints**,
not reconstructed from a character plus a parity rule — because the printed
patterns do not follow one rule. DC1 appears with even parity and then odd, which
is the point of it; but (E), (C) and (Null) are all printed with a parity bit of
0, which is odd parity for (E) and (C) and even for (Null). Deriving them would
mean choosing a convention the table does not use. The test checks both: that the
bits match the printed patterns exactly, and that their seven data bits read
low-order first give 0x11, 0x45, 0x43 and 0x00.

**One trap, found by testing the negative case.** §7.2.1.2 asks the originator for
"the characters from at least two adjacent ADPs" — four characters. Four
repetitions of the *no*-error-correction ADP also contain four (E) characters, so
a detector that counted the characters it recognised would report V.42 support
from a pattern that says the opposite. Both clauses actually ask for
*alternation*: §7.2.1.3 says "four DC1s of alternating parity". The detector's
positive answer is therefore the alternation test and nothing else — there is no
API by which a caller can ask the weaker question.

Measured, both roles: an answerer that supports V.42 and one that does not both
reach agreement at 0.24 s, an originator alone falls back at T400, and an answerer
alone sends nothing but mark and then falls back.

## 8.2 to 8.5 LAPM

All the encodings are Table 6's, Table 7's and Table 8's, and they turn out to be
the familiar LAPB ones — SABME 0x7F, UA 0x73, DISC 0x53, DM 0x1F, XID 0xAF. Bit 1
is the first transmitted, so in an octet it is the least significant, which is the
only thing that needs care.

**Table 6 is worth testing against the table.** The C/R bit depends on the role
*and* on whether the frame is a command or a response: a command from the
originator and a response from the answerer both carry 1, the other two carry 0.
Both ends making the same mistake is completely invisible in a loopback, so it is
checked against the four printed rows rather than against itself.

Sequence numbers are modulo 128, so the control field is two octets for the
formats that carry them. The defaults of §9.2 are used: N401 = 128 octets,
window k = 15, DLCI 0.

### What it does

`Lapm` works in frames and `Link` puts the framing round it, so the protocol can
be driven and tested without a modulator and then dropped onto a data phase
unchanged. Establishment by SABME/UA with T401 and N400 retries; I-frame transfer
with V(S), V(A) and V(R); segmentation at N401; the window; acknowledgement by RR;
REJ on a sequence error and retransmission; DISC/UA release.

Measured: 900 octets cross in 8 I frames with V(A) caught up to V(S); one dropped
I frame is recovered and the data is still exact; a duplicated I frame is not
delivered twice; a sender that never hears an acknowledgement stops at exactly
k = 15 outstanding frames; with no answer at all, SABME is retried N400 times and
the attempt fails. Over the bit stream, 1080 octets in 10 frames in 197 ms, and a
single flipped bit costs one discarded frame and is recovered.

### The bug the loss test found

The first version delivered 256 of 900 octets after one dropped frame, with the
REJ arriving and **zero** retransmissions. §8.4.5's N(R) acknowledges everything
below it, so V(A) advances to N(R) — and §8.4.6 also describes setting V(S) back
to N(R). Doing both leaves V(A) equal to V(S), and a retransmission loop that
walks from one to the other has nothing to walk over.

The frames to repeat are exactly the ones still held unacknowledged, so that is
what drives it now, and V(S) does not need to move at all. Worth recording
because the code read like the clause it came from and was still wrong: two
correct statements about two variables, and an interval between them that had
quietly become empty.

This is also the one test that could only have been written by deliberately
breaking the channel. Establishment, transfer, windowing and release all passed
on the first run; every defect was in the recovery path, which is the part that
never executes when things work.

## 12.2 XID, and what the modems actually do

The first live call was a surprise. The V.32 handshake reached 9600 trellis, our
detection phase waited T400 for an ODP, saw none, and fell back to V.14 — while
the modem sat there retransmitting frames at us. The captured data phase said so
plainly: the ones fraction over the whole 10 seconds was a rock-steady 0.75,
which is 6/8, which is HDLC flag fill. Deframing it gave **eight identical XID
commands**.

So no detection phase at all. Section 7.2.1.2 permits that — "the detection phase
actions by the originator may be disabled by the user. In this case, the
originator moves directly to the protocol establishment phase" — and 8.10.2's
note contemplates XID as "the first protocol frame following the detection phase
(if used) or establishment of the physical connection (if the detection phase is
not used)".

And the answerer's obligation is spelled out in the very clause I had
implemented. 7.2.1.3 gives it *two* exits:

> the control function of the answerer shall transmit 1-bits (mark) until
> termination of the detection phase, receipt of the ODP, **or detection of the
> start of the protocol phase (the start of the protocol phase is indicated by
> receipt of continuous flags, or of an LAPM or alternative procedure protocol
> frame)**

I had built the ODP exit and not the other one. The clause even repeats it in the
fallback rule — "the ODP is not observed ... **and the start of the protocol
establishment phase is not observed within the same period**". Reading a clause
and implementing half of it is a different failure from misreading it, and the
only reason it surfaced is that a real modem takes the half I skipped.

Flag detection counts four *consecutive* flags rather than looking for one: 0x7E
turns up in scrambled data about once every 256 bits, and the ODP is mostly ones.

### The captured XID

    82 80 00 13 03 03 8A 89 00 05 02 04 00 06 02 04 00 07 01 0F 08 01 0F

FI 0x82 general purpose, GI 0x80 parameter negotiation, GL 19, then Table 11a's
five parameters: the HDLC optional-functions mask, N401 both directions, and k
both directions. Every one of them is the default — N401 = 0x0400 bits = 128
octets (Note 3 carries it in *bits*), k = 15 — and the mask requests no optional
procedure at all, only the conformance bits 2, 4, 8, 9, 12 and 16 that Note 1
demands and then tells the receiver to ignore. Note also PL = 3 on PI 3 where
Note 1 says 4.

The frame is kept verbatim in the tests. A real artefact catches what a
self-consistent round trip cannot.

Two things in the encoding are worth care. **Note 3**: N401 travels in bits and is
used in octets. **Note 2**: transmit and receive are relative to the sender, so
the initiator's PI 5 is answered by the responder's PI 6. With everything at its
default the crossed and uncrossed responses are byte-identical, so a loopback
cannot tell them apart — which is why the tests ask with values that are not
default.

### One quirk per modem, and neither is in the Recommendation

Our spec-conformant XID response went unanswered. The modem retransmitted, we
answered again, fourteen times. The first useful step was to establish that this
was rejection and not loss: an HDLC frame dies on one bit error, so "ignored" and
"never arrived" look identical from our end. Repeating the response eight times
per received command put **112 responses** on the line and produced nothing —
112 frames do not all die, so the content was being rejected.

Then, since the modem retransmits about once a second, each retry could be
answered *differently*. Six variants, one per retry, and whichever is in flight
when SABME appears is the one it accepts:

| variant | Conexant CX93010 | Cirrus CL-MD56xx |
|---|---|---|
| spec: PI 3 present, 4 octets, C/R = 1 | rejected (112 tries) | **accepted, first try** |
| C/R = 0 | rejected | — |
| PI 3 echoed at *their* 3-octet length | rejected | — |
| **PI 3 omitted entirely** | **accepted** | — |

The Conexant rejects any response carrying PI 3 — including one whose PI 3 is
byte-identical to what it sent us. The Cirrus takes the conformant response
immediately, and asks for N401 = 64 octets, which the negotiation correctly takes
the minimum of.

So the default stays spec-conformant and the omission is a per-modem workaround
(`--xid-no-opt`). Testing the second modem before changing the default is the only
reason it is not now wrong for the Cirrus. Omitting PI 3 is legal — 9.2.3's
"absence of a value indicates use of the default", and an all-zero
optional-functions mask says exactly what absence says — but it is not what the
other end wants.

## 8.4.6 The stall the far end's own acknowledgements caused

First connected call, and it moved 648 octets and then stopped: V(S) 87, V(A) 72,
fifteen frames outstanding, the window shut, **zero retransmissions and zero T401
expiries**. The far end had sent 580 RR frames, all carrying the same N(R).

`_ack` restarted T401 and cleared the retransmission counter on every frame that
carried an N(R), acknowledging or not. Each of those 580 RRs pushed the deadline
out another second, so the timer could never expire and the outstanding frames
were never repeated. A permanent stall, driven entirely by the far end being
talkative.

8.4.6 says it in a parenthesis:

> The error-correcting entity shall stop the timer T401 on receipt of a valid
> I frame or an RR, RNR, or REJ supervisory frame **with the N(R) higher than
> V(A) (actually acknowledging some I frames)**, or an REJ frame with an N(R)
> equal to V(A).

The parenthesis is the whole rule. Nothing else in the clause hints that a
*repeated* N(R) differs from a fresh one, and both readings behave identically
until a peer starts polling with a stale one.

## 8.4.8 Timer recovery

Fixing the timer exposed that what it triggered was wrong too. 8.4.8 does not
retransmit on expiry; it enters the **timer-recovery condition** and sends "an
appropriate supervisory command ... with the P bit set to 1" — an enquiry. The
peer's F = 1 response says what it actually holds, so the retransmission that
follows is informed rather than a guess. At N400 the connection terminates per
8.4.9.

The mirror matters as much for interop: an RR *command* with P = 1 must be
answered with a response carrying F = 1. That needs the Table 6 command/response
test, because two of our own entities without it answer each other's answers for
ever. And it cannot be tested by feeding an entity its own response — Table 6
gives the answerer's response and the originator's command the same C/R = 1, and
they are distinguished only by the fact that no entity receives its own frames.

## Throughput, and the scheduling that decided it

Our own transmit direction runs at 89 to 96% of the channel. The other direction
measured 41 to 42% — and did so on the Conexant and the Cirrus, at 9600 trellis
and 9600 nonredundant, over four runs, to within 1%.

**That consistency was the tell.** Two different chipsets do not agree to three
significant figures on anything. The limit was in the test harness: the dialling
script called `s.read(4000)` on a port with a 0.5 s timeout, so every loop
iteration blocked for half a second waiting for 4000 bytes that arrive at about
1100 a second, and in that time the flood wrote one 256-byte block. 256 bytes per
0.5 s is 512 byte/s. The measurement was reporting its own read timeout.

This is the same lesson this project has now learned several times over: calibrate
the estimator before believing what it says about the thing being measured. An
absolute number that reproduces suspiciously well across configurations that
*should* differ is evidence about the instrument, not the subject.

Fixed by reading only `in_waiting` and writing a larger block.

### What it actually carries

With the harness fixed, both directions land at about 90% of the line rate.

| | Cirrus, 9600 nonredundant | Conexant, 9600 trellis then a retrain to 4800 |
|---|---|---|
| octets received | 63 872 in 59.0 s | 26 240 in 47.9 s |
| | 1082 byte/s = 8657 bit/s = **90%** | 548 byte/s = 4384 bit/s = **91%** |
| octets delivered to its DTE | 62 656, 100% printable | 19 810 |
| frames | 1000, **none discarded** | 225, 3 bad FCS, 2 oversize |
| retransmissions | 0 resends, 0 T401 expiries | 75 resends, 7 T401 expiries |

**V.42 is not merely safer than V.14 here, it is faster.** V.14 spends ten bits on
every eight-bit character, so 9600 bit/s carries 7680 bit/s of user data at best —
80%. LAPM pays five octets of framing per 128 and no start or stop bits at all,
and measures 90%. Error correction and about 13% more throughput, from removing
the per-character overhead that V.14 exists to preserve.

The Conexant figure is from after a retrain, which is why its frame counts are
worse: three bad FCS, two oversize, 75 retransmissions. The 91% is what got
through in spite of that.

### The same lesson, a second time, in the other direction

Every absolute figure in the table above is the *modem to us* direction. Our own
transmit direction was measured later, with the rate caps lifted, and it came out
at **898 byte/s — at 9600 and at 12000 alike**. Same tell as before, same
conclusion available for the reading, and it was misread anyway.

The hypothesis was the LAPM window, and it fitted well enough to be believed. The
Cirrus negotiates `N401 64/64, k 15/15`, so 15 frames of 64 octets is 960 octets
in flight; 960 octets per 1.1 s round trip is 873 byte/s. A window is exactly the
kind of thing that produces a rate-independent ceiling, and `V(S) 119 V(A) 106`
said 13 of the 15 were outstanding when the call ended.

**It was not the window.** The measurement that settles it is throughput against
round trip, because the two candidates predict different shapes: a window that
counts frames in flight gives `min(L, W/RTT)`, flat until the window is smaller
than the bandwidth-delay product; a window also consumed by frames still sitting
in our own transmit queue has to cover the drain as well, giving
`1/(1/L + RTT/W)`, which falls away from the start. Offline, over a clean channel
with a delay line:

| round trip | 40 | 240 | 440 | 840 ms |
|---|---|---|---|---|
| measured | 11 455 | 11 467 | 11 470 | 11 474 bit/s |

Flat, against a framing-adjusted ceiling of 11 549, and still 11 246 with both
directions saturated at once. The queue-consuming model predicts about 8 440 at
440 ms. The window slides on each acknowledgement the way 8.4.1 intends.

The ceiling was one slice in the harness. `(pat + pat)[i:i + a.feed]` cannot
return more than `len(pat)` octets, so with a 9-octet `--send SLOPMODEM` every
20 ms frame carried 18 whatever `--feed` said — and because the chunk was then
exactly 18, `i = sent % 9` stayed pinned at 0 for the whole call. 18 octets per
frame is 900 byte/s, 7 200 bit/s, identical at every line rate. The bug bites
only when `--feed` exceeds `2 * len(pat)`, which is why it had gone unnoticed:
the cap is invisible with a long enough pattern.

So this is the `s.read(4000)` lesson twice, in opposite directions, and the second
time the tell was recognised and then attributed to the subject rather than the
instrument. The rule wants strengthening: a rate-independent number means
*calibrate the instrument*, and the calibration has to be an experiment the
instrument cannot pass by accident. `test_v42.py` now runs one — throughput
against round trip over a delay line, which the other link tests could never see
because they swap bit vectors with no delay at all, and with no delay the window
can never be the thing that binds.

With the feeder able to fill a frame, both directions run at the line:

| | modem → soft | soft → modem | frames |
|---|---|---|---|
| **12000** V.32bis trellis, 83.2 s | 1352 byte/s = **10 818 bit/s (90%)** | 1349 byte/s = **10 789 bit/s (90%)** | 1760, **none discarded** |
| **9600** V.32 non-redundant, 83.9 s | 1082 byte/s = **8 659 bit/s (90%)** | 1068 byte/s = **8 545 bit/s (89%)** | 1421, **none discarded** |

100% printable both ways, no resends, no T401 expiries, and 4 096 octets still
queued at the end of each — backpressured against the line rather than starved by
the feeder, which is the state a throughput measurement should end in.

**One thing above is now unreconciled.** The claim opening this section — that our
transmit direction "runs at 89 to 96% of the channel" — cannot be squared with a
feeder that could not exceed 900 byte/s, which is 75% at 9600 and was in the code
from the first commit. Either those runs used a longer `--send` pattern, in which
case the cap never applied to them, or the percentage was a delivery ratio rather
than a fraction of capacity. The invocations were not recorded, so it cannot be
settled from what is here. It is left standing and flagged rather than quietly
rewritten.

And a note on what "90% of the channel" can even mean: the ceiling is set by
framing, five octets per `N401`, so it is `N401/(N401+5)` of the line — 96% at the
default 128 and 93% at the 64 the Cirrus negotiates. The 90% figures above are 97
to 99% of the ceiling that was actually available, not 90% of an achievable 100%.

### V.14 has no backpressure, and needs pacing rather than a bound

Lifting the feeder cap exposed a second defect in the same three lines. The gate
read `m.ec is None or (up and outq < 4096)`, so with no error-correcting entity it
was unconditionally open. V.42 has a window to push back with; V.14 has nothing —
no window, no retransmission, nothing downstream that can say stop. `--feed 64`
offers 3 200 char/s into a channel carrying 1 200, the converter's queue runs past
its hiwater, and it begins deleting *every* stop bit, which is a stream the far
framer cannot acquire on. It measured **27.9% printable** and looked like a line
fault. The 18-octet cap had been holding V.14 runs under the line rate by
accident, which is the only reason they had always read clean.

Bounding the queue is necessary and not sufficient. At 64 characters we stopped
filling RAM and deleted no stop bits at all — and the far end still read **54.6%
printable with no recoverable pattern**, because a bound still permits offering
*exactly* the line rate, and V.14 at exactly the line rate leaves the two clocks
no margin. `dte.AsyncEncoder`'s docstring already said so about the slips that came
back from the Conexant; the fix had to be pacing, not just a ceiling.

Paced to 95% of the line's own character budget — `rate/500` characters per 20 ms
frame, V.14 spending ten bits on eight — and bounded as well:

| feeder | our transmit | what the far DTE saw |
|---|---|---|
| original 18-octet cap | 900 byte/s | clean, but a 7 200 bit/s ceiling |
| cap lifted, queue bounded only | line rate, no margin | 54.6% printable, no pattern recoverable |
| cap lifted and paced | 86 174 chars, **0 stop bits deleted** | 94% printable, **37 555 of 37 555 correct**, BER 0 |

The 94% rather than 100% is one 5.5 retrain at 14 s diluting the ratio; the
pattern check runs on the longest clean run and is exact.

### The Conexant retrains at 14.2 seconds, and why

Six runs triggered a 5.5 retrain 5.3 to 5.9 s after the data phase opened — at
14.054, 14.195, 14.198, 13.693 and 14.355 s. Within a few hundred milliseconds
across six calls is not a line event, and afterwards the rate is 4800
nonredundant rather than 9600 trellis. One earlier call ran the full 58 s at
9600 trellis, so it is probabilistic rather than unconditional.

Working it out took four wrong turns, each killed by a measurement.

**Is the modem retraining, or is our detector crying wolf?** The event text reads
"the far end is holding a carrier state", but that is *our detector's
conclusion*, not evidence about the modem. The captured audio settles it: at
13.580 s the far end switches from data to a pure 1800 Hz carrier at **99.4% of
total power**, full amplitude, held for 280 ms, then goes silent. That is signal
AA. The modem initiates, and the detector is right.

**Is it N400 exhaustion on XID?** Timestamping received frames says no. The modem
sends only **two** XIDs before retraining, at 11.26 and 12.50 s, and the retrain
lands 1.19 s later — about when a third would have been due. It gives up after two
attempts and asks for a retrain instead of trying again.

**Is the link marginal?** Here the receiver's own diagnostic misled me. It prints
"within 0.35 of a point 88.8%", which sounds marginal until you notice that the
9600 trellis constellation has d_min = 1.414, so a decision boundary sits at
0.707 and 0.35 is only *half way* to it. Measured properly, the received signal is
Es/N0 = **22.05 dB** with P(symbol error) 3e-5, so a 250-bit frame survives 99.8%
of the time. Receive-side frame loss explains nothing. A diagnostic threshold is
not a decision boundary.

**Is our XID response malformed?** No. Deframing our *own transmitted audio* over
the 9600 window recovers four byte-exact responses:

    addr 03  ctl AF  XID  82 80 000E 05 02 0400 06 02 0400 07 01 0F 08 01 0F

so the framing, stuffing, FCS, scrambler, trellis encoder and modulator are all
doing their jobs.

### It is echo, and at 9600 it is a trade-off we cannot win

What is left is the *forward* path, which we cannot measure directly — so measure
its consequence. Raising our transmit level from −24 to −18 dBFS **removes the
retrain entirely**: 9600 trellis holds for the whole call and LAPM connects. But
our own receive collapses, from 88% within 0.35 to **30.9%**, with 161 bad FCS.

Our transmit level should have nothing to do with our own receiver, and the reason
it does is measurable. Cross-correlating our transmitted audio against our
received audio from the same call gives a sharp peak at **rho = 0.112, an echo
return loss of about 19 dB** — against a correlation floor of 0.007. (The delay
was first reported here as 9.6 ms. That was wrong: the capture files are offset by
the RTP pump's two priming frames, and the real figure is about 50 ms. See
`sip-audio-path.md`.) Three samples wide, decaying to 0.03 by 60 ms: a real impulse response,
not an artefact of both signals sharing an 1800 Hz carrier. Both modems hang off
FXS ports of the same FRITZ!Box, so this is its analogue hybrid reflecting us back
at ourselves, and 19 dB is an ordinary figure for a two-wire hybrid.

That sets a trade-off the transmit level cannot escape:

| our level | echo in our own receiver | our eye at 9600T | result |
|---|---|---|---|
| −24 dBFS | −43 dBFS, 18 dB under the wanted signal | 88% within 0.35 | modem cannot decode us, **retrains at 14.05–14.36 s** |
| −21 dBFS | −40 dBFS, 15 dB under | — | still retrains, at 14.09 s |
| −18 dBFS | −37 dBFS, 12 dB under | **31%**, 161 bad FCS | no retrain, LAPM connects, 1% throughput |
| −15 dBFS | −34 dBFS, 9 dB under | — | no retrain for 32 s, then *our* receiver loses lock |

The window is empty. Every level either leaves the modem unable to decode a
32-point constellation or drowns our own receiver in our own echo. Below the
retrain, at 4800, the same calls run at 92% with a 100%-clean eye — the rate is
not the problem, the margin is.

At equal transmitted power the 9600 trellis constellation's minimum distance is
0.447 against QPSK's 1.414 — **10 dB less margin**, about 6 dB after the code's
gain. So 4800 works at either level and 9600 trellis works at neither. A T/2 equaliser with 41 taps spans 8.5 ms, so it cannot reach an echo at 50 ms
either way — which withdraws an explanation offered here earlier. The 21-to-41 tap
improvement, median error 0.55 to 0.22 with nothing gained at 61, is real and
reproducible, but it is not the equaliser reaching the echo and the cause is
unexplained.

**The missing piece is an echo canceller.** Every real V.32 modem has one, because
V.32 is full duplex on a two-wire circuit, and V.8's ANSam phase reversals exist
to switch off the *network's* canceller precisely because the modems do it
themselves. We have none, so our own transmitter is the dominant noise source in
our own receiver and 9600 trellis has no margin to spare. That is the real open
item, and it is a piece of DSP rather than a tuning fix.

What it did expose is that **V.42 has to survive a V.32 retrain.** The first
version rebuilt the session and ran a fresh detection phase, which desynchronises
the link against a far end that keeps its own V(S), V(A) and V(R): measured, V(R)
of 2 after 181 I frames received, the window jammed, 56 retransmissions. Keeping
the session and only re-attaching the bit tap works — the run above crossed a
retrain and then moved 26 240 octets.

### The scheduling change that came from believing it

Before finding the harness fault I had a plausible story for 42%: `Lapm.poll`
built the whole window in one call and `Link` queued all of it as bits, so an RR
generated on the next received frame went out *behind* up to fifteen 128-octet
I frames — 15 360 bits, 1.6 s at 9600. So replies were made to jump the queue,
and `poll` was given a limit on how many I frames it would produce per call.

The queue-depth limit turned out to be actively harmful and is now off by default:
against the Cirrus, a limit of two frames produced 15 retransmissions and 6 T401
expiries where no limit produced 1302 frames with neither. Reasoning about a
number the instrument had invented produced a change that made things worse.

Queue jumping stayed, because the bug it exposed was real.

The queue holds whole frames rather than a flat bit stream, because a supervisory
frame inserted at the front of a bit queue splices itself into the middle of
whatever is already being transmitted.

### And then the Cirrus rejected the frames

The reordering broke an invariant I had not thought about. Frames were built with
the N(R) that was current at the time, and then transmitted out of that order — so
a frame built earlier went out later carrying a *lower* N(R). N(R) going backwards
is an invalid N(R).

The Cirrus said so exactly. It sent an **FRMR** and then a DISC, and FRMR's
information field is a diagnosis rather than a complaint:

    rejected control field 2C 08   ->  N(S) 22, N(R) 4
    its own V(S) 8, V(R) 22
    W 0  X 0  Y 0  Z 1             ->  Z: invalid N(R)

Its V(S) was 8 and we acknowledged 4 after having already acknowledged more.

The fix is at the root rather than at the symptom: **bind N(R) when the frame is
rendered to bits, not when it is built.** Every frame that carries an N(R) then
carries the current V(R), so the sequence on the wire is monotonic whatever order
the frames were made in — and an acknowledgement is never late either, because
whatever goes out next is already carrying it. Front-queuing becomes safe rather
than merely useful.

The same defect drew different reactions, which is worth remembering: the Cirrus
sent FRMR and tore the connection down, the Conexant issued a REJ and carried on
at 79% of a 4800 channel. One peer diagnosing a fault precisely is worth more
than another peer tolerating it.

### Not implemented

SREJ, RNR flow control, and FRMR reporting. We answer XID but never initiate
one, which 7.2.2 permits: "negotiation/indication may be omitted if default
parameter values and procedures are satisfactory". V.42bis compression is
switched off at the modem for the same reason — a negotiated compression we
cannot perform would arrive as BTLZ codewords inside perfectly good I frames.

## Not built yet

- **XID negotiation** (§8.6), and with it any parameter other than the §9.2
  defaults. Also SREJ, RNR flow control, FRMR, and §8.5.3's timer recovery.
- **Wiring it into the data phase**, alongside or instead of the V.14 converter,
  with the detection phase deciding which.
- **§8.5's break handling**, §7.8's loopback test, and the Annex A MNP-compatible
  alternative.
- **V.8's bypass.** Appendix VI.2 notes that V.8 provides a way to skip the
  detection phase entirely when both ends have already said they support V.42 —
  which this rig cannot use, since the FRITZ!Box destroys ANSam and V.8 never
  happens. Worth knowing that the detection phase is not optional *here*.
