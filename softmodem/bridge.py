"""A slop bridge: relay a call between the two hardware modems and record it.

Everything so far has been our software talking to a modem, or to itself. This is
neither: it answers a call from one modem, places a call to the other, and
forwards the audio between them without touching it. The modems negotiate with
each other, and we get both directions of a *real* handshake on tape.

That is worth having for its own sake, and it is the instrument this project has
been missing. Reading a modulation out of a Recommendation whose figures are
figures and whose tables have lost their signs to OCR is guesswork checked
against structure; watching two modems that already implement it is measurement.
It is how Figure 2/V.22bis was settled earlier, and it is the only route left to
Figure 3/V.32's 32-point trellis constellation, whose coordinates are the part of
the scan that is beyond repair.

No transcoding: both legs are PCMA, so payloads are forwarded byte for byte.

  python3 bridge.py '**2' --seconds 70 --out-a ref/br_a.raw --out-b ref/br_b.raw
      (then have **1 dial **620)
"""
import argparse, random, re, socket, struct, sys, time
import g711, dsp
from sip_glue import sipmin, raw_recv, resp_for, HOST, USER, PW
import modem


def rtp_send(sock, remote, pt, ssrc, seq, ts, payload):
    hdr = struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
    if remote[0] and remote[1]:
        sock.sendto(hdr + payload, remote)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("number", help="number to call for the second leg")
    ap.add_argument("--seconds", type=float, default=70.0)
    ap.add_argument("--wait", type=float, default=90.0)
    ap.add_argument("--out-a", default="ref/bridge_a.raw",
                    help="audio received from the answered leg (the caller)")
    ap.add_argument("--out-b", default="ref/bridge_b.raw",
                    help="audio received from the originated leg")
    a = ap.parse_args()

    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s" % (sipmin.status(r)[0] if r else None), flush=True)

    sa = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sa.bind(("0.0.0.0", 0))
    sa.settimeout(0.02)
    sb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sb.bind(("0.0.0.0", 0))
    sb.settimeout(0.02)
    totag = sipmin.rid(32)

    # ---- leg A: wait to be called -------------------------------------
    print("waiting up to %.0fs for INVITE (have one modem dial **620) ..."
          % a.wait, flush=True)
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
    aip, apt, apts = modem.parse_sdp(inv)
    pt = 8 if "8" in apts else 0
    ua.send(resp_for(inv, 100, "Trying", ua, totag))
    ua.send(resp_for(inv, 200, "OK", ua, totag,
                     modem.sdp_for(ua.lip, sa.getsockname()[1], pt)))
    a_cid = (sipmin.hget(inv, "Call-ID") or "").strip()
    a_ct = re.search(r";tag=([^\s;]+)", sipmin.hget(inv, "From") or "")
    a_cm = re.search(r"<([^>]+)>", sipmin.hget(inv, "Contact") or "")
    a_target = a_cm.group(1) if a_cm else "sip:%s" % HOST
    print("leg A answered: %s:%s PT %d" % (aip, apt, pt), flush=True)

    # ---- leg B: place the second call ---------------------------------
    ruri = "sip:%s@%s" % (a.number, HOST)
    print("leg B: INVITE %s" % ruri, flush=True)
    rsp, b_cid, b_ftag = ua.authed("INVITE", ruri, timeout=40.0,
                                   body=modem.sdp_for(ua.lip,
                                                      sb.getsockname()[1], pt))
    code = sipmin.status(rsp)[0] if rsp else None
    print("  -> %s" % code, flush=True)
    if code != 200:
        ua.authed("BYE", a_target, callid=a_cid, fromtag=totag,
                  totag=a_ct.group(1) if a_ct else None)
        return 1
    b_totag = re.search(r";tag=([^\s;]+)", sipmin.hget(rsp, "To") or "")
    b_totag = b_totag.group(1) if b_totag else None
    b_cm = re.search(r"<([^>]+)>", sipmin.hget(rsp, "Contact") or "")
    b_target = b_cm.group(1) if b_cm else ruri
    bip, bpt, bpts = modem.parse_sdp(rsp)
    print("leg B answered: %s:%s" % (bip, bpt), flush=True)
    ua.req("ACK", b_target, callid=b_cid, fromtag=b_ftag, totag=b_totag,
           cseq=ua.cseq)

    # ---- relay --------------------------------------------------------
    # Receive-driven in both directions: a frame that arrives on one leg is
    # forwarded on the other. Nothing is generated locally, so neither modem is
    # paced by us; they pace each other, which is the point.
    ssrc_a, ssrc_b = random.getrandbits(32), random.getrandbits(32)
    seq_a = seq_b = random.getrandbits(15)
    ts_a = ts_b = 0
    buf_a, buf_b = bytearray(), bytearray()
    n_a = n_b = 0
    silence = (b"\xD5" if pt == 8 else b"\xFF") * 160
    # prime both legs so the far ends see a stream at once
    for _ in range(2):
        rtp_send(sa, (aip, apt), pt, ssrc_a, seq_a, ts_a, silence)
        seq_a += 1
        ts_a += 160
        rtp_send(sb, (bip, bpt), pt, ssrc_b, seq_b, ts_b, silence)
        seq_b += 1
        ts_b += 160
    t0 = time.time()
    stopped = None
    while time.time() - t0 < a.seconds:
        for src, dst, dstaddr in ((sa, sb, (bip, bpt)), (sb, sa, (aip, apt))):
            try:
                d, _ = src.recvfrom(4096)
            except socket.timeout:
                continue
            if len(d) <= 12 or (d[1] & 0x7F) not in (0, 8, pt):
                continue
            pay = d[12:]
            if src is sa:
                buf_a.extend(pay)
                n_a += 1
                rtp_send(sb, (bip, bpt), pt, ssrc_b, seq_b, ts_b, pay)
                seq_b += 1
                ts_b += 160
            else:
                buf_b.extend(pay)
                n_b += 1
                rtp_send(sa, (aip, apt), pt, ssrc_a, seq_a, ts_a, pay)
                seq_a += 1
                ts_a += 160
        ua.sock.settimeout(0.001)
        try:
            t = ua.sock.recvfrom(65535)[0].decode("utf-8", "replace")
            if t.startswith("BYE "):
                ua.send(resp_for(t, 200, "OK", ua, totag))
                print("  a leg sent BYE", flush=True)
                stopped = "bye"
                break
        except Exception:
            pass
    print("relayed %.1f s: %d frames from A, %d from B (%s)"
          % (time.time() - t0, n_a, n_b, stopped or "time"), flush=True)

    open(a.out_a, "wb").write(bytes(buf_a))
    open(a.out_b, "wb").write(bytes(buf_b))
    for nm, path, buf in (("A", a.out_a, buf_a), ("B", a.out_b, buf_b)):
        lin = g711.decode(buf, pt)
        rms = dsp.mean_square(lin) ** 0.5 if lin else 0.0
        import math
        print("  leg %s -> %s  (%d samples, %.1f s, %.1f dBFS)"
              % (nm, path, len(lin), len(lin) / 8000.0,
                 20 * math.log10(max(rms, 1) / 32768.0)), flush=True)

    if stopped != "bye":
        for tgt, cid, ft, tt in ((a_target, a_cid, totag,
                                  a_ct.group(1) if a_ct else None),
                                 (b_target, b_cid, b_ftag, b_totag)):
            b, _, _ = ua.authed("BYE", tgt, callid=cid, fromtag=ft, totag=tt)
            print("  BYE -> %s" % (sipmin.status(b)[0] if b else "no reply"),
                  flush=True)
    sa.close()
    sb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
