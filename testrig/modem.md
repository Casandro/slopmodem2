# Test Rig — Hardware Modem Inventory

Host: `raspberrypi` (`Linux 6.6.51+rpt-rpi-v8 aarch64`), reachable over SSH as user `casandro`
(member of `dialout`, so no `sudo` needed for the serial ports).

Two modems are attached over USB. Both were enumerated live via AT commands; every value below is
either a direct device response or explicitly marked as an inference.

| | Modem 1 | Modem 2 |
|---|---|---|
| **Dial number** | **`**2`** | **`**1`** |
| Device node | `/dev/ttyACM0` | `/dev/ttyUSB0` |
| Stable path | `/dev/serial/by-id/usb-Conexant_USB_Modem_12345678-if00` | `/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller-if00-port0` |
| USB ID | `0572:1340` Conexant CX93010 ACF Modem | `067b:2303` Prolific PL2303 (USB↔RS-232 bridge) |
| Chipset | Conexant CX93001 | Cirrus Logic CL-MD56xx |
| Firmware | `CX93001-EIS_V0.2013-V92` | `CD08.55 - 643 SERIAL (V.90-ONLY) SPEAKERPHONE 01` |
| Attachment | Native USB modem (CDC-ACM) | External serial modem behind a USB-serial bridge |
| Top standard | **V.92** | **V.90** (client side only) |
| Compression | V.42bis **and V.44** | V.42bis / MNP5 only |
| Command set | ITU `+` command set (`+MS`, `+DS`, `+ES`, `+DS44`) | Legacy Rockwell/Cirrus (`%C`, `\N`, `"H`, `"O`) |
| Voice mode | yes — verified, 7 codecs @ 8 kHz, Mu-Law/A-Law | yes — verified, 8 codecs @ 4.8–11.025 kHz, handset |

---

## Modem 1 — Conexant CX93001 (`/dev/ttyACM0`)

USB CDC-ACM device, so the host-side baud rate is irrelevant (the USB pipe carries the data).
`115200` was used throughout.

### Identification

| Query | Response |
|---|---|
| `ATI` | `56000` |
| `ATI3` / `AT+GMR` | `CX93001-EIS_V0.2013-V92` |
| `ATI5` | `B5` |
| `AT+GMI` | `CONEXANT` |
| `AT+GMM` | `V90` |
| `AT+GCAP` | `+FCLASS,+MS,+ES,+DS` |
| `AT+GCI?` | `B5` (country = USA) |

### Modulations and speeds

`AT+MS=?` →
```
+MS: (B103,B212,V21,V22,V22B,V23C,V32,V32B,V34,V90,V92,ALM1,ALM2),(0,1),(300-33600),(300-48000),(300-56000),(300-56000)
```

| Modulation | Standard | Data rates |
|---|---|---|
| `B103` | Bell 103 | 300 bit/s |
| `B212` | Bell 212A | 1200 bit/s |
| `V21` | ITU V.21 | 300 bit/s |
| `V22` | ITU V.22 | 1200 bit/s |
| `V22B` | ITU V.22bis | 2400 bit/s |
| `V23C` | ITU V.23 | 1200/75 bit/s |
| `V32` | ITU V.32 | 4800–9600 bit/s |
| `V32B` | ITU V.32bis | 4800–14400 bit/s |
| `V34` | ITU V.34 | 2400–33600 bit/s |
| `V90` | ITU V.90 | up to 56000 down / 33600 up |
| `V92` | ITU V.92 | up to 56000 down / 48000 up (V.PCM-upstream) |
| `ALM1`, `ALM2` | Analogue-loopback diagnostic modes | — |

Rate window fields from `+MS=?`: automode `(0,1)`, min TX `300-33600`, max TX `300-48000`,
min RX `300-56000`, max RX `300-56000`.

Current setting after reset — `AT+MS?` → `V92,1,300,48000,300,56000`
(V.92, automode on, TX 300–48000, RX 300–56000).

