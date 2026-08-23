# Soft-modem step 1: V.8 negotiation over SIP — results

Code in `../softmodem/`. Companion to `modem.md` (hardware), `sip-audio-path.md` (transport) and
`../references/` (the specs). Everything below was measured against the rig.

## Result

**A software modem in `softmodem/` completes a full ITU-T V.8 negotiation with both hardware modems
over SIP, in both roles.**

As **call DCE** it detects the answer tone, waits Te, transmits CM on V.21(L), decodes the modem's JM
on V.21(H), sends CJ, and runs the 75 ms gap through to `DONE`.

As **answer DCE** it emits ANSam, decodes the modem's CM, replies with the JM intersection, and
waits for CJ. This role was originally blocked by the PBX (see below) and is unblocked by offsetting
the ANSam carrier 10 Hz:

| Role | Modem | CM / JM exchanged | Agreed |
|---|---|---|---|
| answer | `**2` Conexant | CM `C1 65 13 94 2A 0D 27` → JM `C1 05 10 90` | **V.21** |
| answer | `**1` Cirrus | CM `C1 45 13 94 2A 0D` → JM `C1 05 12 90` | **V.21, V.22bis/V.22** |

Both reached `DONE` on receiving CJ. The CM octets are the modems' own capability advertisements,
decoded by our V.21 demodulator: both offer V.34 duplex, V.32bis/V.32, V.22bis/V.22, V.23 duplex and
V.21, and the Conexant additionally sends a PCM-modem-availability octet (`27`) that the Cirrus
omits.

| Our CM advertises | Modem | JM octets received | Common modes agreed |
|---|---|---|---|
| V.21 | `**2` Conexant | `C1 05 10 90 0D` | **V.21** |
| V.21 | `**1` Cirrus | `C1 05 12 10 2A 0D` | V.22bis/V.22 |
| V.34, V.32bis, V.22bis, V.21 | `**2` Conexant | `C1 45 13 90 0D` | **V.34 duplex, V.32bis/V.32, V.22bis/V.22, V.21** |
| V.34, V.32bis, V.22bis, V.23, V.21 | `**1` Cirrus | `C1 45 13 94 2A 0D` | **V.34 duplex, V.32bis/V.32, V.22bis/V.22, V.23 duplex, V.21** |

Example run against `**2`:

```
[ 3.400] WAIT_ANS answer tone detected (2100 Hz, purity 0.99, -26 dBFS)
[ 4.400] TE       Te elapsed (1.00 s), starting CM on V.21(L)
[ 5.560] CM       JM x2: C1 05 10 90 0D  modes=['V.21'] cf=data (unspecified application)
[ 5.660] CJ       CJ sent (3 all-zero octets on V.21(L))
[ 5.740] GAP      75 ms gap done - V.8 negotiation complete
final state: DONE ; CM sequences sent: 6
```

The JM content is self-validating: a modem can only answer "V.21 in common" if it actually received
and parsed our CM. Our CM for V.21 encodes to `C1 05 10 90`, which is hand-checkable — `C1` is the
call-function octet (tag `1000`, b5..b7 = `011` = data), `05` is `modn0`, `10` and `90` are extension
octets, and `90` has b7 set, which Table 4/V.8 defines as V.21 availability.

**Both modems support V.21 for a data call**, so a V.21 data phase is a viable next step. One
oddity: when our CM advertised *only* V.21, the Cirrus counter-offered V.22bis/V.22 — a mode we had
not offered, which V.8 8.2.3 does not permit. Offered V.21 as part of a larger set, the same modem
confirms V.21 happily. The Conexant behaves correctly in both cases.

> **Direction taken.** Because the V.8 flag cannot cross this rig, the project now proceeds on the
> plain-ANS path instead, accepting the V.32bis Annex A ceiling of 14.4 kbit/s. See
> **`v32-ans-path.md`**. The V.8 work below stands as the characterisation of why.

## What the modulation on the answer tone actually signals

Worth stating plainly, because it is why a 15 Hz envelope was worth this much effort.

V.8 defines two different answer tones. §3.7 `ANS` is the plain V.25 answer tone. §3.8 `ANSam` is
"a sinewave signal at 2100 Hz, amplitude-modulated, as defined in 7.2" — §7.2 being 2100 ± 1 Hz,
amplitude-modulated by a 15 ± 0.1 Hz sinewave with the envelope between 0.8 and 1.2 times average.

**The modulation is the message.** It carries exactly one bit: *I support the V.8 CM/JM exchange.*
§8.2.1 notes that some Recommendations "require the transmission of unmodulated Answer tone (ANS)
and do not allow for CM/JM exchanges", and §8.2.2 that "if the answer DCE supports CM/JM exchanges,
ANSam shall be transmitted".

The calling modem's behaviour forks completely on it (§8.1.1):

| Detected | What the call DCE does |
|---|---|
| **ANSam** | silence for Te (≥ 0.5 s, ≥ 1 s if echo-canceller disabling is wanted), then send CM, await JM, send CJ |
| **ANS** | "proceed in accordance with Annex A/V.32 bis, ITU-T T.30, or other appropriate Recommendations" |

And §7.2 makes it a prohibition rather than a preference: **"A call DCE shall not transmit a signal
CM unless ANSam has been detected."** Without the modulation the negotiation cannot even be
attempted.

