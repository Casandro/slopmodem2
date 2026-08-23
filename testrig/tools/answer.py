"""Register as **620 and ANSWER an inbound call, capturing/analysing the caller's tones."""
import socket, struct, time, math, re, random, sys
import sipmin
import media
from rtpcall import ALAW, ULAW, goertzel, parse_sdp

import sipcfg
HOST, USER, PW = sipcfg.load()

def raw_recv(ua, timeout):
    end = time.time() + timeout
    while time.time() < end:
        ua.sock.settimeout(max(0.2, end - time.time()))
        try:
            d, addr = ua.sock.recvfrom(65535)
        except socket.timeout:
            return None, None
        return d.decode("utf-8", "replace"), addr
    return None, None

def resp_for(req, code, reason, ua, totag, body=""):
    """Build a response echoing the request's dialog headers."""
    keep = []
    for name in ("Via", "From", "Call-ID", "CSeq"):
        for m in re.finditer(r"^(%s:\s*.+?)\s*$" % name, req, re.I | re.M):
            keep.append(m.group(1))
    to = sipmin.hget(req, "To") or "<sip:%s@%s>" % (USER, HOST)
    if ";tag=" not in to:
        to = to + ";tag=" + totag
    lines = ["SIP/2.0 %d %s" % (code, reason)] + keep + ["To: " + to,
             "Contact: <sip:%s@%s:%d>" % (USER, ua.lip, ua.lport),
             "User-Agent: slopmodem-probe"]
    if body:
        lines.append("Content-Type: application/sdp")
    lines.append("Content-Length: %d" % len(body))
    return "\r\n".join(lines) + "\r\n\r\n" + body

def answer(seconds=15.0, wait=45.0, out="rx_in.raw"):
    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s %s   (contact %s:%d)" % (sipmin.status(r) + (ua.lip, ua.lport)))
    if sipmin.status(r)[0] != 200: return
    print("waiting up to %.0fs for an inbound INVITE ..." % wait, flush=True)

    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("0.0.0.0", 0)); rtp.settimeout(0.25)
    rport = rtp.getsockname()[1]
    totag = sipmin.rid(32)

    inv = None
    end = time.time() + wait
    while time.time() < end:
        msg, addr = raw_recv(ua, max(0.5, end - time.time()))
        if not msg: continue
        ua.log.append(("<<", msg))
        if msg.startswith("INVITE "):
            inv = msg
            frm = sipmin.hget(msg, "From") or ""
            print("INVITE received from: %s" % frm.strip())
            break
        if msg.startswith("OPTIONS "):
            ua.send(resp_for(msg, 200, "OK", ua, totag))
    if inv is None:
        print("no inbound INVITE arrived"); return

    rip, rpt, pts = parse_sdp(inv)
    pt = 8 if "8" in pts else (0 if "0" in pts else int(pts[0]) if pts else 8)
    print("  caller RTP %s:%s payloads=%s -> answering with PT %d" % (rip, rpt, pts, pt))
    ua.send(resp_for(inv, 100, "Trying", ua, totag))
    sdp = ("v=0\r\no=- %d 1 IN IP4 %s\r\ns=-\r\nc=IN IP4 %s\r\nt=0 0\r\n"
           % (random.getrandbits(30), ua.lip, ua.lip) +
           "m=audio %d RTP/AVP %d\r\na=rtpmap:%d %s/8000\r\na=sendrecv\r\n"
           % (rport, pt, pt, "PCMA" if pt == 8 else "PCMU"))
    ua.send(resp_for(inv, 200, "OK", ua, totag, sdp))
    print("  sent 200 OK, streaming %.0fs" % seconds, flush=True)

    silence = bytes([0xD5 if pt == 8 else 0xFF]) * 160
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
    st = media.pump(rtp, (rip, rpt), pt, seconds, lambda n: silence, on_sip=poll_sip)
    media.report(st)
    inbuf = st["audio"]
    if inbuf:
        open(out, "wb").write(bytes(inbuf))
        tbl = ALAW if pt == 8 else ULAW
        lin = [tbl[b] for b in inbuf]
        print("  raw saved to %s" % out)
        W = 2000
        print("  %-7s %-9s %-9s %-9s" % ("t(s)", "RMS", "domFreq", "purity"))
        for i in range(0, len(lin) - W + 1, W):
            seg = lin[i:i+W]; ms = sum(v*v for v in seg)/W
            if ms < 200:
                print("  %-7.2f %-9.0f (silence)" % (i/8000.0, math.sqrt(ms))); continue
            best, bf = -1, 0
            for f in range(150, 3401, 25):
                pw = goertzel(seg, f)
                if pw > best: best, bf = pw, f
            for f in [bf+d for d in range(-24, 25, 3)]:
                if f > 0:
                    pw = goertzel(seg, f)
                    if pw > best: best, bf = pw, f
            print("  %-7.2f %-9.0f %-9d %-9.2f" % (i/8000.0, math.sqrt(ms), bf, best/ms))
    ua.sock.settimeout(2.0)
    rtp.close()

if __name__ == "__main__":
    answer(float(sys.argv[1]) if len(sys.argv) > 1 else 15.0)