Note the **48000 max TX**: that is the V.92 upstream PCM rate, and it is what distinguishes this
modem from Modem 2 (which caps upstream at 33600).

### Data compression

**V.42bis** — `AT+DS=?` → `+DS: (0,3),(0),(2048),(32)`; current `3,0,2048,32`

| Field | Range | Current | Meaning |
|---|---|---|---|
| direction | `0,3` | `3` | 0 = off, 3 = negotiate in both directions |
| negotiation fallback | `0` | `0` | do not disconnect if compression is not negotiated |
| dictionary size | `2048` | `2048` | codewords (fixed) |
| max string length | `32` | `32` | bytes (fixed) |

**V.44** — `AT+DS44=?` → `+DS44: (0,3),(0),(0),(256-2048),(256-2048),(32-255),(32-255),(512-4096),(512-4096)`;
current `3,0,0,512,512,32,32,1024,1024`

| Field | Range | Current |
|---|---|---|
| direction | `0,3` | `3` (both directions) |
| negotiation fallback | `0` | `0` |
| capability | `0` | `0` (stream mode) |
| max codewords TX / RX | `256-2048` | `512` / `512` |
| max string length TX / RX | `32-255` | `32` / `32` |
| max history size TX / RX | `512-4096` | `1024` / `1024` |

V.44 is the V.92-era replacement for V.42bis (LZJH instead of BTLZ) and typically gives
noticeably better throughput on compressible payloads. **Only this modem has it.**

### Error control and flow control

| Query | Response | Meaning |
|---|---|---|
| `AT+ES=?` | `(0-4,6,7),(0-4),(0-6,8,9)` | originator / answerer fallback negotiation options |
| `AT+ES?` | `3,0,2` | V.42 with detection phase, fallback to buffered mode |
| `AT+ER=?` | `(0,1)` | error-control reporting off/on (currently `0`) |
| `AT+DR=?` | `(0,1)` | compression reporting off/on (currently `0`) |
| `AT+IFC=?` | `(0-3),(0-2)` | DTE flow control (RTS/CTS active per `&K3`) |
| `AT+ILRR` | `ERROR` | local-rate reporting not supported |

Reset profile (`AT&V`): `B1 E1 L2 M1 N0 Q0 T V1 W0 X4 Y0 &C1 &D2 &G0 &J0 &K3 &Q5 &R1 &S0 &T5 &X0`
with `S46:138` (V.42bis enabled), `S48:7` (V.42 negotiation enabled), `S36:7`, `S38:20`.
`&Q5` = error-corrected (reliable) mode, `&K3` = RTS/CTS flow control.

### V.92 supplementary features

| Command | Range | Feature |
|---|---|---|
| `AT+PQC` | `0-3` | V.92 Quick Connect (shortened training) |
| `AT+PMH` | `0,1` | V.92 Modem-on-Hold |
| `AT+PIG` | `0,1` | PCM upstream ignore |
| `AT+PCW` | `0-2` | Call-waiting handling |

### Other

- `AT+FCLASS=?` → `0,1,1.0,2,8` — data, fax class 1 / 1.0 / 2, and voice (class 8).
- `AT+GCI=?` lists 66 country codes: `00,07,09,0A,0F,16,1B,20,25,26,27,2D,2E,31,36,3C,3D,42,46,50,51,52,53,54,57,58,59,61,62,64,69,6C,73,77,7B,7E,82,84,89,8A,8B,8E,98,99,9C,9F,A0,A1,A5,A6,A9,AD,AE,B3,B4,B5,B7,B8,C1,F9,FA,FB,FC,FD,FE`.
  Currently `B5` (USA).

---

## Modem 2 — Cirrus Logic CL-MD56xx (`/dev/ttyUSB0`)

An external serial modem reached through a Prolific PL2303 bridge. It **autobauds**: `AT` was
answered with `OK` at every rate tried (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200).

### Identification