What is lost is the whole CM/JM menu: modulation-mode selection, the call function (data,
fax-transmit, fax-receive, V.18 textphone, H.324, videotext), the protocol category (LAPM without
the ODP/ADP exchange), the PSTN-access category, and PCM-modem availability.

Concretely it costs every high-speed mode. V.34 §11.1.1.1 mandates the V.8 procedure, and §11.1.1.3
is explicit: "If signal ANS (rather than ANSam) is detected, the modem shall proceed in accordance
with Annex A/V.32 bis, Recommendation T.30, or other appropriate Recommendations." No ANSam means no
V.34, and therefore no V.90 or V.92 either, since those build on V.34's start-up. The fallback is
V.32bis Annex A, ceiling 14.4 kbit/s — from a modem pair that would otherwise reach 33.6 k.

**The phase reversals are a separate, orthogonal bit on the same carrier**, which is easy to conflate
given how much both were manipulated here. Reversals mean *disable network echo cancellers* (V.25
§2.3 and §4.3, G.168 §7.1), and V.8 §7.2 says "when network echo canceller disabling is not
required, phase reversals shall not be imparted to the ANSam signal". V.34 §11.1.2.1 then gives them
a second meaning: "If duplex operation is intended, this signal shall include phase reversals... If
half-duplex operation is intended, phase reversals are optional."

So one 2100 Hz carrier conveys up to three independent things:

| Property | Signals |
|---|---|
| presence of the tone | a modem answered |
| 15 Hz amplitude modulation | the answerer speaks V.8 (CM/JM available) |
| 180° phase reversals | disable network echo cancellers; in V.34, duplex intent |

This rig strips the second and third and leaves only the first. Both modems then did exactly what
§8.1.1 prescribes — took the ANS branch into V.32bis Annex A and emitted the sustained 1800 Hz
signal `AA` measured at purity 0.999. Their behaviour was correct throughout; the network destroyed
the flag. Two details corroborate the reading: the measured tone durations of ~4.8 s and 5.0 s match
§8.2.2's "5 ± 1 s", and because the reversals are stripped too, this rig also loses echo-canceller
disabling and V.34's duplex indication.

## The finding that shaped the design: a 2100 Hz answer-tone regenerator in the path

The original plan was for the soft-modem to be the **answer** DCE — we send ANSam, the modem sends
CM. That does not work on this rig. Finding out why took two attempts, and the first answer was
wrong; both are recorded here because the mistake is instructive.

V.8 7.2 defines ANSam as 2100 Hz amplitude-modulated by a 15 Hz sinewave, envelope ranging 0.8 to
1.2 times average. A call DCE that hears plain **ANS** instead goes to V.32bis Annex A (V.8 8.1.1),
which is exactly the sustained 1800 Hz **V.32 signal `AA`** measured in `sip-audio-path.md`. So the
question was: what happens to our AM?

### What the evidence actually shows

Putting a modem into voice mode (`+FCLASS=8`, `+VRX`) makes it record what arrives at its analog
port — the only window onto the far side of the FXS port. Sending an AM tone and varying **only the
carrier frequency**, all at −24 dBFS:

| Sent | Measured output | AM depth received | AM rate | Coherence |
|---|---|---|---|---|
| 1500 Hz | 1500.0 Hz | **20.00 %** | 15.00 Hz | 0.997 |
| 2090 Hz | 2089.9 Hz | **19.97 %** | 15.00 Hz | 0.955 |
| **2095 Hz** | **2099.9 Hz** | **0.03 %** | — | 0.000 |
| **2098 Hz** | **2099.9 Hz** | **0.06 %** | — | 0.000 |
| **2100 Hz** | 2099.9 Hz | **0.05 %** | — | 0.000 |
| 2102 Hz | 2102.0 Hz | **19.94 %** | 15.00 Hz | 0.958 |
| 2103 Hz | 2102.9 Hz | **19.99 %** | 15.00 Hz | 0.962 |
| 2105 Hz | 2104.9 Hz | **19.94 %** | 15.00 Hz | 0.962 |
| 2110 Hz | 2109.9 Hz | **19.86 %** | 15.00 Hz | 0.966 |
| 2600 Hz | 2599.9 Hz | **20.92 %** | 15.00 Hz | 0.955 |

Three things follow, and the third is decisive:

1. **The path is transparent to 20 % AM.** At the same level, a 15 Hz AM tone on a 1500, 2090, 2102,
   2105, 2110 or 2600 Hz carrier arrives with its full depth and near-perfect coherence. So there is
   no general compressor, limiter or AGC acting on the envelope.
2. **The effect is confined to a narrow band around 2100 Hz**, roughly 2093–2101 Hz. Moving the
   carrier **3 Hz** up, to 2103 Hz, is enough for the AM to survive completely.
3. **A 2095 Hz input emerges at 2099.9 Hz.** A filter or a compressor cannot shift a tone's
   frequency. Something is *detecting* an answer tone and *regenerating* it from its own oscillator.

So the mechanism is a **narrowband 2100 Hz answer-tone detector that replaces the tone with a
locally generated, unmodulated 2100 Hz sine**. That is a standard thing for a VoIP gateway to do
with ANS, and it converts ANSam into ANS in transit. Which is precisely why both modems answer with
V.32 `AA`: they are behaving correctly, and by the time our ANSam reaches them it genuinely is
plain ANS.

