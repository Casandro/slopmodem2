# Test Rig — SIP endpoint `**620` and the modem audio path

Companion to `modem.md` (hardware modems) and `ata.md` (credentials). This document covers the
third endpoint on the rig — the SIP account `**620` — and what was measured about the audio path
between it and the two hardware modems.

Everything below was produced live against the rig. No SIP client software was installed on either
host, so a minimal SIP/RTP user agent was written for this; it lives in `testrig/tools/`.

## The endpoint

| Property | Value |
|---|---|
| Registrar / PBX | `192.168.5.174` — **AVM FRITZ!Box 7490**, firmware `113.07.62 (Dec 1 2025)` |
| Internal number | `**620` |
| Transport | SIP over UDP/5060, digest auth (MD5) |
| Registration | `REGISTER` → `200 OK`, contact bound as `sip:<user>@<local-ip>:<port>` |
| Negotiated codec | **G.711 A-law (PCMA, payload type 8)** at 8 kHz |
| Credentials | in `ata.md` — deliberately not duplicated here or in the tools |

The FRITZ!Box offers `8 0 2 102 100 99 97 101` on inbound calls (PCMA, PCMU, G.726-32,
plus dynamic types) and `8 0 101` outbound. PCMA was selected in both directions.

`tcp/5060`, `tcp/80` and `tcp/443` are open on the box. A passive SIP `OPTIONS` to **udp**/5060
drew no reply, which is normal for a FRITZ!Box from an unregistered peer — registration works
regardless.

## Outbound: `**620` calls the modems

Both hardware modems were armed with `ATS0=1` (auto-answer) and `ATX4`, holding DTR high, then
called from `**620`. Both rang and answered, and their full answer-tone sequence came back over
RTP.

| | `**1` — Cirrus CL-MD56xx | `**2` — Conexant CX93001 |
|---|---|---|
| `INVITE` result | `200 OK` | `200 OK` |
| `RING` at modem | 2.20 s | 2.40 s |
| RTP received | 762 pkts / 15.2 s audio | 767 pkts / 15.3 s audio |
| Frame rate | 50.0 fps steady, ~111-frame burst in first second | same |
| Audio level | RMS 3203 (−20.2 dBFS) | RMS 3009 (−20.7 dBFS) |

### Answer-tone timeline

Dominant frequency per 250 ms slice, with *purity* = fraction of the slice's total power in that
single tone (1.00 = a mathematically pure sinusoid):

**`**1` (Cirrus):**

| Time | Freq | Purity | Interpretation |
|---|---|---|---|
| 0.00–0.75 s | 425 Hz | 1.00 | German call-progress tone from the FRITZ!Box |
| 1.00–5.75 s | **2100 Hz** | 1.00 | **V.8/V.25 answer tone (ANS/ANSam)** — ~4.8 s, spec-conformant |
| 6.00–8.75 s | 2250 Hz | 0.98 | **V.22bis `USB1`** — unscrambled binary 1 in the high channel |
| 9.00–9.50 s | 600 Hz | 0.73 | V.32-family startup component |
| 9.75–11.75 s | **1650 Hz** | 1.00 | **V.21 channel-2 mark** — V.8 `JM` / V.21 signalling |
| 12.00–14.75 s | 1300 Hz | 1.00 | V.23-family tone (fallback attempt) |

The 2250 Hz tone was unidentified for a long time and is worth writing down, because the arithmetic
is easy to get wrong. `USB1` is unscrambled binary 1 at 1200 bit/s, so every dibit is `11`, which
Table 1/V.22bis makes a 270° quadrant change — i.e. **−90° per symbol**. At 600 baud that is
−150 Hz, putting the tone at 2400 − 150 = **2250 Hz**. Reading the 270° as 270°/symbol instead
gives −450 Hz and a prediction of 1950 Hz, where a detector finds nothing at all. See the
originating section of `v22-modem.md`, where this is what the calling modem has to listen for.