| Query | Response |
|---|---|
| `ATI` | `56000` |
| `ATI1` | `ROM TEST OK` |
| `ATI2` | `CD08.55 - 643 SERIAL (V.90-ONLY) SPEAKERPHONE 01` |
| `ATI3` | `CL-MD56xx` |
| `ATI4` | `CCCVGEXX  V2.00 30/04/98` (DSP code, dated 1998-04-30) |
| `ATI5` | `Present, 32K DSP RAM` / `Host I/F: Serial` / `P. Mem.: 8 Bit 1 W.S.` / `D. Mem: 8 Bit 1 W.S.` / `DSP code location = INT ROM` |
| `ATI6` | `USA 1` (country) |

The `+G` identification commands (`+GCAP`, `+GMI`, `+GMM`, `+GMR`) all return `ERROR` — this is a
pre-V.250 command set.

### Modulations and speeds

`AT+MS=?` is **not supported** (`ERROR`), and `+MS` only accepts the full four-parameter form
`AT+MS=<mod>,<automode>,<min_rate>,<max_rate>`. Support was therefore established by probing each
modulation name and recording `OK` vs `ERROR`:

| Modulation | Result | Standard / rates |
|---|---|---|
| `V21` | OK | ITU V.21, 300 bit/s |
| `V22` | OK | ITU V.22, 1200 bit/s |
| `V22B` | OK | ITU V.22bis, 2400 bit/s |
| `V23C` | OK | ITU V.23, 1200/75 bit/s |
| `V32` | OK | ITU V.32, 4800–9600 bit/s |
| `V32B` | OK | ITU V.32bis, 4800–14400 bit/s |
| `V34` | OK | ITU V.34, 2400–33600 bit/s |
| `V90` | OK | ITU V.90, up to 56000 down / 33600 up |
| `V92` | ERROR | not supported (firmware is "V.90-ONLY") |
| `K56` | ERROR | K56flex not supported |
| `X2` | ERROR | US Robotics x2 not supported |
| `B103`, `B212` | ERROR | not selectable via `+MS` |

Bell 103 / Bell 212A are **not** absent from the hardware — on this command set they are selected
with `ATB1` (Bell) vs `ATB0` (ITU) rather than through `+MS`. The reset profile has `B0` (ITU).

Rate ceilings, established by probing `max_rate`:

- **V.34**: accepted up to `33600`; `36000` → `ERROR`.
- **V.90**: accepted `33600`, `48000`, `54666`, `56000`; rejected `42000`, `46000`, `57600`.
  The rejected values are not on the V.90 rate grid (28000 + n×1333⅓), so this is a genuine,
  spec-conformant V.90 implementation rather than a rounded-off approximation.

Current setting after reset — `AT+MS?` → `V90,1,0,33600` (V.90, automode on, max upstream 33600).
Consistent with `S37:019` in the profile, which is the Rockwell-style code for a 33600 line speed.

### Data compression

This modem uses the legacy vendor command set; ranges below were established by probing.

| Command | Accepted range | Reset value | Meaning |
|---|---|---|---|
| `%C` | `0,1` | `1` | data compression: 0 = off, 1 = enabled (V.42bis / MNP5) |
| `\N` | `0-6` | `3` | operating mode; 3 = auto-reliable (V.42 → MNP → buffered fallback) |
| `"H` | `0-3` | `3` | V.42bis negotiation direction; 3 = both directions |
| `"O` | `006-250` | `032` | V.42bis maximum string length, in bytes |
| `-J` | — | `1` | V.8bis enabled |

`"O` accepting exactly `6`–`250` matches the V.42bis specification's permitted range for maximum
string length. Note that unlike Modem 1, `%C` is a **plain on/off toggle** — there is no way to
select V.42bis and MNP5 independently.

There is **no V.44 support**: `+DS`, `+DS44`, `+DCS` and `+ES` all return `ERROR`.

### Reset profile (`AT&V`)

