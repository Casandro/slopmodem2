# slopmodem2

A software modem in pure Python — no numpy, no scipy, no `audioop` — that places
and answers calls over SIP/RTP and talks to real hardware modems on the other
end of a FRITZ!Box.

Working, against two USB modems (a Conexant CX93010 and a Cirrus CL-MD56xx):

| | |
|---|---|
| **V.21** | 300 bit/s FSK, both channels |
| **V.22 / V.22bis** | 1200 and 2400 bit/s, full duplex |
| **V.32** | 4800, 9600 non-redundant, 9600 trellis coded |
| **V.32bis** | 7200, 9600, 12000, 14400 on one encoder and one Viterbi |
| **V.8** | ANSam, CM/JM/CJ negotiation |
| **V.14** | asynchronous-to-synchronous conversion |
| **V.42** | HDLC framing, detection phase, LAPM, XID |
| echo cancellation | for the hybrid on the far side of the box |
| terminal server | inbound calls routed by dialled number, bridged to TCP |

Best measured: V.32bis at 12000 trellis coded, carrying V.42 in **both directions
at once for 83 seconds** — 10 818 bit/s in and 10 789 bit/s out, 90% of the
channel each way, 1760 frames with none discarded and no retransmissions. At 9600
non-redundant the same configuration reaches 9183 bit/s, 96% of the channel.

14400 negotiates on every attempt and will not hold: a 5.5 retrain every 7 to
13 seconds. See `testrig/v42-error-correction.md`.

## Layout

    softmodem/     the modem: DSP, modulations, protocols, tests
    testrig/       what was measured and what it taught, in detail
    testrig/tools/ SIP and RTP plumbing, capture and orchestration
    rules/         the bar the project was held to

Run the tests with `cd softmodem && python3 test_offline.py` (and the other
`test_*.py` — ten suites, all offline except where they say otherwise).

## The write-ups are the point

Most of the value here is in `testrig/`, which records the measurements rather
than just the conclusions — including the wrong turns, because several of them
were instructive and a couple were expensive:

| | |
|---|---|
| `v8-negotiation.md` | V.8, and getting a modem to answer at all |
| `v22-modem.md` | V.22bis end to end |
| `v32-ans-path.md` | V.32: trellis code recovered from captured symbols, then confirmed against the printed tables |
| `v42-error-correction.md` | V.42, LAPM, and two modem quirks that are in neither the spec nor the datasheets |
| `echo-cancellation.md` | why 9600 needed it, and how not to make a clean line worse |
| `sip-audio-path.md` | the transport, and the 19 dB echo hiding in it |
| `terminal-server.md` | bridging calls to TCP, and a V.42 wedge it uncovered |
| `modem.md` | the two modems, their command sets and their disagreements |

## Not in the repository

**SIP credentials.** `testrig/tools/sipcfg.py` reads the account out of
`testrig/ata.md`, which is deliberately the only place they live and is not
committed. Copy `testrig/ata.md.example` and fill it in, or set `SIP_HOST`,
`SIP_USER` and `SIP_PW`.

**The ITU-T Recommendations.** The work leans on them constantly and cites clause,
table and figure numbers throughout, but they are ITU copyright and are not
redistributed here. Download what you need from itu.int into `references/`.
