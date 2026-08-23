"""Measure this rig's near-end echo at V.32's carrier, by cross-correlation.

V.32 is the first modulation here that puts both directions in the same band:
2400 baud on an 1800 Hz carrier, no frequency-division split. On a two-wire
circuit that is why a V.32 modem needs an echo canceller -- its own transmitter
lands on top of the far signal.

Whether *this* rig needs one is a measurement, not an assumption. We are not on a
two-wire circuit: RTP carries the two directions as separate streams and the
FRITZ!Box does the hybrid, so the question is how much of what we send comes back
to us.

Cross-correlation is the right tool because the far end's signal is uncorrelated
with ours: correlate the received stream against our own transmitted stream over
a range of delays, and the peak is the echo path. Reported as a correlation
coefficient and as the implied echo return loss.

  python3 echo_probe.py --seconds 20 --level -24
"""
import argparse, math, random, socket, sys, time
import dsp, g711, rtp, v32
from sip_glue import sipmin, raw_recv, resp_for, HOST, USER, PW
import modem


def correlate(rx, tx, max_lag=2400, step=1):
    """Peak normalised cross-correlation of rx against tx over 0..max_lag."""
    n = min(len(rx), len(tx)) - max_lag
    if n < 8000:
        return None
    ex = math.sqrt(sum(v * v for v in tx[:n]) or 1.0)
    best = []
    for lag in range(0, max_lag + 1, step):
        acc = 0.0
        for i in range(0, n, 4):            # decimate: the echo is broadband
            acc += rx[i + lag] * tx[i]
        seg = rx[lag:lag + n:4]
        ey = math.sqrt(sum(v * v for v in seg) or 1.0)
        exx = math.sqrt(sum(v * v for v in tx[0:n:4]) or 1.0)
        best.append((abs(acc) / (ey * exx), lag))
    best.sort(reverse=True)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--wait", type=float, default=60.0)
    ap.add_argument("--level", type=float, default=-24.0)
    ap.add_argument("--out", default="ref/echo_rx.raw")
    ap.add_argument("--tx-out", default="ref/echo_tx.raw")
    a = ap.parse_args()

    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s" % (sipmin.status(r)[0] if r else None), flush=True)
    rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rs.bind(("0.0.0.0", 0))
    rs.settimeout(0.25)
    totag = sipmin.rid(32)
    print("waiting up to %.0fs for INVITE ..." % a.wait, flush=True)
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

    # A V.32 data signal: random bits at 9600, so the echo measurement is made
    # with the very waveform the receiver will have to work with.
    random.seed(20250822)
    m = v32.Mod(level_dbfs=a.level)
    txbuf = []

    def on_frame(inbound):
        out = m.modulate([random.randint(0, 1) for _ in range(192)], bps=4)
        while len(out) < 160:
            out.append(0)
        txbuf.extend(out[:160])
        return g711.encode(out[:160], pt)

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
    rx = g711.decode(st["in_audio"], pt)
    tx = txbuf
    open(a.out, "wb").write(bytes(st["in_audio"]))
    open(a.tx_out, "wb").write(bytes(st["out_audio"]))
    print("  rx -> %s (%d samples), tx -> %s (%d samples)"
          % (a.out, len(rx), a.tx_out, len(tx)), flush=True)

    lt = 10 * math.log10(dsp.mean_square(tx) / 32768.0 ** 2 + 1e-30)
    lr = 10 * math.log10(dsp.mean_square(rx) / 32768.0 ** 2 + 1e-30)
    print("  transmitted %.1f dBFS, received %.1f dBFS" % (lt, lr))
    top = correlate(rx, tx)
    if not top:
        print("  not enough audio to correlate")
        return 1
    print("  top cross-correlations (received vs our own transmission):")
    for c, lag in top[:5]:
        print("     lag %4d samples (%5.1f ms)  coefficient %.4f%s"
              % (lag, lag / 8.0, c,
                 "   -> echo return %.1f dB" % (-20 * math.log10(c))
                 if c > 1e-6 else ""))
    c0 = top[0][0]
    print()
    print("  echo return loss at the peak: %.1f dB" % (-20 * math.log10(max(c0, 1e-9))))
    print("  V.32 needs the far signal to survive this. Its 16-point "
          "constellation has")
    print("  a decision distance of 1 against a mean power of 10, so it wants "
          "an SNR")
    print("  of roughly 20 dB; echo below about -25 dB is a nuisance rather "
          "than a wall.")
    rs.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