```
Flow-Control: RTS/CTS (&K3)
B0 E1 L1 M0 N1 T Q0 V1 W0 X4 Y0 &C1 &D2 &G0 &J0 &P0 &Q0 &S0 &U0 &Y0
%A013 %C1 %E1 %G1 \A3 \C0 \G0 \J0 \K5 \N3 \Q3 \T000 \X0 -C0 -J1 "H3 "O032
S00:001 S01:000 S02:043 S03:013 S04:010 S05:008 S06:002 S07:060 S08:002
S09:006 S10:014 S11:070 S12:050 S18:000 S25:005 S30:000 S33:000 S37:019
```

Relevant bits: `\Q3` / `&K3` RTS/CTS flow control, `%E1` auto-retrain, `%G1` rate renegotiation
enabled, `\A3` MNP block size 256, `%A013` auto-reliable fallback character = CR.

### Other

- `AT+FCLASS=?` → `0,1,8` — data, fax class 1, voice. (No class 2 fax, unlike Modem 1.)
- The `ATI2` string advertises speakerphone support, consistent with voice class 8.

---

## Line interface

Both modems are connected to a line that supplies dial tone. Tested with `ATX4` (full result-code
set, so `NO DIALTONE` is reportable) followed by `ATDW` (dial, explicitly wait for dial tone):

| Modem | `ATDW` result |
|---|---|
| Modem 1 | `NO CARRIER` |
| Modem 2 | `NO CARRIER` |

Neither returned `NO DIALTONE`, so **dial tone was detected on both lines** — inferred, since that
is the specific purpose of the `W` dial modifier.

### The PBX — AVM FRITZ!Box at 192.168.5.174

Both modem lines terminate on an **AVM FRITZ!Box** (identified from its web UI on tcp/80 and
tcp/443 — `<title>FRITZ!Box</title>`, AVM-specific CSP headers). It also has tcp/5060 open as an
internal SIP registrar. A passive SIP `OPTIONS` to udp/5060 got no reply, which is normal for a
FRITZ!Box from an unregistered peer.

This explains the whole numbering scheme, which follows AVM's internal-extension convention:

| Number | Endpoint |
|---|---|
| `**1` | analog FXS (a/b) port 1 — **Modem 2**, Cirrus CL-MD56xx, `/dev/ttyUSB0` |
| `**2` | analog FXS (a/b) port 2 — **Modem 1**, Conexant CX93001, `/dev/ttyACM0` |
| `**620` | IP-telephone extension — the ATA (see `ata.md`) |

So the modems are not on a line simulator but on a real PBX's analog ports, and the "dial tone
detected on both lines" result above is the FRITZ!Box generating dial tone. A third endpoint,
`**620`, is reachable as a SIP registration on the same box; credentials are in `ata.md` (not
duplicated here). That endpoint, and measurements of the audio path between it and both
modems, are documented in **`sip-audio-path.md`**.

Practical consequence: a `**1` ↔ `**2` call is switched entirely inside the FRITZ!Box, so its
analog ports and internal audio path — not the modems — set the ceiling on what can actually be
negotiated. Whether that path is clean enough for full-rate V.34 has **not** been measured yet; it
needs an actual data call.

### Directory numbers — which modem is which

The two lines can reach each other and are numbered `**1` and `**2`. The assignment was determined
by dialling from each modem in turn with auto-answer disabled (`ATS0=0`) and watching the other for
`RING`:

| Dialled | From Modem 1 (`ttyACM0`) | From Modem 2 (`ttyUSB0`) |
|---|---|---|
| `**1` | Modem 2 **rings** | `BUSY` after 4 s (own line) |
| `**2` | `BUSY` (own line) | Modem 1 **rings** (10 rings observed) |

All four trials agree, and the two `BUSY` results are independent confirmation — each modem
reports its own number busy when it dials it.

> ### **`**1` = Modem 2 — Cirrus Logic CL-MD56xx — `/dev/ttyUSB0`**
> ### **`**2` = Modem 1 — Conexant CX93001 — `/dev/ttyACM0`**

Calls were never answered in these trials because `ATS0=0` was set deliberately; the dialling modem
ended in `NO CARRIER` (or `BUSY`) and both were hung up and reset afterwards. To actually complete a
call, set `ATS0=1` on the answering side.

---

## Voice mode