### Isolating the box: capture on both sides, in both directions

The measurement above passes through the modem's voice receive path, so on its own it brackets the
box *and* the modem front end. Two further captures separate them.

**Same call, both sides.** Saving the outbound stream exactly as transmitted alongside the modem's
analog-port recording confirms our own transmitter is not the problem:

| Capture point | Carrier | AM depth | Coherence | Level |
|---|---|---|---|---|
| A: our TX, as actually sent | 2100 Hz | **19.96 %** | 0.961 | −23.9 dBFS |
| B: modem analog port | 2100 Hz | **0.05 %** | 0.000 | −22.2 dBFS |

**Reverse direction, modem as the source.** Driving the modem's `+VTX` with a file we generate makes
it transmit a known AM tone, so its *receive* path is out of the loop entirely. Path: modem voice
DAC → analog → FXS → FRITZ!Box → RTP → us.

| Sent by the modem | Received at our RTP | AM depth | Coherence |
|---|---|---|---|
| 1500 Hz (control) | 1500.35 Hz | **19.66 %** | 0.984 |
| 2100 Hz | 2099.80 Hz | **0.85 %** | 0.019 |
| **2103 Hz** | **2100.00 Hz** | **0.72 %** | 0.016 |
| 2110 Hz | 2110.05 Hz | **19.71 %** | 0.943 |
| 2600 Hz | 2600.10 Hz | **19.87 %** | 0.943 |

This isolates the box:

- The 1500 Hz control carries 20 % AM intact in **both** directions, so neither path is generally
  hostile to envelope modulation, and neither the modem's DAC nor its ADC flattens AM as such.
- The 2100 Hz effect appears in **both** directions. The modem's receive path is involved only in the
  forward test; its transmit path only in the reverse test. They are different circuits. The only
  element common to both paths is the FRITZ!Box.
- **The carrier snaps in both directions**: 2095 → 2099.95 Hz and 2098 → 2099.95 Hz going towards the
  modem, and 2103 → 2100.00 Hz coming back. Nothing passive moves a tone's frequency, and a tracking
  filter would lock to the *input* frequency, not to 2100. A fixed local oscillator does.
- The regenerated tone is *purer* than what we sent — mean per-block purity 0.996 versus 0.980 for
  our own 20 %-AM signal — which is exactly what removing the sidebands gives.

So the conversion of ANSam to ANS is the box's doing, and it is regeneration rather than filtering.
The capture band is narrow and slightly different per direction: towards the modem it takes 2095 and
2098 but not 2090 or 2102; coming back it takes 2103 but not 2110. Call it roughly 2100 ± 5 Hz.

### Listen to it: `regenerator_demo.wav`

`regenerator_demo.wav` (8 kHz, 16-bit, stereo, 13.5 s) puts both capture points side by side.
**Left is what we transmitted over RTP; right is the same call recorded at the modem's analog port.**
Three sections, each a matched TX/RX pair from a single call:

| Section | Starts | Left (sent) | Right (received) |
|---|---|---|---|
| 1500 Hz control | 0.00 s | 1500.0 Hz, AM 20.15 % | 1499.9 Hz, AM **20.00 %** |
| 2100 Hz | 4.50 s | 2100.0 Hz, AM 19.97 % | 2099.9 Hz, AM **0.03 %** |
| 2095 Hz → snap | 9.00 s | **2095.0 Hz**, AM 19.91 % | **2100.0 Hz**, AM **0.08 %** |

The control section sounds and measures the same on both channels. In the second the right channel
loses its 15 Hz warble. In the third the right channel loses the warble *and* sits 5 Hz above the
left, so downmixing the two channels there produces an audible 5 Hz beat — the frequency snap made
audible. A spectrogram at ~1 Hz resolution shows the same thing visually.

Two things about the file, since it is evidence rather than illustration. Alignment is at the start of
each detected tone segment, not sample-exact: the modem starts recording part-way into our tone, and
in the affected sections the received tone comes from a different oscillator, so there is no phase
relationship to align to. And each channel is normalised per section to the same RMS, because the two
capture points have unrelated absolute gains — channel amplitude carries no meaning here, only the
envelope and the frequency do. Regenerate it with `softmodem/make_wav.py`.

### VBD does not help: the box does not implement V.152

The obvious remedy is voice-band data signalling — ITU-T V.152, carried in SDP as the RFC 3108
`gpmd` attribute — which asks a gateway to treat the stream as data and switch off echo
cancellation, silence suppression and voice-optimised processing. It does not work here.

The FRITZ!Box's own offer contains no VBD at all, only codecs:

```
m=audio 7088 RTP/AVP 8 0 2 102 100 99 97 101
a=rtpmap:2 G726-32/8000     a=rtpmap:100 G726-40/8000   a=rtpmap:97 iLBC/8000
a=rtpmap:102 G726-32/8000   a=rtpmap:99 G726-24/8000    a=rtpmap:101 telephone-event/8000
```

Adding `a=gpmd:<pt> vbd=yes;ecan=off` and `a=silenceSupp:off - - - -` to our SDP — as the answer,
and then properly as the *offer* by originating — changes nothing. The box never echoes `gpmd` back,
so it is simply ignoring the attribute. Interleaved A/B at −24 dBFS, measured at the modem's analog
port:

| Run | box echoed `gpmd` | AM depth |
|---|---|---|
| baseline, PCMA | no | 0.68 % |
| **VBD**, PCMA | no | 0.61 % |
| baseline, PCMA | no | 0.58 % |
| **VBD**, PCMA | no | 0.62 % |
| **VBD**, PCMU | no | 0.71 % |
| baseline, PCMU | no | 1.36 % |
| **VBD**, 1500 Hz control | no | **18.61 %** |

VBD and non-VBD are indistinguishable, and forcing PCMU instead of PCMA makes no difference either.
The 1500 Hz control confirms the measurement chain was working throughout.

### Why a gateway does this

Not AVM's stated intent — that is not observable from here — but the standards make the typical
reasons clear, and they explain why this behaviour looks *correct* from a gateway designer's point of
view.

**2100 Hz is not primarily a modem signal; it is a network control signal.** V.25 is titled
"...including procedures for **disabling of echo control devices**", and §4.3 says that where network
echo cancellers are to be disabled as well as echo suppressors, "phase reversals shall be
introduced". V.25 §11 then refers the implementer to G.164 and G.165 for detector behaviour. So every
device in the path that does echo control is *expected* to run a narrowband 2100 Hz detector.

G.168 §7 spells the detector out. An echo canceller "should be equipped with a tone detector" that
disables it on 2100 Hz **with** phase reversals. And §7.2 adds the case that matters here:

> To improve the operation of the echo canceller for fax signals and low-speed voiceband data, it may
> be beneficial for some echo cancellers to disable the NLP for such calls. In this case, the echo
> canceller may optionally detect any 2100 Hz tone without phase reversals.

So a 2100 Hz tone is a documented trigger for switching parts of the voice-processing chain off. Once
a device has that detector, several standard practices lead to regeneration:

- **Tone relay across codecs.** Gateways routinely detect telephony tones and re-emit them locally
  rather than pass the waveform through a codec that would distort it. This box offers iLBC and
  G.726-24/32/40, all of which mangle a steady tone. A generic tone-relay stage produces a clean
  tone by construction.
- **Packet-loss immunity.** A regenerated tone survives lost or late RTP frames; a relayed waveform
  does not.
- **Reuse of the existing tone generator.** An FXS port already synthesises dial tone, ringback and
  busy. Feeding a detected answer tone through the same generator is a small implementation step.
- **Fax/modem mode switching.** Answer-tone detection is the usual trigger for switching to
  passthrough, VBD or T.38, and holding a clean tone bridges the detection latency.

**The deeper reason is a change in what the tone means.** For V.25, ANS is an *event*: its only job
is to be detected, and its waveform detail carries nothing. V.8 later overloaded the same tone with
15 Hz AM so that it also carries one bit of information — "I speak V.8". A gateway written to the
older model treats the tone as an event, regenerates it cleanly, and silently discards the newer
information layer. Cleaning up ANS is harmless; doing the same to ANSam destroys a negotiation.

One measurement is worth setting against the spec. G.168 §7.2 says that "in the frequency band
2079 Hz to 2121 Hz detection must be possible whilst in the band 1900 Hz to 2350 Hz detection may be
possible". The box's regeneration band is only about 2100 ± 5 Hz — considerably **narrower than the
detection band a conformant tone detector is required to cover**. That is precisely the gap the
2090/2110 Hz workaround exploits, and it is also why the workaround is fragile: a device implementing
the full mandatory band would catch 2090 and 2110 Hz as well.

Finally, note the regeneration happens even when the negotiated codec is G.711, which needs no tone
relay at all, and it is unaffected by which of PCMA or PCMU is chosen. So it is not an adaptive
optimisation for a lossy codec; it looks like a fixed stage in the FXS audio path.

### G.722 is refused: the box will not use a wideband codec here

A wideband codec would have to bypass a narrowband tone path, so it is a good test of whether the
regeneration is codec-related. It cannot be run on this rig: the FRITZ!Box refuses G.722.

Offers interleaved with a PCMA control, so a seized line cannot be mistaken for a codec rejection:

| Our offer | Result |
|---|---|
| `8` (PCMA) | `200 OK`, answer PCMA |
| `9` (G.722 alone) | **`488`, `Warning: 399 "no fitting codec"`** |
| `8` (PCMA) | `486` — line still seized from the failed call, a transient artefact |
| `9 8` (G.722 then PCMA) | `200 OK`, answer **PCMA** — G.722 silently dropped |
| `8` (PCMA) | `200 OK`, answer PCMA |

The offer itself is well formed: G.722 is static payload type 9, and per RFC 3551 its rtpmap clock
rate is declared as 8000 even though the codec samples at 16 kHz, so `a=rtpmap:9 G722/8000` is
correct. The box's own inbound offers never include payload type 9 either.

The likely reason is the destination. Both `**1` and `**2` terminate on analogue FXS ports, which are
inherently 300–3400 Hz, so a wideband codec has nothing to deliver there; AVM supports G.722 for
HD calls between IP and DECT endpoints, not towards an a/b port. Note the first `486` above is a
reminder that a failed INVITE can leave the analogue line seized, which is easy to misread as a
codec problem — hence the interleaved control.

### G.726-32 makes no difference either

