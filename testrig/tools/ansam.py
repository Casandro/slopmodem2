"""Answer an inbound call as **620 and emit a real V.8 ANSam answer tone,
then report what the calling modem sends back."""
import socket, struct, time, math, re, random, sys
import sipmin
import media
from rtpcall import ALAW, ULAW, goertzel, parse_sdp
from answer import raw_recv, resp_for, HOST, USER, PW

# ---- A-law encoder (G.711) ----
SEG_AEND = [0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF]
def lin2alaw(pcm):
    pcm >>= 3
    if pcm >= 0: mask = 0xD5
    else:        mask = 0x55; pcm = -pcm - 1
    seg = 8
    for i, e in enumerate(SEG_AEND):
        if pcm <= e: seg = i; break
    if seg >= 8: return 0x7F ^ mask
    a = seg << 4
    a |= (pcm >> 1) & 0xF if seg < 2 else (pcm >> seg) & 0xF
    return a ^ mask
ENC = None

def make_ansam(seconds=6.0, sr=8000, amp=8000, f=2100.0, rev=0.450, am=15.0, depth=0.20):
    """V.8 ANSam: 2100 Hz, AM at 15 Hz, phase reversal every 450 ms."""
    n = int(seconds * sr)
    out = bytearray()
    ph = 0.0
    dph = 2 * math.pi * f / sr
    sign = 1.0
    for i in range(n):
        if i and i % int(rev * sr) == 0:
            sign = -sign                       # phase reversal
        env = 1.0 - depth + depth * math.sin(2 * math.pi * am * i / sr)
        v = int(amp * env * sign * math.sin(ph))
        ph += dph
        out.append(lin2alaw(max(-32768, min(32767, v))))
    return bytes(out)

def run(seconds=20.0, wait=45.0, tone_len=6.0, out="rx_ansam.raw"):
    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s %s  (contact %s:%d)" % (sipmin.status(r) + (ua.lip, ua.lport)))
    if sipmin.status(r)[0] != 200: return
    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("0.0.0.0", 0)); rtp.settimeout(0.25)
    rport = rtp.getsockname()[1]
    totag = sipmin.rid(32)
    print("waiting up to %.0fs for an inbound INVITE ..." % wait, flush=True)
    inv = None; end = time.time() + wait
    while time.time() < end:
        msg, _ = raw_recv(ua, max(0.5, end - time.time()))
        if not msg: continue
        if msg.startswith("INVITE "):
            inv = msg; print("INVITE from: %s" % (sipmin.hget(msg, "From") or "").strip()); break
        if msg.startswith("OPTIONS "): ua.send(resp_for(msg, 200, "OK", ua, totag))
    if inv is None: print("no INVITE"); return
    rip, rpt, pts = parse_sdp(inv)
    pt = 8 if "8" in pts else 0
    print("  caller RTP %s:%s -> PT %d (%s)" % (rip, rpt, pt, "PCMA" if pt == 8 else "PCMU"))
    ua.send(resp_for(inv, 100, "Trying", ua, totag))
    sdp = ("v=0\r\no=- %d 1 IN IP4 %s\r\ns=-\r\nc=IN IP4 %s\r\nt=0 0\r\n"
           % (random.getrandbits(30), ua.lip, ua.lip) +
           "m=audio %d RTP/AVP %d\r\na=rtpmap:%d %s/8000\r\na=sendrecv\r\n"
           % (rport, pt, pt, "PCMA" if pt == 8 else "PCMU"))
    ua.send(resp_for(inv, 200, "OK", ua, totag, sdp))
    print("  200 OK sent; emitting %.1fs ANSam (2100 Hz, 15 Hz AM, 450 ms phase reversals)"
          % tone_len, flush=True)

    tone = make_ansam(tone_len)
    silence = bytes([0xD5 if pt == 8 else 0xFF]) * 160
    tone = make_ansam(tone_len)
    silence = bytes([0xD5 if pt == 8 else 0xFF]) * 160
    def gen(n):
        off = n * 160
        return tone[off:off+160] if off + 160 <= len(tone) else silence
    def poll_sip():
        ua.sock.settimeout(0.001)
        try:
            m2, _ = ua.sock.recvfrom(65535)
            t = m2.decode("utf-8", "replace")
            if t.startswith("BYE "):
                ua.send(resp_for(t, 200, "OK", ua, totag)); print("  caller sent BYE")
                return True
        except Exception:
            pass
        return False
    st = media.pump(rtp, (rip, rpt), pt, seconds, gen, on_sip=poll_sip)
    media.report(st)
    inbuf = st["audio"]
    if inbuf:
        open(out, "wb").write(bytes(inbuf))
        tbl = ALAW if pt == 8 else ULAW
        lin = [tbl[b] for b in inbuf]
        W = 1000   # 125 ms slices - finer, to catch short V.21 bursts
        print("  raw -> %s ; 125ms slices (only non-silent shown):" % out)
        print("  %-7s %-8s %-8s %-7s" % ("t(s)", "RMS", "domFreq", "purity"))
        shown = 0
        for i in range(0, len(lin) - W + 1, W):
            seg = lin[i:i+W]; ms = sum(v*v for v in seg)/W
            if ms < 400: continue
            best, bf = -1, 0
            for f in range(150, 3401, 25):
                pw = goertzel(seg, f)
                if pw > best: best, bf = pw, f
            for f in [bf+d for d in range(-24, 25, 3)]:
                if f > 0:
                    pw = goertzel(seg, f)
                    if pw > best: best, bf = pw, f
            print("  %-7.3f %-8.0f %-8d %-7.2f" % (i/8000.0, math.sqrt(ms), bf, best/ms))
            shown += 1
        if not shown: print("  (calling modem stayed silent throughout)")
    rtp.close()

if __name__ == "__main__":
    run(float(sys.argv[1]) if len(sys.argv) > 1 else 20.0)