**Yes — both modems do voice, and it was verified live, not just from the class list.**
Both advertise `8` in `AT+FCLASS=?`, both accept `AT+FCLASS=8`, and both were driven through a
real off-hook record to confirm audio samples actually flow.

### Live record test

Sequence used on each: `AT+FCLASS=8` → `AT+VSM=<codec>,8000` → `AT+VLS=1` (off-hook, telephone
line) → `AT+VRX` (start record), then a 3-second read of the sample stream.

| | Modem 1 (Conexant) | Modem 2 (Cirrus) |
|---|---|---|
| Codec used | `131` Mu-Law, 8 kHz | `0` 8-bit linear, 8 kHz |
| `AT+VRX` | `CONNECT`, samples followed | `CONNECT`, samples followed |
| Throughput | 24064 bytes / 3.00 s = **8018 byte/s** | 24100 bytes / 3.00 s = **8030 byte/s** |
| Sample data | all 32 leading bytes vary | 14/32 leading bytes vary, clean periodic waveform |

8018 and 8030 byte/s are both within 0.4 % of the 8000 byte/s expected for 8-bit samples at 8 kHz,
so the audio path is genuinely live and correctly clocked — not a stub that returns `OK` and
silence. Both streams contained a varying periodic waveform (dial tone on the off-hook line),
not a constant fill pattern.

### Line selections — `AT+VLS=?`

Modem 1 offers 9 configurations, Modem 2 also 9, but they are **not the same set**:

| Code | Modem 1 | Modem 2 | Meaning |
|---|---|---|---|
| `0` | yes | yes | all relays off / on-hook |
| `1` | `T` | `T` | telephone line (off-hook) |
| `2` | — | `L` | local phone |
| `4` | `S` | `S` | internal speaker |
| `5` | `ST` | `ST` | speaker + line |
| `6` | `M` | `M` | microphone |
| `7` | — | `MST` | mic + speaker + line |
| `8` | `S1` | — | speaker (alt.) |
| `9` | `S1T` | — | speaker + line (alt.) |
| `11` | `M1` | — | microphone (alt.) |
| `13` | `M1S1T` | — | mic + speaker + line |
| `14` | — | `H` | handset |
| `15` | — | `HT` | handset + line |

Modem 2 is the only one with **handset (`H`, `HT`) and local-phone (`L`) selections**, consistent
with its `ATI2` speakerphone claim and its external-box form factor. Modem 1 exposes duplicate
speaker/mic paths (`S`/`S1`, `M`/`M1`) instead.

### Codecs — `AT+VSM=?`

**Modem 1** — 7 codecs, all fixed at 8000 Hz only:

| ID | Codec | Bits |
|---|---|---|
| `0` | Signed PCM | 8 |
| `1` | Unsigned PCM | 8 |
| `129` | IMA ADPCM | 4 |
| `130` | Unsigned PCM | 8 |
| `131` | Mu-Law | 8 |
| `132` | A-Law | 8 |
| `133` | 14-bit PCM | 14 |

**Modem 2** — 8 codecs, each supporting **five sample rates** (4800, 7200, 8000, 9600, 11025 Hz):

| ID | Codec | Bits |
|---|---|---|
| `0`, `128` | 8-bit Linear | 8 |
| `1`, `129` | 16-bit Linear | 16 |
| `2`, `132` | 4-bit ADPCM | 4 |
| `140` | `CL1` (Cirrus proprietary) | 8 |
| `141` | 3-bit ADPCM | 3 |

The two are complementary rather than one being strictly better:

- **Modem 1** has the telephony-standard companding laws — **Mu-Law and A-Law** — plus 14-bit
  linear. That makes its output directly usable as `.au`/`.wav` G.711 without transcoding, which
  Modem 2 cannot do at all.
- **Modem 2** has **selectable sample rates up to 11025 Hz** and lower-bitrate ADPCM (down to
  3-bit), where Modem 1 is locked to 8000 Hz. It also has a proprietary `CL1` codec.