**`**2` (Conexant):**

| Time | Freq | Purity | Interpretation |
|---|---|---|---|
| 0.00–0.75 s | 425 Hz | 1.00 | call-progress tone |
| 1.00–2.75 s | — | — | silence |
| 3.00–8.00 s | **2100 Hz** | 1.00 | **V.8/V.25 answer tone** — ~5 s |
| 9.00–11.00 s | 2250 Hz | 0.91 | **V.22bis `USB1`** (see the note above) |
| 12.00–15.00 s | 600 Hz | 0.68 | V.32-family startup component |

Both modems walk down through their modulation set looking for a peer that never answers (the SIP
side sent only silence), which is exactly the expected behaviour. The Cirrus gets further through
the sequence within the capture window than the Conexant.

**The important result is the purity column.** Tones arrive at 0.98–1.00 purity and correct
amplitude after traversing FXS → FRITZ!Box → G.711 A-law → RTP → UDP. The path introduces no
audible distortion, so it carries modem signalling faithfully.

`**1` additionally reported `CONNECT 115200` about 18 s in, followed immediately by `NO CARRIER`.
That is the DTE-side rate report, not a real data connection — there was no modem at the far end.
It is noted here because it means a bare `CONNECT` string must **not** be treated as proof of a
working link.

## Inbound: the modems call `**620`

The same UA also answers. `**1` dialled `**620` with `ATDT**620`:

- `INVITE` arrived from `"Telefon" <sip:**1@fritz.box>` — an **independent confirmation of the
  number assignment** in `modem.md`: the FRITZ!Box itself labels the Cirrus as `**1`.
- The UA replied `100 Trying` then `200 OK` with an SDP answer; media flowed immediately.

### A calling modem is silent until it hears an answer tone

First inbound attempt, with the SIP side sending only silence: **798 RTP packets, 16.0 s, RMS 8 —
pure silence in both directions.** The modem never emitted a single tone and never reported a
result code.

This is correct modem behaviour, not a fault: the *calling* modem waits to hear the answer tone
before transmitting anything (V.25 calling tone is off by default on both units). Sending silence
at an originating modem produces nothing at all.

### Sending a real ANSam gets a response

A V.8 `ANSam` was then synthesised on the SIP side and sent as the answer tone — 2100 Hz,
amplitude-modulated at 15 Hz to 20 % depth, with a phase reversal every 450 ms, A-law encoded
(`ansam.py`). Result:

| Time | Freq | Purity | RMS |
|---|---|---|---|
| 0.00–0.875 s | — | — | silence (modem still waiting) |
| 1.000 s | 1800 Hz | 0.23 | 62 — onset |
| 1.125 s | 1800 Hz | 0.75 | 1569 — rising |
| 1.250–21.75 s | **1800 Hz** = V.32 `AA` | **1.00** | ~1960, rock steady |

The modem began transmitting **within one second** of hearing the ANSam, and held a pure 1800 Hz
tone for the remaining 20 s. It had been completely silent before.

**This is the proof that the audio path works in both directions**: a synthesised tone travelled
SIP → RTP → FRITZ!Box → FXS → modem, the modem's DSP detected it and changed state, and its
response travelled all the way back.

The 1800 Hz tone is **V.32 signal `AA`**, confirmed against `../references/ITU-T_V.32_1993-03.pdf`:
V.32 defines `AA` as "Tone at 1800 ± 7 Hz generated by repetitively transmitting carrier state A",
and notes that the answering modem "may disconnect from the line if the 1800 ± 7 Hz tone is not
detected following transmission of" the answer tone. So the modem did exactly what a V.32 call-mode
modem should: it heard an answer tone and replied with `AA`, for the specified minimum of ≥ 1 s.

It is *not* the V.22/V.22bis 1800 Hz guard tone — that is transmitted only in the high channel, i.e.
by the answering modem, whereas here our modem was the caller.

