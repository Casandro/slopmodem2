"""Answer a call and play a fixed signal at it, decoding nothing.

The point is to take the modems out of the measurement. voicecap.py on the Pi
puts a modem in voice mode (+FCLASS=8) and records what arrives at its analog
port, so pairing it with this gives our transmitter, the whole SIP and FXS path,
and an ADC -- and no demodulator anywhere. Whatever that capture shows is the
channel's doing and cannot be a modem's receiver giving up.

  python3 playraw.py --rate 14400 --level -18 --seconds 25
"""
import argparse, math, socket, sys, time
import g711, rtp, v32, modem
from sip_glue import sipmin, raw_recv, resp_for, HOST, USER, PW


def signal_for(rate, level, seconds):
    """A V.32bis data-phase signal at `rate`, scrambled, trellis coded."""
    nsym = int(seconds * v32.BAUD)
    ts = v32.TRELLIS_SETS.get(rate)
    m = v32.Mod(level_dbfs=level, scrambler_taps=v32.Scrambler.GPA)
    if rate == 4800:
        bits = [1] * (2 * nsym)
        return m.modulate(bits, bps=2)
    bps = ts.nbits - 1
    # 5.4's B1 is continuous scrambled binary ones, which is what a data phase
    # carries when the DTE is idle, and it exercises the full constellation
    # because the scrambler whitens it.
    bits = [1] * (bps * nsym)
    return m.shape(m.symbols(bits, bps=bps, trellis=True, ts=ts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, default=14400)
    ap.add_argument("--level", type=float, default=-18.0)
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--wait", type=float, default=90.0)
    ap.add_argument("--out", default="ref/playraw_tx.raw")
    a = ap.parse_args()

    x = signal_for(a.rate, a.level, a.seconds + 2.0)
    rms = math.sqrt(sum(float(v) * v for v in x) / len(x))
    print("generated %.1f s at %s bit/s, %.1f dBFS rms"
          % (len(x) / 8000.0, a.rate, 20 * math.log10(max(rms, 1) / 32768.0)),
          flush=True)

    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s" % (sipmin.status(r)[0] if r else None), flush=True)
    rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rs.bind(("0.0.0.0", 0))
    rs.settimeout(0.02)
    totag = sipmin.rid(32)

    print("waiting up to %.0fs for an INVITE ..." % a.wait, flush=True)
    inv = None
    end = time.time() + a.wait
    while time.time() < end:
        msg, _ = raw_recv(ua, max(0.5, end - time.time()))
        if msg and msg.startswith("INVITE "):
            inv = msg
            break
        if msg and msg.startswith("OPTIONS "):
            ua.send(resp_for(msg, 200, "OK", ua, totag))
    if inv is None:
        print("no INVITE")
        return 1
    rip, rpt, pts = modem.parse_sdp(inv)
    pt = 8 if "8" in pts else 0
    ua.send(resp_for(inv, 100, "Trying", ua, totag))
    ua.send(resp_for(inv, 200, "OK", ua, totag,
                     modem.sdp_for(ua.lip, rs.getsockname()[1], pt)))
    print("answered; caller RTP %s:%s PT %d" % (rip, rpt, pt), flush=True)

    pos = [0]
    sent = []

    def on_frame(_inbound):
        i = pos[0]
        blk = x[i:i + 160]
        pos[0] = i + 160
        if len(blk) < 160:
            blk = list(blk) + [0] * (160 - len(blk))
        sent.extend(blk)
        return g711.encode(blk, pt)

    def on_sip():
        ua.sock.settimeout(0.001)
        try:
            t = ua.sock.recvfrom(65535)[0].decode("utf-8", "replace")
            if t.startswith("BYE "):
                ua.send(resp_for(t, 200, "OK", ua, totag))
                print("caller sent BYE", flush=True)
                return True
        except Exception:
            pass
        return False

    st = rtp.pump(rs, (rip, rpt), pt, a.seconds, on_frame, on_sip=on_sip,
                  frame_bytes=160, watchdog=1e9)
    rtp.report(st)
    # What we put on the wire, as linear, so the capture has a reference to be
    # compared against rather than only a spectrum to be admired.
    open(a.out, "wb").write(bytes(g711.encode(sent, pt)))
    print("played %d samples -> %s" % (len(sent), a.out), flush=True)
    rs.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