So for G.711-compatible capture use Modem 1; for higher-sample-rate or low-bitrate capture use
Modem 2.

### Voice feature commands

| Command | Modem 1 | Modem 2 | Feature |
|---|---|---|---|
| `+VSP` | `(0,1)` | `0,1` | **speakerphone** on/off — both |
| `+VDX` | `(0,1)` | not supported | speakerphone duplex control |
| `+VGS` | `(0-255)` | not supported | speaker gain |
| `+VGT` | `(0-255)` | `121-131` | transmit (playback) gain |
| `+VGR` | `(0-255)` | `121-131` | receive (record) gain |
| `+VTS` | `(200-3000),(200-3000),(0-255)` | `(200-3000),(200-3000),(5-500)` | tone generation: two freqs 200–3000 Hz + duration |
| `+VTD` | `(0-255)` | `5-255` | DTMF/tone duration |
| `+VNH` | `(0-2)` | `0-2` | automatic hangup control |
| `+VCID` | `(0-2)` | `0,1,2` | **Caller ID** (formatted / unformatted) — both |
| `+VRA` | `(0-255)` | not supported | ringback-never-came timer |
| `+VRN` | `0-25` | not supported | ringback-gone timer |
| `+VIT` | `(0-255)` | not supported | DTE inactivity timer |
| `+VDR` | `(0-1),(0-6)` | not supported | distinctive ring detection |
| `+VIP` | OK | OK | reset voice parameters to default |
| `+VEM`, `+VBT` | `ERROR` | — | event monitoring / buffer threshold not supported |

Modem 1 has the substantially richer control surface: duplex speakerphone control, speaker gain,
distinctive-ring detection, and the ringback/inactivity timers that make unattended
answering-machine logic practical. Modem 2 covers the basics (speakerphone, gains, tone
generation, caller ID) but has none of the timers, so answer-detection logic would have to be
implemented on the host side.

Both support **tone generation over the full 200–3000 Hz DTMF/signalling range** with independent
dual frequencies, which is useful for generating test tones into the rig. Note Modem 2's tone
duration has a **minimum of 5** (10 ms units) where Modem 1 accepts `0`.

### Caveats found while testing voice

1. **`<DLE><CAN>` did not abort `+VRX` on either modem.** After `AT+VRX` both kept streaming
   samples straight through the abort sequence, and subsequent `AT+VLS=0` / `AT+FCLASS=0` / `ATZ`
   commands were swallowed by the audio stream — leaving both modems **stuck off-hook in record
   mode**. Recovery required **dropping DTR** (both have `&D2`, so DTR loss forces on-hook and
   command mode), then `ATH0`, `AT+FCLASS=0`, `ATZ`. Any voice test harness for this rig must have
   a DTR-drop escape path; do not rely on in-band abort.