The modem then stayed in that state, holding `AA` long after the 6 s ANSam ended, because nothing
followed up with the rest of the V.32 handshake (`AC`/`CA`, then the training sequences). Getting
past this point is what a soft-modem would have to implement.

## RTP pacing — lock to the peer's frame rate

**Rule: send exactly one RTP frame for every frame received.** Do not pace outbound RTP off the
local wall clock.

The two endpoints have independent sample clocks, and neither is exact. A sender that ticks its own
20 ms timer drifts against the peer no matter how carefully the timer is written, and the drift is
one-directional and cumulative — we either starve or flood the peer's jitter buffer. For a
soft-modem that is fatal rather than cosmetic: a slipping sample clock destroys carrier tracking and
equaliser convergence, so training either fails or the link falls over mid-data-phase.

Letting the peer be the clock removes the problem entirely. In steady state the ratio is 1:1 and the
two ends stay locked regardless of how far the peer's actual rate sits from the nominal 50 fps.

### What the FRITZ!Box actually does

Measured per wall-second, both directions:

| | Mean rate | Steady state | First second |
|---|---|---|---|
| Outbound (`**620` → `**1`) | 54.4 fps | **50.0 fps** | **111 frames** |
| Inbound (`**1` → `**620`) | 50.0 fps | **50.0 fps** | 50 frames |

The box's clock is **exactly 50.0 fps** once running. The 54.4 fps mean on outbound calls is not a
clock offset — it is a **single burst of ~111 frames in the first wall-second**, about 1.2 s of
buffered audio flushed at call setup. Inbound calls show no burst at all, because there is nothing
buffered ahead of the answer.

This matters for the pacing choice. A wall-clock sender emits 50 frames during that first second
while 111 arrive, so it is ~61 frames — roughly **1.2 s** — behind before the call has properly
started, and a fixed-rate sender never recovers that offset. Receive-driven pacing emits 111 frames
in the same second and stays aligned.

### Verified

| | frames in | frames out | out/in | watchdog | audio vs wall |
|---|---|---|---|---|---|
| Outbound to `**1` | 762 | 764 | **1.003** | 0 | 15.2 s / 14.0 s |
| Inbound from `**1` | 1101 | 1103 | **1.002** | 0 | 22.0 s / 22.0 s |

The two surplus frames in each case are the priming burst, and no watchdog frames were needed.

### Two things receive-driven pacing needs

1. **A priming burst.** Send a couple of frames up front. The peer may wait to hear media before
   sending any, and a strictly reactive sender would then never start.
2. **A watchdog.** If the peer genuinely stops sending, a purely reactive sender stops too and both
   ends deadlock, each waiting for the other. `media.pump()` emits a frame if nothing has arrived
   for 60 ms and counts those separately, so a real 1:1 lock can be distinguished from a stream
   being carried by the watchdog. Always check that counter — `watchdog=0` is what a healthy lock
   looks like.

### Caveat for the soft-modem

Echoing a startup burst back 1:1 keeps *frame counts* aligned but pushes 111 frames at the peer in
one second, which could stress its jitter buffer. The 1:1 rule is the right transport-level
behaviour and is what is implemented here, but a real soft-modem should additionally feed inbound
frames into a jitter buffer and drive its modulator from a sample clock **derived from inbound frame
arrivals** rather than emitting demodulated-and-remodulated audio one-for-one in lockstep. That
refinement has not been built or tested.

## The path has echo: about 19 dB at 58 to 66 ms

Measured by cross-correlating our transmitted audio against our received audio
from the same call, in the data phase of a V.32 connection. Both modems hang off
FXS ports of the same FRITZ!Box, and what comes back is its analogue two-wire
hybrid reflecting our own signal; 19 dB is an unremarkable figure for a hybrid.