The box does offer G.726-32 (payload types 2 and 102), so that codec *is* reachable — it just needs
an ADPCM implementation, since the probe tone has to be correctly encoded for the analogue side to
carry it. `g726.py` implements ITU-T G.726 32 kbit/s from the Recommendation.

Negotiating payload type 2 and sending the same AM probe, measured at the modem's analog port:

| Carrier | Our TX (G.726, decoded back) | At modem's analog port |
|---|---|---|
| 2100 Hz | 2100 Hz, AM **19.95 %** | 2100 Hz, AM **0.03–0.06 %** (3 runs) |
| 1500 Hz (control) | 1500 Hz, AM **19.95 %** | 1509 Hz, AM **19.81 %** |

The control is what makes this conclusive: over the very same G.726 path a 1500 Hz AM tone arrives
with its modulation intact, so neither the codec nor our encoder is destroying it. Only 2100 Hz is
affected.

So the regeneration is **codec-independent** — identical under G.711 A-law, G.711 mu-law and
G.726-32 ADPCM. Together with its appearing in both directions, and happening even under G.711 which
needs no tone relay at all, that points to a fixed stage in the FXS audio path rather than anything
in the packet-side codec chain.

A note on the codec implementation, since the measurements lean on it. It is validated against
ffmpeg, an independent implementation, and it is **not bit-exact**: it agrees for the first ~60
samples then drifts by small rounding amounts in the quantizer scale factor. Getting that far took
four fixes, each found by reading the Recommendation rather than by guesswork — the reset value of
`yl` (Table 6), `PKS2 = PK0 ⊕ PK2` in UPA2 (not PK1 ⊕ PK2), the `F(I) << 9` scaling FILTA expects,
and the 12-bit wraps in SUBTB and ADDA. What it does achieve is spec-level fidelity: self round-trip
SNR 25.1 dB against ffmpeg's own 24.9 dB, ffmpeg's stream decoded by us at 22.9 dB, and — the
property the experiment actually needs — a probe tone through our encoder and ffmpeg's decoder at
34–35 dB with the carrier exact and AM preserved to 19.95 %. `test_g726.py` asserts that last
property rather than bit-exactness, and says so.

### It is not a start-of-call effect: alternating noise and tone for a minute

One reasonable hope is that the detector arms only at the start of a call, so a 2100 Hz tone
appearing later — or reappearing after an interruption — might pass. It does not.

The probe alternates 5 s of band-limited noise with 5 s of 20 %-modulated 2100 Hz, for a full minute
inside one call, recorded at the modem's analog port. The noise gives the detector something to
un-latch on between tone slabs. Capture integrity was 100.1 % of the expected sample count, so no
serial bytes were dropped and the timing is trustworthy.

The recording shows a perfect alternation — `.....TTTTT.....TTTTT.....` — and every tone slab is
mangled:

| Slab | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| Time in call | 4.5–9.5 s | 14.5–19.5 s | 24.5–29.5 s | 34.5–39.5 s | 44.5–49.5 s | 54.5–59.5 s |
| AM depth received | 0.05 % | 0.06 % | 0.04 % | 0.05 % | 0.05 % | 0.05 % |

Six independent onsets across a minute, all destroyed — including the last, 55 to 60 seconds into
the call. **The detector re-acquires on every tone onset**, and five seconds of intervening noise
does not shake it off.

The same test at 1500 Hz is the control, and over the same alternating structure the modulation comes
through untouched every time: 20.01, 19.94, 20.02, 20.12, 20.13, 20.01, 20.03 % across seven slabs,
coherence 0.996–1.001.

Two honest notes on the control. The classifier sometimes splits one 5 s slab into two when purity
momentarily dips, which is why seven slabs appear rather than six. And the modem's recording begins
at a variable offset relative to our stream — dial and `+VRX` timing varies by tens of seconds
between runs — so early slabs sometimes fall outside the capture window. In one earlier control run
the offset was large enough that the first 25 s contained only noise slabs; re-running it recovered
the expected pattern. Neither affects the measured depths.

**So the caveat to carry forward is broader than "2100 Hz tones at the start may get mangled":** on
this rig, a 2100 Hz tone is liable to be regenerated *whenever* it appears in a call, not only during
call setup. Anything that needs to put information into a ~2100 Hz tone at any point in a call has to
assume it will be stripped.

### The echo-canceller disabling tone does not survive either

There is such a tone, and it is the obvious thing to try first. V.25 2.3 defines it: 180 degree
reversals in the phase of the answer tone at intervals of 425 to 475 ms, the phase reaching
180 ± 10 degrees within 1 ms, and the amplitude not dropping more than 3 dB below steady state for
more than 400 us. G.168 7.1 makes it the *only* signal a conformant tone disabler should act on —
it "should disable the echo canceller only upon detection of a signal which consists of a 2100 Hz
tone with periodic phase reversals inserted in that tone, and not disable with any other in-band
signal, e.g. ... a 2100 Hz tone without phase reversals" — and a disabled canceller "no longer
modif[ies] the signals which pass through it in either direction". If the regeneration lived in
that block, disabling it first ought to open the path.

