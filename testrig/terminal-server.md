# A modem terminal server

Inbound modem calls, routed by the number that was dialled, bridged to a TCP
connection. A caller with a real modem reaches a service over IP.

    python3 termsrv.py --routes ../testrig/routes.example

A raw byte pipe, and nothing else: no AT commands, no DTR/DSR or RTS/CTS, no
`CONNECT` or `NO CARRIER` text. What the caller types reaches the host and what
the host writes reaches the caller.

## What it does, measured

One call, a Cirrus CL-MD56xx dialling in, bridged to a banner-and-echo server on
127.0.0.1:2323, scored by echoing printable ASCII back to the caller:

| | |
|---|---|
| negotiated | **12 000 bit/s, V.42/LAPM, 0 retrains** |
| caller to host | 118 561 octets |
| host to caller | 118 528 octets, both directions at once for 101.8 s |
| echoed back to the caller | **114 880 of 114 880 correct, 100.0000%** |
| the host's banner | delivered, and printed on the caller's terminal |

The three rejection paths, in one daemon run each, with the daemon still serving
afterwards: a number with no rule gets **404**, twice in a row; a rule whose
backend is not listening gets **503** (`connect failed [Errno 111]`); and a good
call in the same process bridges normally.

## The dialled number is not where you would look for it

The plan was to match the user part of the Request-URI and of `To:`. On the rig
neither carries the dialled number. A FRITZ!Box rewrites both to the registered
account:

    INVITE sip:wurstuser@192.168.5.117:46894 SIP/2.0
    To: <sip:wurstuser@192.168.5.117:46894>
    P-Called-Party-ID: <sip:**9@fritz.box>

Matching only the first two gives *every* call the same number -- the account
name -- whatever was dialled, and the first rig test duly routed nowhere and
answered 404. RFC 3455's `P-Called-Party-ID` carries the extension here, so
`numbers_of()` offers the Request-URI, `To`, `P-Called-Party-ID`, `Diversion` and
`X-Dialed-Number`, in that order, deduplicated. `termsrv.py --dump-invite` prints
the whole INVITE, which is how to find out what a given switch offers rather than
assuming.

**This is the box, not a general rule, and the rig cannot test the other case.**
A FRITZ!Box gives no way to make it address the Request-URI to the dialled
extension -- it always rewrites to the registered account -- so the
Request-URI-and-`To` matching that was the original design is exercised by
`test_termsrv.py` only, never end to end on this hardware. Plenty of switches do
put the number in the Request-URI, and against one of those the first two sources
would be the ones that matter and `P-Called-Party-ID` would be absent. That is
the reason all five sources are tried rather than picking a winner: which header
carries the number is a property of the switch in front of you, and the rig can
demonstrate exactly one of the possibilities.

Rules are tried in file order, each against every number, and the first rule
matching any of them wins -- rules are the outer loop, so an earlier rule
matching the To number beats a later rule matching the Request-URI. `fullmatch`,
not `search`, so `620` does not match `**6201`; a prefix rule is `620.*`.

## The bug this found: V.42 detection can wedge a call for good

The highest-value thing in this work is not the bridge. It is that
`v32answer.py --ec` has always been able to hang a call permanently, and nobody
had noticed because nothing had ever pointed a byte stream through it.

7.2.1's detection phase matches DC1 with alternating parity against the
*scrambled* data-phase bit stream. A V.14-only far end whose DTE happens to send
characters during the 750 ms window produces that pattern by chance. Detection
then declares LAPM and the answerer sends its ADPs -- and 7.2.1.1 leaves
establishment to the *originator*, which a V.14 modem will never do. The session
sits in phase `lapm` with `lapm.state` `disconnected` and `up` False for the rest
of the call: `put()` accepts nothing, `received()` returns `b""` forever,
`fell_back` is never set, and **not one octet moves in either direction, with no
error reported anywhere**.

Reproduced soft to soft at 9600 -- answerer `ec=True`, originator `ec=False` and
chattering from the first data frame -- 0 octets recovered across 1500 frames:

    ec.phase        : lapm
    ec.up           : False
    lapm.state      : disconnected
    ec_fell_back    : False
    bytes recovered : 0

The fix is a watchdog in the bridge: if detection concludes LAPM but the link has
not come up `LAPM_UP_MAX` seconds later, force 7.2.1.3's fallback. The same run
then carries 28 833 octets. `v32fsm.force_v14()` exists so a data-phase owner can
say this without reaching into `_ec_fallback` across a module boundary.