| where | delay | correlation | echo return loss |
|---|---|---|---|
| calls to the Conexant | **461 samples, 57.6 ms** | 0.115 | 19 dB |
| calls to the Conexant | **525 samples, 65.6 ms** | 0.130 | 18 dB |
| calls to the Cirrus | none found | ≤ 0.036 against a 0.051 threshold | — |
| (floor across all lags) | | 0.006 | 44 dB |

**The echo is per-port, not per-box.** The reflection happens at the hybrid the
*far* modem is plugged into, so a call to the Conexant reflects off the Conexant's
port. Two runs to the Cirrus found nothing above threshold at any lag.

### The delay cannot be under one RTP frame, and an earlier figure said it was

This section previously reported 9.6 ms, and that number was wrong in a way worth
recording, because it survived several rounds of use.

The delay cannot be shorter than one 20 ms frame. Our frame *k* is emitted only
after inbound frame *k* has been read, and whatever the box reflects has to be
packetised before it comes back, so the earliest our own signal can reappear is in
inbound frame *k+1*. A measured 9.6 ms would have the echo arriving before it was
transmitted.

The error was in the capture files, not the line. `rtp.pump` emits a priming burst
of two frames before any inbound arrives, and every emit is appended to
`out_audio` while `in_audio` only grows when a packet is received — the run logs
say so plainly, `in=3018 out=3020`. So the transmit file leads the receive file by
**320 samples**, and every lag measured from those files was that much too small:
9.6 ms was really 49.6 ms.

The canceller itself was never affected: it is fed inside the frame loop, where
its two streams advance together, so the lags in its own scan log are true. Those
are the numbers in the table above. The moral is the ordinary one — a
cross-correlation is only as good as the alignment of the two things being
correlated, and "impossible answer" is a stronger signal than "plausible answer",
which is why it took a physical argument rather than a bigger window to find.

**This matters more than it looks.** Without an echo canceller our own transmitter
is the dominant noise source in our own receiver, and the level we transmit at
sets our own receive SNR. A 4-point constellation has margin to spare; a 32-point
trellis constellation, with 10 dB less minimum distance at the same power, does
not. The consequences are worked through in `v42-error-correction.md` — it is why
V.32 at 9600 with trellis coding cannot be held against the Conexant at any
transmit level, while 4800 runs at 92%.

Every real V.32 modem carries an echo canceller for exactly this reason; V.8's
ANSam phase reversals exist to switch off the *network's* canceller because the
modems do the job themselves.

## What this does and does not establish

**Established:**

- `**620` registers and authenticates against the FRITZ!Box, and can place and receive calls.
- `**620` ↔ `**1` and `**620` ↔ `**2` both connect, with G.711 A-law media in both directions.
- The end-to-end audio path carries pure tones at 0.98–1.00 purity — clean enough for modem
  signalling.
- Both hardware modems respond to signalling delivered over that path: they emit their full answer
  sequence when called, and a calling modem changes state when sent a valid ANSam.

**Not established:**

- **No data connection has been made.** Nothing here reaches the `rules.md` bar of "at least a
  minute of data phase data transmitted in both directions". That needs a real modem at the SIP
  end — i.e. a soft-modem implementing at minimum a V.21 or V.22 handshake plus data phase — or a
  direct `**1` ↔ `**2` call between the two hardware modems.
- Whether the path supports full-rate V.34 (33.6 k) is unmeasured. Tone purity is necessary but
  not sufficient; V.34 also needs low delay-jitter and no gain hunting.
- A soft-modem now completes a full V.8 negotiation with both modems over this path; see
  **`v8-negotiation.md`**. That work also found that the FRITZ!Box FXS path applies a
  **narrowband 2100 Hz answer-tone regenerator**: a tone within roughly 2100 ± 5 Hz is replaced by
  a locally generated unmodulated 2100 Hz sine, in **both** directions (2095 → 2099.95 Hz towards
  the modem, 2103 → 2100.00 Hz coming back), which converts ANSam into plain ANS in transit. A
  1500 Hz control carries 20 % AM intact both ways, so the box — not either modem — is responsible.
  That is why the 1800 Hz `AA` response below was provoked: the modem was correctly reporting what
  it received.
