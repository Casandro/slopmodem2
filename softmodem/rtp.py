"""Receive-driven RTP pump with a streaming callback.

Same pacing rule as testrig/tools/media.py -- one frame out for every frame in,
so the peer is the clock and our sample clock cannot drift against it. The
difference is the callback signature: media.py's `gen(n)` cannot see inbound
audio, but a modem must demodulate what it hears before deciding what to send.

`on_frame(inbound_payload_or_None) -> outbound_payload`
  inbound is None for the priming burst and for watchdog frames, where there is
  no received frame to respond to.
"""
import socket, struct, time, random

def pump(rtp, remote, pt, seconds, on_frame, prime=2, watchdog=0.060,
         on_sip=None, on_stop=None, capture=True, accept_pts=None,
         frame_bytes=160):
    """accept_pts: payload types to treat as media. Must include the negotiated
    type -- the 1:1 pacing is driven by inbound frames, so filtering them out
    would leave only the watchdog driving transmission."""
    if accept_pts is None:
        accept_pts = (0, 8, pt)
    ssrc = random.getrandbits(32)
    seq = random.getrandbits(15)
    ts = 0
    n_in = n_out = n_watchdog = 0
    inbuf = bytearray()
    outbuf = bytearray()

    def emit(inbound):
        nonlocal seq, ts, n_out
        pay = on_frame(inbound)
        if pay is None:
            pay = (b"\xD5" if pt == 8 else b"\xFF") * frame_bytes
        hdr = struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
        if remote[0] and remote[1]:
            rtp.sendto(hdr + pay, remote)
        if capture:
            outbuf.extend(pay)
        seq += 1
        ts += 160
        n_out += 1

    for _ in range(prime):
        emit(None)

    t0 = time.time()
    last_in = t0
    stopped = None
    while time.time() - t0 < seconds:
        try:
            d, _ = rtp.recvfrom(4096)
            if len(d) > 12 and (d[1] & 0x7F) in accept_pts:
                pay = d[12:]
                if capture:
                    inbuf.extend(pay)
                n_in += 1
                last_in = time.time()
                emit(pay)
        except socket.timeout:
            pass
        now = time.time()
        if now - last_in > watchdog:
            emit(None)
            n_watchdog += 1
            last_in = now
        if on_sip is not None and on_sip():
            stopped = "sip"
            break
        if on_stop is not None:
            why = on_stop()
            if why:
                stopped = why if isinstance(why, str) else "stopped"
                break
    return {"in_audio": inbuf, "out_audio": outbuf, "in": n_in, "out": n_out,
            "watchdog": n_watchdog, "dur": time.time() - t0, "stopped": stopped}

def report(st):
    ratio = (st["out"] / st["in"]) if st["in"] else float("nan")
    print("  RTP: in=%d out=%d (out/in=%.3f, %d watchdog) %.1fs in %.1fs wall"
          % (st["in"], st["out"], ratio, st["watchdog"],
             len(st["in_audio"]) / 8000.0, st["dur"]))