3. **Voice mode is a mode, not a query.** `AT+FCLASS=8` persists until explicitly reset. Always
   confirm with `AT+FCLASS?` (this query *does* work correctly on both, unlike the legacy
   `?` trap on Modem 2's `%C`/`\N`/`-J`) and reset to `AT+FCLASS=0` when done.
3. `AT+VLS=1` takes the line **off-hook** — it is the voice-mode equivalent of picking up the
   handset, not a passive query. Use `AT+VLS=0` (or DTR drop) to release.

Both modems were confirmed returned to `+FCLASS: 0`, on-hook, with reset profiles intact after
this test (`AT+MS?` → `V92,1,300,48000,300,56000` and `AT+DS?` → `3,0,2048,32` on Modem 1;
`%C1 \N3 -J1 "H3 "O032` on Modem 2).

---

## Practical notes for the test rig

1. **Always reset a modem before using it.** Every session must start with a reset, because probing
   and aborted calls leave persistent state behind (`+FCLASS=8`, altered `+MS`, `%C0`, off-hook).
   The reliable sequence, used by all scripts here, is: **drop DTR** for ~0.6 s (both modems are
   `&D2`, so this forces on-hook and command mode even mid-stream) → `ATH0` → `ATZ` → then set the
   options you want (`ATS0=`, `ATX4`, …). `ATZ` alone is *not* enough to recover a modem stuck
   streaming voice samples.
2. **A modem-to-modem call will not reach 56k.** V.90 and V.92 downstream rates require one end to
   be a digital, PCM-side server modem. Two analogue client modems calling each other over a line
   simulator negotiate V.34 instead, so expect **33600 bit/s** as the practical ceiling for a
   Modem 1 ↔ Modem 2 link, not the `56000` both report from `ATI`.
3. **The two modems are asymmetric.** Only Modem 1 does V.92, V.44 and fax class 2. Any test that
   needs V.44 compression or V.92 features (Quick Connect, Modem-on-Hold) cannot use Modem 2 as the
   far end. Their common ground is V.21/V.22/V.22bis/V.23/V.32/V.32bis/V.34 with V.42/V.42bis.
4. **Gotcha — `?` is not a query on Modem 2.** `AT%C?`, `AT\N?` and `AT-J?` all return `001` but
   actually *write* the value 0 to those settings; they are parsed as `%C0`, `\N0`, `-J0`. This
   silently disabled compression and error correction during enumeration. Always read Modem 2's
   state with `AT&V` and never with a `?` suffix. Modem 1's `+`-style queries (`AT+DS?` etc.) behave
   correctly.
5. **`AT+MS` syntax differs.** Modem 1 accepts partial forms (`AT+MS=V34`); Modem 2 requires all
   four parameters (`AT+MS=V34,1,0,33600`) and returns `ERROR` otherwise.
6. **Both modems are set to country USA** (Modem 1 `+GCI=B5`, Modem 2 `ATI6` → `USA 1`). On a German
   PSTN or a line simulator configured for German tones this can break dial tone detection, pulse
   dialling and call-progress detection. Modem 1 can be re-regioned with `AT+GCI=<hex>`; Modem 2's
   country appears to be fixed in firmware.
7. **`ModemManager` is running** on the Pi but currently claims neither port (`mmcli -L` → "No
   modems were found"), so there is no contention today. If it ever starts probing these ports it
   will corrupt sessions — consider a udev rule with `ID_MM_DEVICE_IGNORE=1` for both devices.
8. **Voice mode works on both**, but `<DLE><CAN>` will not abort a `+VRX` recording — budget a
   DTR-drop escape path into any voice harness, or the modem stays off-hook streaming audio.
9. **Modem 2 must dial with an explicit `ATDT`.** `ATDT**2` correctly rang Modem 1, but bare
   `ATD**2` returned `BUSY` — the `*` digits are mishandled without the tone-dial modifier, even
   though `AT&V` already reports `T`. Always use `ATDT` on Modem 2. Modem 1 dials correctly with
   plain `ATD`.
10. **Modem 2 is slow to give up on a call.** Its `S7` is 60 (vs 50 on Modem 1), so a ring-no-answer
   takes ~62 s to return `NO CARRIER`. Allow at least 70 s in call-test timeouts, or lower `S7`.
11. **Modem 2 autobauds**, so any host rate from 1200 to 115200 works. Use 115200 with RTS/CTS to
   avoid throttling a compressed V.34 link. Modem 1 is CDC-ACM, so its host rate is a no-op.

## Reproducing this enumeration

Helper scripts were left on the Pi in `~/modemprobe/`:

| Script | Purpose |
|---|---|
| `at.py <port> <baud> <cmd>...` | Send AT commands, dump raw replies |
| `probe.py <port> <baud> <cmd>...` | Send commands, classify each `OK`/`ERROR` (used for capability probing); issues `ATZ` at the end |
| `scan.py <port>` | Baud-rate scan |
| `dt.py` / `dt2.py <port> [dialstr]` | Dial tone / line checks |
| `voice.py <port> <codec>` | Voice-mode record test (enters class 8, off-hook, `+VRX`, measures byte rate) |
| `recover.py <port>` | Force a stuck modem back on-hook into data mode via DTR drop |
| `whois.py` | Dial `**1`/`**2` from each modem, watch the other for `RING` (number assignment) |
| `m2dial.py` | Modem 2 dial-string variants with a 70 s window |

Both modems were returned to their stored profiles with `ATZ` after probing, and the restored state
was verified with `AT&V`.

## Error correction, and why it has to be switched off

Both modems default to **V.42/LAPM with V.42bis compression**, and V.32 says
nothing about either. A soft modem that speaks only V.14 will complete the whole
V.32 start-up, report `CONNECT`, and then deliver nothing usable to the far DTE —
because the modem is waiting for a LAPM partner. This cost a long detour; see
"It was V.42" in `v32-ans-path.md`.

A soft V.42 now works against both modems — `softmodem/v42.py`, driven from the
V.32 data phase with `--ec` — so switching it off is no longer required; see
`v42-error-correction.md`. To run *with* it, leave the error-correction defaults
alone and disable only compression (`ATS46=136` on the Conexant, `AT%C0 "H0` on
the Cirrus): we have no V.42bis, and a negotiated compression we cannot perform
arrives as BTLZ codewords inside perfectly good I frames.

### V.42 quirks, one per modem

Both were found by answering each retransmitted XID command with a different
variant until one was accepted, and neither is in the Recommendation.

| | Conexant `**2` (CX93010) | Cirrus `**1` (CL-MD56xx) |
|---|---|---|
| detection phase | **skipped entirely** — opens with flags then XID | skipped the same way |
| XID response with PI 3 (HDLC optional functions) | **rejected**, even byte-identical to its own | accepted first try |
| XID request | N401 = 128 octets, k = 15 | N401 = **64** octets, k = 15 |
| PL on PI 3 in its own command | 3, where Table 11a Note 1 says 4 | — |

So the Conexant needs `--xid-no-opt` and the Cirrus does not. Because neither
runs the detection phase, an answerer that only watches for the ODP concludes
there is no V.42 out there while the far end retransmits XID at it.

**The two use different command sets and neither accepts the other's.** Read the
profile with `AT&V` afterwards rather than trusting that a command took — the most
useful single habit found in this work.

| | Conexant `**2` (CX93010) | Cirrus `**1` (CL-MD56xx) |
|---|---|---|
| default | `&Q5` (error-corrected), `S48:7` (LAPM), `S46:138` (V.42bis) | `\N3` (auto-reliable), `%C1`, `"H3` |
| turn it off | `AT&Q6`, `ATS48=128`, `ATS46=136` | `AT\N0`, `AT%C0`, `AT"H0`, `AT\A0` |
| the other's commands | `\N0` ok, `%C0` ok, `"H0`/`\A0` **ERROR** | `&Q6`, `S48=`, `S46=` all **ERROR** |
| `AT\N1` (direct) | **ERROR** — not supported | ok |

Two traps:

- **`AT\N1` returning ERROR is silent** if the setup script does not check. It sat
  in a dial script through every live call in this project before anyone read the
  reply.
- **Direct mode (`&Q0`, `\N1`) ties the DTE port speed to the line rate.** With the
  Pi's port at 115200 against a 9600 connection, the call comes up and the DTE
  still sees nothing. `&Q6` — normal mode, speed buffered, no error correction —
  is what fits.

Offering the union of both sets and printing which were accepted is the robust
approach; half the ERRORs are expected.

## Two hardware quirks worth knowing

- **The Conexant's ACM port wedges** after roughly ten calls in a session: the DTR
  ioctl itself blocks, and neither an in-band abort (`\x10\x18`, `+++`) nor a
  `USBDEVFS_RESET` clears it. It needs a physical replug. `recover.py` will not
  help once it is in that state.
- **The Cirrus's trellis receiver is weak.** Bridged onto a V.32 9600
  trellis-coded call with the Conexant, it received **20.0%** of the pattern
  correctly while the Conexant received **99.6%** of its. Trellis was the optional
  alternative at 9600 and implementations varied. For trellis work, use the
  Conexant; the Cirrus is fine at 9600 nonredundant.