- The `1800 Hz` signal **is** now identified (V.32 `AA`, see above).
- The `2250 Hz` signal **is now identified**: it is V.22bis `USB1`, unscrambled binary 1 at
  1200 bit/s in the high channel. Dibits `11` are +270° per symbol, which at 600 baud is a
  steady −150 Hz shift of the 2400 Hz carrier, giving exactly 2250 Hz. See
  `v22-modem.md`.
- The `1650 Hz` in the `**1` sequence is most consistent with V.21 channel 2 mark rather than the
  V.8bis `ESr` tone, on duration grounds (~2 s observed vs 100 ms nominal), but this rests on
  duration alone.

## Listening to the path with no modem in it

When 14 400 would not hold and every candidate on our side had been ruled out, the
obvious remaining suspect was the box: something the ATA does that a modem does
not expect. Testing that means getting the signal onto the line and off it again
with nothing demodulating in between, which the rig can do — `playraw.py` answers
a call and plays a real V.32bis data-phase signal at it, and `vcap.py` puts a
modem in voice mode (`+FCLASS=8`) so its own ADC records what arrives at the FXS
port. Our transmitter, the whole SIP and analog path, and no receiver anywhere.

**What it establishes.** The path does nothing gross. The capture runs at exactly
8000 samples/s, 0.03% of samples are exact zeros, there are no steps or dropouts,
the level is −25.9 dBFS — the same figure our receiver reports on live calls — and
out-of-band energy is 16 to 33 dB down with nothing spurious in it. Before any
equalisation the recording correlates 0.72 to 0.90 with what was sent. One
incidental number: our sample reaches the far ADC 613 ms later, with the two
clocks 46 ppm apart, though that includes the modem's own voice buffering and is
not a network latency figure.

**What it cannot establish, which took three tries to admit.**

*First try.* Run our own receiver over the capture. It read 9.2% at 14 400 —
strikingly close to the 9.4% live calls report, and it would have been written
down as confirmation. The control says otherwise: the same method reads **13% at
12 000**, on a path a real 12 000 call reads **1.4%** on. `_Rx` with `to_data()`
called cold never trains; in `eyewin.py` it is primed by eleven seconds of real
handshake first. The number was our receiver's cold acquisition, not the channel.

*Second try.* Drop the receiver and fit the capture against the reference — best
41-tap linear filter per block, so delay, gain and frequency response all come
out and only noise and nonlinearity are left. That gives 11.24% at 14 400. And
11.16% at 12 000. Two different signals, agreeing to within 0.1%, which is this
document's own standing signature for having measured the instrument.

*Third try, as calibration rather than measurement.* A pure 1800 Hz tone, which a
linear path predicts exactly, comes back at **7.13%** residue with a raw
correlation of 0.993. That is the floor, and it is the recorder: a voice-mode
front end is not the path a data receiver uses, and it has no obligation to be
linear or quiet.

| through the voice-mode capture | unexplained residue |
|---|---|
| pure 1800 Hz tone | **7.13%** |
| 12 000 data | 11.16% |
| 14 400 data | 11.24% |

So the experiment resolves gross faults and not fine ones. It would have caught a
dropout, a gain problem or a spurious tone. It cannot distinguish a channel at 1%
from one at 9%, which is exactly the distinction that was wanted.

**The ATA is ruled out anyway, by a control that already existed.** The identical
path — same box, same FXS port, same codec, the same evening — carries **12 000 at
1.4% residual for 83 seconds at 90% of the channel in both directions**. The box
sees 8 kHz A-law either way and cannot know what constellation is inside it, so
whatever fails at 14 400 cannot be something the ATA does to 14 400. The negative
result stands; it just rests on the rate contrast rather than on the capture.

