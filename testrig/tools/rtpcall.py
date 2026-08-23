"""Register as **620 on the FRITZ!Box, call a number, capture RTP, analyse tones."""
import socket, struct, time, math, re, sys, random
import sipmin
import media

import sipcfg
HOST, USER, PW = sipcfg.load()

# ---- G.711 decode ----
def alaw2lin(a):
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0: t += 8
    elif seg == 1: t += 0x108
    else: t = (t + 0x108) << (seg - 1)
    return t if (a & 0x80) else -t
def ulaw2lin(u):
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if (u & 0x80) else (t - 0x84)
ALAW = [alaw2lin(i) for i in range(256)]
ULAW = [ulaw2lin(i) for i in range(256)]

def goertzel(x, f, sr=8000):
    """Mean power of the component at f, directly comparable to mean square."""
    n = len(x)
    if n == 0: return 0.0
    w = 2 * math.pi * f / sr
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + c * s1 - s2
        s2, s1 = s1, s0
    p = s1*s1 + s2*s2 - c*s1*s2      # ~ (A*n/2)^2 for amplitude A
    amp2 = 4.0 * max(p, 0.0) / (n * n)   # = A^2
    return amp2 / 2.0                    # mean power of that sinusoid

def parse_sdp(msg):
    body = msg.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in msg else ""
    ip = re.search(r"c=IN IP4 ([\d.]+)", body)
    m  = re.search(r"m=audio (\d+) RTP/AVP ([\d ]+)", body)
    return (ip.group(1) if ip else None,
            int(m.group(1)) if m else None,
            m.group(2).split() if m else [])

def call(number, seconds=12.0, send_pt=8, wav_out="rx.raw"):
    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s %s" % sipmin.status(r))
    if sipmin.status(r)[0] != 200: return

    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("0.0.0.0", 0)); rtp.settimeout(0.25)
    rport = rtp.getsockname()[1]
    sdp = ("v=0\r\n"
           "o=- %d 1 IN IP4 %s\r\n" % (random.getrandbits(30), ua.lip) +
           "s=-\r\nc=IN IP4 %s\r\nt=0 0\r\n" % ua.lip +
           "m=audio %d RTP/AVP 8 0 101\r\n" % rport +
           "a=rtpmap:8 PCMA/8000\r\na=rtpmap:0 PCMU/8000\r\n"
           "a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-15\r\na=sendrecv\r\n")
    ruri = "sip:%s@%s" % (number, HOST)
    print("INVITE %s" % ruri)
    rsp, cid, ftag = ua.authed("INVITE", ruri, body=sdp)
    if rsp is None:
        print("  no final response"); return
    code, rea = sipmin.status(rsp)
    print("  -> %s %s" % (code, rea))
    if code != 200:
        return
    totag = re.search(r";tag=([^\s;]+)", sipmin.hget(rsp, "To") or "")
    totag = totag.group(1) if totag else None
    contact = sipmin.hget(rsp, "Contact") or ""
    cm = re.search(r"<([^>]+)>", contact)
    target = cm.group(1) if cm else ruri
    rip, rpt, pts = parse_sdp(rsp)
    print("  remote RTP %s:%s  payloads=%s" % (rip, rpt, pts))
    pt = 8 if "8" in pts else (0 if "0" in pts else int(pts[0]))
    # ACK
    ua.req("ACK", target, callid=cid, fromtag=ftag, totag=totag, cseq=ua.cseq)

    # ---- RTP media loop: receive-driven, one frame out per frame in ----
    silence = bytes([0xD5 if pt == 8 else 0xFF]) * 160
    st = media.pump(rtp, (rip, rpt), pt, seconds, lambda n: silence)
    media.report(st)
    inbuf = st["audio"]

    # ---- analyse ----
    tbl = ALAW if pt == 8 else ULAW
    if inbuf:
        open(wav_out, "wb").write(bytes(inbuf))
        lin = [tbl[b] for b in inbuf]
        rms = math.sqrt(sum(v*v for v in lin) / len(lin))
        print("  audio RMS = %.0f (%.1f dBFS), raw saved to %s"
              % (rms, 20*math.log10(max(rms,1)/32768.0), wav_out))
        # dominant frequency per 0.25 s slice, coarse scan then refine
        W = 2000
        print("  %-7s %-9s %-9s %-9s" % ("t(s)", "RMS", "domFreq", "purity"))
        prev = None
        for i in range(0, len(lin) - W + 1, W):
            seg = lin[i:i+W]
            ms = sum(v*v for v in seg) / W
            if ms < 200:
                lab = "(silence)"
                print("  %-7.2f %-9.0f %-9s" % (i/8000.0, math.sqrt(ms), lab)); prev = None
                continue
            best, bf = -1, 0
            for f in range(150, 3401, 25):
                pw = goertzel(seg, f)
                if pw > best: best, bf = pw, f
            for f in [bf + d for d in range(-24, 25, 3)]:
                if f <= 0: continue
                pw = goertzel(seg, f)
                if pw > best: best, bf = pw, f
            print("  %-7.2f %-9.0f %-9d %-9.2f" % (i/8000.0, math.sqrt(ms), bf, best/ms))
    # ---- BYE (must be authenticated: the FRITZ!Box challenges in-dialog requests) ----
    ua.cseq += 0
    b, _, _ = ua.authed("BYE", target, callid=cid, fromtag=ftag, totag=totag)
    print("  BYE -> %s" % (sipmin.status(b)[0] if b else "no reply"))
    rtp.close()

if __name__ == "__main__":
    call(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 12.0)
