"""Receive-driven RTP media loop.

Rationale
---------
Pacing outbound RTP off the local wall clock (``next += 0.02``) lets the two
endpoints drift apart: our sample clock and the peer's are independent, and
neither is exact.  Measured on this rig, the FRITZ!Box delivered 762 frames
(15.2 s of audio) in 14.0 s of wall clock -- about 9 % fast.  Over a call that
slip means we either starve or flood the peer's jitter buffer, and for a
soft-modem a slipping sample clock destroys carrier tracking outright.

The fix is to let the peer be the clock: send exactly one frame for each frame
received.  In steady state the ratio is 1:1 and the two ends stay locked
however far the peer's nominal rate is from 50 fps.

A short priming burst gets media flowing (the peer may wait to hear us first),
and a watchdog keeps the stream alive if the peer goes quiet -- otherwise a
silent peer would deadlock both sides.  Both are counted separately so the
1:1 lock can be verified rather than assumed.
"""
import socket, struct, time, random

def pump(rtp, remote, pt, seconds, gen, prime=2, watchdog=0.060, on_sip=None):
    """Drive RTP for `seconds`, sending one frame per frame received.

    rtp     : bound UDP socket (should have a short timeout set)
    remote  : (ip, port) of the peer, or (None, None) to receive only
    pt      : payload type to send
    gen     : gen(n) -> 160-byte payload for outbound frame n
    prime   : frames to send up front to get media started
    watchdog: if no inbound frame for this long, send one anyway (seconds)
    on_sip  : optional callable, polled each iteration (e.g. to answer BYE);
              return True to stop early

    Returns dict with the captured audio and frame counters.
    """
    ssrc = random.getrandbits(32)
    seq = random.getrandbits(15)
    ts = 0
    n_out = 0
    inbuf = bytearray()
    n_in = 0
    n_watchdog = 0
    bins = {}          # wall-second -> inbound frame count, to separate a
                       # startup burst from a genuine clock-rate offset

    def emit():
        nonlocal seq, ts, n_out
        pay = gen(n_out)
        hdr = struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
        if remote[0] and remote[1]:
            rtp.sendto(hdr + pay, remote)
        seq += 1
        ts += 160
        n_out += 1

    for _ in range(prime):
        emit()

    t0 = time.time()
    last_in = t0
    stopped = False
    while time.time() - t0 < seconds:
        try:
            d, _ = rtp.recvfrom(4096)
            if len(d) > 12 and (d[1] & 0x7F) in (0, 8):
                inbuf.extend(d[12:])
                n_in += 1
                last_in = time.time()
                bins[int(last_in - t0)] = bins.get(int(last_in - t0), 0) + 1
                emit()                 # <-- one out per one in: the clock lock
        except socket.timeout:
            pass
        now = time.time()
        if now - last_in > watchdog:
            emit()
            n_watchdog += 1
            last_in = now              # rate-limit the watchdog
        if on_sip is not None and on_sip():
            stopped = True
            break
    dur = time.time() - t0
    return {"audio": inbuf, "in": n_in, "out": n_out, "watchdog": n_watchdog,
            "dur": dur, "stopped_early": stopped, "bins": bins}

def report(st):
    ratio = (st["out"] / st["in"]) if st["in"] else float("nan")
    print("  RTP: in=%d out=%d (out/in=%.3f, %d watchdog) %.1fs audio in %.1fs wall"
          % (st["in"], st["out"], ratio, st["watchdog"],
             len(st["audio"]) / 8000.0, st["dur"]))
    if st["in"]:
        print("      mean peer rate %.1f fps (nominal 50.0)" % (st["in"] / st["dur"]))
        b = st.get("bins") or {}
        if b:
            ks = sorted(b)
            print("      inbound fps per wall-second: "
                  + " ".join("%d" % b[k] for k in ks))
            tail = [b[k] for k in ks[2:-1]]
            if tail:
                print("      steady-state mean (excl. first 2s) = %.1f fps"
                      % (sum(tail) / len(tail)))