**It fires on real hardware.** The first working rig call logged
`7.2.1.3 forced: detection said LAPM but the link never came up - running V.14`
and went on to carry 46 136 octets. Without the watchdog that call carries zero.

## Two configuration traps, both found on the rig

**The echo canceller is not optional here.** The first attempt ran with
`cancel_echo=False` and `trellis=True`, and the handshake reached
`R3: selecting 12000` and then sat there -- `no E after 2432T of R3`, with R2
having arrived sixteen seconds late. Matching `orch_throughput.py`'s proven
configuration (canceller on, trellis off, `--bis` alone carrying the
trellis-coded bis rates) connected first time. The canceller is on by default;
`--no-echo` turns it off.

**`capture=False` is mandatory.** `rtp.pump` defaults to keeping every inbound
and outbound payload for the life of the call. That is fine for a 95-second
diagnostic and about 58 MB an hour for a terminal session that is supposed to
last.

## Flow control, and one idea that does not work

Backpressure in both directions is the *absence of a read*, not a buffer. While
the handshake runs, while a retrain is in progress, and whenever the modem's
transmit queue is full, the bridge simply does not call `recv()`. The kernel
window closes and the host is throttled to the line rate by TCP itself, with
nothing of ours in the path to size wrongly or go stale. A host that writes a
banner on connect has it waiting in the socket buffer, delivered the instant the
data phase opens.

The obvious idea for the other direction -- stop draining `received()` and let
LAPM's window backpressure the far modem -- does not work, and it is worth
recording why. `v42.Lapm` acknowledges every I frame on receipt and implements no
RNR; the class docstring says so. The window governs *unacknowledged* frames, and
we acknowledge everything immediately, so it never closes. Not draining just
moves the same bytes from a buffer we can see into `lapm.inq`, which is unbounded
and which nobody will think to check. So the bridge always drains, keeps one
bounded visible buffer, and hangs up if a peer stays wedged for 30 s.

`recv()` is never called with a length of zero. `recv(0)` returns `b""`, which is
indistinguishable from end of stream, and that one line would hang every call up
moments after it connected. When the queue is full and we are not reading at all,
a `MSG_PEEK` once a second notices a FIN that would otherwise go unseen.

## Ending a call

`v32fsm` never assigns `FAILED`, and once `retrains` reaches `RETRAIN_MAX` the
trigger returns `None` on its first line and nothing asks again -- the FSM then
sits in DATA with a dead receiver for as long as anyone lets it. A daemon has to
impose its own liveness policy or calls hang until the cap.

| reason | when |
|---|---|
| `sip-bye` | the caller hung up |
| `tcp-eof` | peer closed, and the modem's queues have drained |
| `tcp-error`, `tcp-stalled` | socket error; or 30 s wedged over 256 KiB pending |
| `no-handshake` | 60 s without reaching the data phase |
| `carrier-lost` | 5 s of no decision-directed lock while in DATA |
| `retrain-stuck` | 20 s outside DATA after once being in it |
| `lapm-down` | LAPM failed, but only once it had been up |
| `rtp-dead` | 10 s, 500 frames, without inbound media |
| `idle` | 900 s with no octet moving either way |

"Drained" is precise: for LAPM, `outq` empty **and `sent` empty** -- every I frame
acknowledged by the far modem, which is a real delivery receipt and the one thing
V.14 cannot offer. For V.14, `enc.pending() == 0 and not enc.bits`, then half a
second of linger for what is already in the modulator.

On a peer FIN the bridge stops reading, keeps flushing modem-to-TCP, waits for
those queues, then `shutdown(SHUT_WR)` and hangs up. The modem side has no
half-close to mirror -- dropping carrier kills both directions at once -- so a
half-open TCP session against a live call has no terminating condition. The
linger is what gets a host's farewell text onto the caller's screen before the
carrier goes.

## Follow-up left undone

`v32flow.feed_budget()` is the extracted copy of the per-frame backpressure
arithmetic; the originals are still inlined in `v32answer.py` and `v32call.py`.
They were left alone deliberately: those two *are* the instruments used to
qualify hardware runs, and changing them changes the measuring apparatus. A test
asserts the extracted function matches the inlined expression exactly across 36
cases, so collapsing them later is a substitution already proved to be a no-op.