It does not. Sending 4 s of spec-conformant disabling tone (an instantaneous sign flip satisfies all
three of V.25's constraints) before the ANSam changes nothing:

| Window of the recording | Carrier | AM depth |
|---|---|---|
| 0–2 s | 2100 Hz | 0.04 % |
| 2–4 s | 2100 Hz | 0.04 % |
| 4–6 s | 2100 Hz | 0.05 % |
| 6–8 s | 2100 Hz | 0.04 % |

and end-to-end the modem still never sends CM.

**The reason is worth stating plainly: the disabling tone cannot get through either.** Counting phase
reversals arriving at the modem's analog port, transmitting one every 450 ms:

| Sent | Carrier received | Reversals received | Interval |
|---|---|---|---|
| 2100 Hz + reversals | 2100 Hz | **0** | — |
| 2110 Hz + reversals | 2111 Hz | **12** | 450 ms |

At 2100 Hz not one reversal survives; 10 Hz away they all do, at exactly the transmitted interval.
The regenerator replaces the tone with a clean sine carrying neither amplitude modulation nor phase
reversals, so the disabling signal is destroyed by the very stage one would want it to disable.

Two consequences follow. The narrow one: a disabling prefix cannot help the V.8 problem, because it
is subject to the same regeneration as the ANSam it precedes. The broader one, which matters beyond
V.8: **on this rig the standard V.25 / G.168 echo-canceller disabling mechanism does not work at
all.** Any echo control downstream of the box will never see the reversals and will stay enabled,
whatever a modem transmits.

### Deeper modulation defeats the regenerator, but not usefully

If the detector needs the signal to look like a steady tone, modulating harder should stop it
latching. It does. Measured at the modem's analog port, 2100 Hz carrier, level fixed at −24 dBFS:

| Depth sent | 20 % | 22 % | 26 % | 30 % | **36 %** | **40 %** | **60 %** | **80 %** | **100 %** |
|---|---|---|---|---|---|---|---|---|---|
| Depth received | 1.55 % | 0.07 % | 0.05 % | 0.03 % | **36.20 %** | **39.85 %** | **59.61 %** | **80.12 %** | **100.18 %** |

The threshold sits between 30 % and 36 %. Below it the tone is replaced as usual; at or above it the
modulation passes through essentially untouched. A 1500 Hz control at 100 % depth also passes
(99.33 %), so deep modulation is not itself a problem for the path.

**This is strong independent evidence for the mechanism.** A compressor, limiter or AGC would flatten
*deeper* modulation at least as hard as shallow modulation — that is what dynamic range control does.
The observed behaviour is the exact opposite: the deeper the modulation, the more faithfully it
survives. That only makes sense if something is *detecting* a tone and substituting for it, and
failing to latch once the signal stops resembling a steady tone.

**But it is not a usable workaround.** The modem never sends CM at any depth the box passes:

| ANSam depth | 0.32 | 0.34 | 0.36 | 0.40 |
|---|---|---|---|---|
| Modem's response | no CM | no CM | no CM | no CM |

V.8 7.2 specifies an envelope of 0.8 to 1.2 times average, i.e. 20 % — so 32 % and up is out of spec
and the modem's ANSam detector correctly declines it. The two thresholds do not overlap: the box
needs roughly ≥ 33 % to let the modulation through, and the modem needs something below 32 % to
recognise ANSam at all. There is no depth that satisfies both.

So depth characterises the detector on a second axis but does not get a negotiation. Offsetting the
carrier remains the workaround that works, because it leaves the modulation at the specified 20 %
and only moves the frequency, which the modem's detector tolerates and the box's does not.

### What does work: offset the ANSam carrier by 10 Hz

The regenerator's capture band is only about 2100 ± 5 Hz, whereas a V.25 answer-tone *detector* is
normally specified to accept 2100 ± 15 Hz. That leaves a gap to exploit: a carrier at 2090 or
2110 Hz slips past the regenerator with its AM intact, and is still close enough for the far end to
accept as an answer tone.

Tested with the modem calling us, watching what it replies with:

| ANSam carrier | V.21(L) 980 Hz | V.32 AA 1800 Hz | Modem's response |
|---|---|---|---|
| 2100 Hz | 0.000 | peak 0.999, 108–164 blocks | V.32 `AA` — ANSam seen as ANS |
| **2110 Hz** | **peak 0.217, 22 blocks** | 0.000 | **CM — ANSam accepted** |
| **2090 Hz** | **peak 0.242, 11 blocks** | 0.001 | **CM — ANSam accepted** |

This is also the strongest evidence for the whole diagnosis. A **10 Hz shift in carrier frequency**,
with nothing else changed, flips the far end from "this is plain ANS, fall back to V.32bis Annex A"
to "this is ANSam, here is my CM". Any explanation other than a narrowband 2100 Hz process in the
path would have to account for that.

Note this deliberately violates V.8 7.2, which specifies 2100 ± 1 Hz for ANSam. It is a workaround
for a specific broken path, not conformant behaviour, and `fsm.Answer` says so at the point where
the carrier is chosen. A conformant modem on a conformant network should use 2100 Hz.

The remaining sensible fix is on the box: a FRITZ!Box lets each analogue port be declared as a
telephone, fax machine, answering machine or PBX, and the non-telephone settings change its audio
handling. That is a change to the user's router configuration, so it has not been touched here.

### The wrong answer, and why it was wrong

The first conclusion recorded here was that the FRITZ!Box applies a **level-dependent compressor**.
That was wrong, and it was wrong for two avoidable reasons.

**Sample size of one.** The claim rested on two captures: AM destroyed at −12 dBFS, AM intact at
−30 dBFS. Repeating properly:

| Sent level | −12 | −18 | −24 | −30 | −36 |
|---|---|---|---|---|---|
| First sweep | intact | destroyed | destroyed | destroyed | intact |
| Repeats | destroyed ×3 | — | destroyed ×5 | — | destroyed ×1, intact ×2 |

The AM is destroyed at every level, *including* the two levels that originally looked clean; those
were the occasional runs where the detector fails to latch. Non-monotonic results across levels
should have been the tell that level was the wrong variable, and no repeat measurements had been
taken at all.

**Over-long coherent integration.** The original "27 dB of AM suppression" figure came from a 4 s
Goertzel on the sidebands. A Goertzel over N samples has a bin about sr/N wide, so 4 s of
integration is ~0.25 Hz wide and washes out anything not perfectly coherent. The same file measured
in 0.5 s windows gives −27.5 dB rather than −46.8 dB. This is the *same* mistake that had already
made a real 1800 Hz tone read as absent earlier in `sip-audio-path.md`, and it recurred because the
measurement code was written afresh instead of using a checked estimator.

The fix was to build `amdepth.py`, which estimates the coherent 15 Hz component of the envelope and
is **calibrated against synthetic signals of known depth** before being trusted: it recovers depths
of 0.01–0.20 to within 0.0006 absolute across levels from −12 to −36 dBFS, still reports 0.2 % AM
correctly, and returns ~6 % (not 0) for a 20 % AM whose envelope phase has been scrambled. So a
reading of 0.03–0.06 % means the AM is genuinely absent rather than merely decohered. Splicing was
ruled out separately: carrier phase discontinuities, which a spliced recording would show, number
0–2 per capture and are absent altogether from the analog-port captures.

A related bug found while validating: `dsp.dominant` scanned in coarse steps while integrating over
the whole record, so its coarse pass stepped over the peak and locked onto noise — it reported
2490 Hz for a 2098 Hz tone. It now limits the coarse window so the bin is a fraction of the scan
step. The AM depth figures were unaffected, because the envelope estimator tolerates a few Hz of
carrier error.

### Consequences

- **ANSam at 2100 Hz cannot cross this path as ANSam**, so at the specified carrier the answer role
  cannot elicit CM from either modem, at any level. Offsetting the carrier to 2090 or 2110 Hz fixes
  it; the call-DCE role avoids the problem entirely, since it depends only on the hardware sending
  an answer tone and parsing our CM, and CM is a robust V.21 FSK signal rather than a fragile
  envelope property.
- These were also tried and all failed to elicit CM: levels −12/−24/−30 dBFS; reversals off, abrupt
  at 450 ms, raised-cosine at 450 ms; AM depth 10/20/30 %; ANSam starting 0.25 s and 1.5 s after
  answer; both modems.
- **Anything that must carry a 2100 Hz tone across this rig faithfully will be defeated**, including
  V.8 ANSam and V.25 echo-canceller-disabling tones — unless it moves off 2100 Hz.

A loose end: the Conexant advertises `+A8E` support for `<v8o>=6` and `<v8a>=5`, which per V.251 6.x
should make it emit `+A8A` / `+A8M` / `+A8J` indications naming the V.8 signals it sees. It accepts
the setting but **never emits any indication**, in either role. The Cirrus does not implement `+A8E`
at all (`ERROR`). So DTE-side V.8 diagnostics are unavailable, which is why the JM decode is the
ground truth here.

## What was built

| Module | Role |
|---|---|
| `g711.py` | A-law / mu-law codecs. No SIP imports, so DSP stays unit-testable. `audioop` is gone in Python 3.13. |
| `dsp.py` | Goertzel (normalised to mean power), periodogram, sliding complex demod, and an ANSam analyser measuring centre frequency, AM rate and depth, phase-reversal interval and in/out-of-band ratio against V.8 7.2. |
| `v21.py` | V.21 modem. `V21Mod` is continuous-phase FSK with a fractional bit-clock accumulator (8000/300 = 26.667 samples/bit). `V21Demod` correlates mark and space, runs a transition-nudged timing loop, slices mid-bit, and gates on an adaptive squelch. |
| `v8.py` | V.8 coding: category/extension octets, CM/JM construction, sequence framing, preamble search, octet parsing, the 8.2.3 JM intersection rule, CJ. |
| `ansam.py` | Spec-tight ANSam generator with switchable reversal shape/interval, AM depth and level. |
| `fsm.py` | `Originate`: `WAIT_ANS → TE → CM → CJ → GAP → DONE`. `Answer`: `SILENCE → ANSAM → JM → CJWAIT → GAP → DONE`. Pure samples in, samples out. |
| `rtp.py` | Receive-driven 1:1 RTP pump with a streaming `on_frame(inbound) -> outbound` callback. |
| `sip_glue.py` | Reuses `testrig/tools/sipmin.py` rather than re-deriving the CSeq-matched digest handling. |
| `run_originate.py`, `run_answer.py` | Runners for the two roles. `orch_orig.py`, `orch_m2.py`, `orch_voicecap.py` drive the modem over SSH alongside. |
| `amdepth.py` | Calibrated AM-depth estimator; `calibrate()` checks it against known depths before use. |
| `probes.py` | Test signals: AM on a swept carrier, two-tone (IMD), step-level. |
| `sweep_am.py`, `probe_at_port.py` | Level and carrier-frequency sweeps measured at a modem's analog port. |
| `dualcap.py` | Captures our TX as actually sent *and* the modem's analog port, in one call. |
| `origprobe.py` | Originates with our own SDP offer (so VBD can really be negotiated) to a modem answering in voice mode. |
| `vbd_ab.py` | Interleaved A/B of VBD vs baseline, with a 1500 Hz control. |
| `codectest.py` | Offers a codec list with the modem freshly armed, and reports what the box answers. |
| `g726.py` | ITU-T G.726 32 kbit/s ADPCM encoder/decoder, plus a stateful streaming `Encoder`. |
| `ansam.ec_disable_samples()` | The V.25 2.3 echo-canceller disabling tone (2100 Hz, 450 ms phase reversals, no AM). |
| `alt_test.py` | Alternates noise and an AM tone for a minute in one call and measures each tone slab in order. |
| `test_g726.py` | Validates the codec against ffmpeg. |
| `make_wav.py` | Builds `../testrig/regenerator_demo.wav` from the matched dual captures. |
| `revcap.py` | Reverse direction: drives the modem's `+VTX` with a known tone and measures at our RTP side. |
| `test_offline.py` | 33 checks, no hardware needed. |

Two frequency facts, taken from the specs rather than memory. V.21 3 says channel 1 has mean
1080 Hz, channel 2 has mean 1750 Hz, deviation ±100 Hz, and "the higher characteristic frequency
(FA) corresponds to a binary 0" — so V.21(L) is mark 980 / space 1180 and V.21(H) is mark 1650 /
space 1850. V.8 3.1–3.6 assigns CI/CM/CJ to V.21(L) and JM to V.21(H), so as call DCE we transmit
on L and receive on H.

## Verification

```sh
cd softmodem
python3 test_offline.py                                  # 33 checks, no hardware
python3 -m dsp --analyse-ansam ref/ansam_m2_conexant.raw  # a real modem's answer tone
python3 orch_orig.py /dev/ttyACM0 '**2' ATX4 ATS0=1 --sip --modes V.21 --level -30
python3 orch_orig.py /dev/ttyUSB0 '**1' ATX4 ATS0=1 --sip \
        --modes 'V.34 duplex,V.32bis/V.32,V.22bis/V.22,V.23 duplex,V.21' --level -30
```

`test_offline.py` covers G.711 SNR and silence encoding; ANSam against V.8 7.2 including the −20 dB
sideband check before and after A-law; V.21 bit round-trip on both channels at −12/−24/−36 dBFS and
at 20 dB and 12 dB SNR with a DC offset; V.8 octet field layout; the guarantee that an encoded
sequence never simulates an HDLC flag; end-to-end CM through modulator, demodulator and parser; the
JM intersection rule including the all-zeros no-common-mode case; and CJ detection.

Reference captures are in `softmodem/ref/`: both modems' answer tones, the analog-port recording
that shows the AM loss, the carrier-sweep probes (`p_am*.raw`), the matched dual captures
(`dual_tx_*` / `dual_rx_*`) behind `regenerator_demo.wav`, and the inbound audio from each
negotiation.

## What is not established

- **No data phase.** V.8 selects a modulation; it does not carry data. Nothing here approaches the
  `rules.md` bar of a minute of data-phase traffic both ways. The next step is a V.21 data phase,
  which is within reach because `v21.py` already does 300 bit/s both ways — but V.21 at 300 bit/s
  is also the slowest possible answer to that requirement.
- **The 2090/2110 Hz workaround is not conformant** and has only been shown to work with these two
  modems. Another modem's ANS detector might be narrower, and nothing here establishes how far the
  carrier can be moved before the far end stops accepting it — only that ±10 Hz works.
- **`--level -30` is empirical.** It was originally chosen on a mistaken belief about level; it
  works, but nothing here shows it is optimal for V.21 FSK. The level at which V.21 detection
  becomes marginal in either direction has not been measured.
- **The depth threshold is bracketed, not pinned.** It lies between 30 % and 36 %; 32 % and 34 %
  were only tested end-to-end (no CM), not measured at the analog port, so the exact knee is unknown.
- **The regenerator's behaviour is only partly mapped.** The capture band is roughly 2100 ± 5 Hz,
  measured at six points towards the modem and five coming back. Its hold/release timing, whether
  the band edges move with level, and whether it also acts on the 2225 Hz Bell answer tone are all
  unmeasured. The regenerated tone measures 2099.80–2100.00 Hz, which is within the resolution of
  these measurements of exactly 2100 Hz.
- The both-directions argument isolates the box by elimination — the modem's transmit and receive
  paths are different circuits and only the box is common to both. It is not a direct probe of the
  box's internals, which would need a physical tap on the FXS pair.
- **We do not verify ANSam before sending CM.** `fsm.py` treats any sustained 2100 Hz as grounds to
  proceed, deliberately: on this path an AM test is not a reliable gate. If the far end had been
  sending plain ANS and not expecting CM, our CM would simply be ignored.
- **The Cirrus's V.21-only counter-offer of V.22bis/V.22 is unexplained.**
- The `2250 Hz` signal from `sip-audio-path.md` is still unidentified; the V.8 work did not touch it.
