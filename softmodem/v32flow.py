"""How many DTE octets it is safe to hand the modem this frame.

The arithmetic below already exists twice, copied verbatim, in v32answer.py and
v32call.py. A third copy is exactly how the feeder bug survived as long as it
did -- `(pat + pat)[i:i + feed]` capped every frame at 18 octets regardless of
the rate, which read as 900 byte/s at 7200, at 9600 and at 12000, and looked for
a long time like a property of the line rather than of the slicing. So the bridge
takes it from here instead of copying it a third time.

It is a free function in its own module rather than a method on v32fsm's Startup
because this is policy -- how far ahead of the line to buffer -- and v32fsm is
mechanism, and the most heavily tested file in the project. Nothing here imports
anything; it only reads attributes off the modem object it is given.

Three branches, because the thing providing the backpressure differs:

**LAPM up.** The transmit queue does it. Offer the caller's whole feed while the
queue is short and nothing while it is long. Note what governs and what does not:
v42.Lapm acknowledges every I frame on receipt and implements no RNR, so the
*window* (k = 15) is the binding constraint and the queue threshold is slack.
Measured soft to soft at 9600, throughput is identical -- 8498 bit/s -- at every
threshold from 256 to 4096, with len(lapm.sent) pinned at 15 throughout. The
threshold therefore costs nothing to lower, and lowering it bounds how much
already-committed output a caller has to sit through after the far end stops
sending. 4096 stays the default so the two existing call sites are unchanged.

**Detection has not finished.** put() buffers into ecq, which is handed to the
V.14 converter in one go if detection fails -- so bound it here too, or the
fallback begins with a burst it will spend the next second deleting.

**V.14.** Ten bits on the line per eight-bit character, so the line's budget is
rate/10 characters a second and rate/500 per 20 ms frame. Pace to that, keep some
back, and still bound the queue.
"""

# Characters we are willing to have queued ahead of the line when there is no
# error-correcting entity to push back for us. One 20 ms frame is 24 characters
# at 12000 bit/s, so this is a few frames of margin -- enough that the line never
# idles, and well under dte.AsyncEncoder's hiwater of 128, past which V.14 starts
# deleting every stop bit it sees.
V14_AHEAD = 64

# ... and how much of the line's own character budget to use. A bound on the
# queue alone is not enough: it still lets us offer exactly the line rate, and
# V.14 at exactly the line rate has no margin for the two clocks to differ, which
# is what AsyncEncoder's docstring means by the slips coming back from the
# Conexant. Feeding at 95% leaves the slack V.14 needs and costs 5% of a
# direction that has no error correction to protect it anyway.
V14_MARGIN = 0.95

# The queue depth the two existing call sites use. See the module docstring for
# why this is slack rather than the real limiter.
LAPM_OUTQ_HI = 4096


def feed_budget(m, feed, ahead=V14_AHEAD, margin=V14_MARGIN,
                lapm_hi=LAPM_OUTQ_HI):
    """Octets that may be offered to m.put() this frame. Zero means none.

    Never bypass this. V.14 has no downstream flow control of any kind, and
    once dte.AsyncEncoder's queue passes its hiwater every stop bit starts being
    deleted -- a stream the far framer cannot acquire on. That read as 27.9%
    printable once and looked like a line fault.
    """
    if m.ec is not None:
        return feed if (m.ec.up and len(m.ec.link.lapm.outq) < lapm_hi) else 0
    if m.want_ec and not m.ec_fell_back:
        return min(feed, ahead - len(m.ecq))
    per_frame = int((m.rate or 4800) / 500.0 * margin)
    return min(feed, max(per_frame, 1), ahead - m.enc.pending())