Worth stating plainly, because it is the third instrument failure in one
investigation and the first two both produced numbers that agreed with the
hypothesis being tested: **a measurement that confirms what you expect is the one
that most needs a control.** The cold receiver said 9.2% against an expected 9.4%.
The fit said 11.2% against an expected margin of 11.0%. Both were noise from the
apparatus, and both would have been believed without a rate to compare against.

## Tools

In `testrig/tools/`. Credentials are read from `ata.md` by `sipcfg.py` (or `SIP_HOST` / `SIP_USER`
/ `SIP_PW` env vars) and are not embedded in any script.

| File | Purpose |
|---|---|
| `sipcfg.py` | Parses SIP host/user/password out of `ata.md` |
| `media.py` | Receive-driven RTP loop: one frame out per frame in, plus priming burst, watchdog and rate profiling |
| `sipmin.py` | Minimal SIP UAC: `REGISTER`/`INVITE`/`ACK`/`BYE`, MD5 digest, **CSeq-matched** response handling |
| `rtpcall.py` | Place an outbound call, stream RTP, dump audio, print a per-slice dominant-frequency timeline |
| `answer.py` | Register and answer an inbound call, sending silence |
| `ansam.py` | Register, answer, and emit a synthesised V.8 ANSam; includes a G.711 A-law **encoder** |
| `orch.py` | Orchestrates: arm a modem over SSH, then call it from `**620` |
| `orch_in.py` | Orchestrates: start the answerer, then make a modem dial `**620` |
| `listen.py` | Pi-side helper (also at `~/modemprobe/listen.py`): resets a modem, sets options, holds DTR high and logs everything it says |
| `vcap.py` | Pi-side: dial in voice mode and record what arrives at the analog port. Unlike `~/modemprobe/voicecap.py` it opens the DTE fast enough for the codec — 16-bit linear at 8 kHz is 16000 byte/s and a 115200 port carries 11520, so the stock version dropped 28% of its samples without saying so. It prints achieved against expected rate |

Two more live in `softmodem/`, because they need the modulator and the G.711 tables:
`playraw.py` answers a call and plays a real V.32bis data-phase signal at any rate,
and `chanfit.py` fits a capture against the reference it was played from. Read
`chanfit.py`'s docstring before trusting a number out of it — it has a floor and
it has a maximum block length, and both were found the hard way.

Usage:

```sh
cd testrig/tools
python3 orch.py    /dev/ttyUSB0 '**1' ATS0=1 ATX4   # **620 calls **1
python3 orch_in.py /dev/ttyUSB0 'ATDT**620'        # **1 calls **620, answered with ANSam
```

### Implementation notes worth keeping

1. **Match SIP responses by `CSeq`.** The FRITZ!Box retransmits `401 Unauthorized`. A naive read
   after the authenticated retry returns the *stale* challenge, so the call looks like it failed
   while the phone is audibly ringing. `sipmin.recv()` filters on CSeq number and method.
2. **In-dialog requests need authentication too.** `BYE` is challenged just like `INVITE`; an
   unauthenticated `BYE` gets `401` and leaves the call up.
3. **`ATDT`, not `ATD`, on `**1`** — see `modem.md` note 9.
4. **Hold DTR high for the whole call.** Both modems are `&D2`, so a dropped DTR aborts. The
   controlling process must keep the serial port open, which is why `listen.py` exists rather than
   just issuing `ATS0=1` and exiting.
5. **Never pace RTP off the local clock** — see the RTP pacing section above. Check the
   `watchdog=0` counter to confirm the 1:1 lock is real.
6. `audioop` is gone in Python 3.13, so `rtpcall.py` carries its own G.711 A-law/µ-law decode
   tables and `ansam.py` its own encoder. No third-party packages are needed anywhere.
