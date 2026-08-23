"""Originate a call to a hardware modem and run the V.8 call-DCE negotiation."""
import argparse, math, random, re, socket, sys, time
import g711, dsp, fsm, rtp, v8, probes
from sip_glue import sipmin, resp_for, HOST, USER, PW

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("number")
    ap.add_argument("--modes", default="V.21",
                    help="comma-separated modulation modes to advertise in CM")
    ap.add_argument("--level", type=float, default=-30.0, help="V.21 TX level dBFS")
    ap.add_argument("--te", type=float, default=1.0, help="Te silence before CM (V.8 8.1.1)")
    ap.add_argument("--cm-max", type=float, default=6.0, help="give up on JM after this long")
    ap.add_argument("--seconds", type=float, default=22.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--codec", default="8,0",
                    help="payload types to offer, in preference order (8=PCMA, 0=PCMU)")
    ap.add_argument("--probe", default=None,
                    help="instead of running the V.8 FSM, transmit this probe signal "
                         "(see probes.py) so the audio path can be characterised")
    ap.add_argument("--vbd", action="store_true", help="add ITU-T V.152 voice-band-data signalling to our SDP "
                         "(a=gpmd:<pt> vbd=yes;ecan=off plus a=silenceSupp:off), which asks the "
                         "gateway to treat the stream as data and stop voice-optimised processing")
    a = ap.parse_args()
    modes = set(m.strip() for m in a.modes.split(",") if m.strip())

    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    print("REGISTER -> %s" % (sipmin.status(r)[0] if r else None), flush=True)

    rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rs.bind(("0.0.0.0", 0)); rs.settimeout(0.25)
    rport = rs.getsockname()[1]
    vbd = ("a=gpmd:8 vbd=yes;ecan=off\r\na=gpmd:0 vbd=yes;ecan=off\r\n"
           "a=silenceSupp:off - - - -\r\n") if a.vbd else ""
    offer_pts = [int(v) for v in a.codec.split(",") if v.strip()]
    names = {8: "PCMA", 0: "PCMU", 9: "G722", 18: "G729", 2: "G726-32"}
    rtpmaps = "".join("a=rtpmap:%d %s/8000\r\n" % (q, names.get(q, "PCMA")) for q in offer_pts)
    sdp = ("v=0\r\no=- %d 1 IN IP4 %s\r\ns=-\r\nc=IN IP4 %s\r\nt=0 0\r\n"
           % (random.getrandbits(30), ua.lip, ua.lip) +
           "m=audio %d RTP/AVP %s\r\n" % (rport, " ".join(str(q) for q in offer_pts)) +
           rtpmaps + vbd + "a=sendrecv\r\n")
    ruri = "sip:%s@%s" % (a.number, HOST)
    print("INVITE %s  (advertising %s)" % (ruri, sorted(modes)), flush=True)
    rsp, cid, ftag = ua.authed("INVITE", ruri, body=sdp)
    if rsp is None or sipmin.status(rsp)[0] != 200:
        print("  -> %s" % (sipmin.status(rsp)[0] if rsp else "no response"))
        if rsp:
            for ln in rsp.split("\r\n"):
                if ln[:1].isupper() and ":" in ln and ln.split(":")[0] in (
                        "Warning", "Reason", "Retry-After", "Server", "User-Agent", "Contact"):
                    print("     %s" % ln)
        return 1
    print("  -> 200 OK", flush=True)
    totag = re.search(r";tag=([^\s;]+)", sipmin.hget(rsp, "To") or "")
    totag = totag.group(1) if totag else None
    cm = re.search(r"<([^>]+)>", sipmin.hget(rsp, "Contact") or "")
    target = cm.group(1) if cm else ruri
    body = rsp.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in rsp else ""
    ip = re.search(r"c=IN IP4 ([\d.]+)", body)
    ma = re.search(r"m=audio (\d+) RTP/AVP ([\d ]+)", body)
    rip = ip.group(1) if ip else None
    rpt = int(ma.group(1)) if ma else None
    pts = ma.group(2).split() if ma else []
    pt = 8 if "8" in pts else (0 if "0" in pts else int(pts[0]))
    print("  remote RTP %s:%s PT %d" % (rip, rpt, pt), flush=True)
    print("  --- SDP answer from far end ---", flush=True)
    for ln in body.replace("\r", "").split("\n"):
        if ln.strip():
            print("    %s" % ln, flush=True)
    print("  ------------------------------", flush=True)
    ua.req("ACK", target, callid=cid, fromtag=ftag, totag=totag, cseq=ua.cseq)

    machine = fsm.Originate(modes, level_dbfs=a.level, te=a.te, cm_max_s=a.cm_max)
    seen = [0]

    if a.probe:
        stream = g711.encode([0] * 4000 +
                             probes.SIGNALS[a.probe](a.seconds, level_dbfs=a.level), pt)
        pos = [0]
        sil = bytes([g711.ALAW_SILENCE if pt == 8 else g711.ULAW_SILENCE]) * 160
        def on_frame(_inbound):
            i = pos[0]; pos[0] = i + 160
            c = stream[i:i + 160]
            return c if len(c) == 160 else sil
    else:
        def on_frame(inbound):
            samples = g711.decode(inbound, pt) if inbound else []
            out = machine.step(samples)
            while len(machine.events) > seen[0]:
                t, st, msg = machine.events[seen[0]]
                print("  [%6.3f] %-8s %s" % (t, st, msg), flush=True)
                seen[0] += 1
            return g711.encode(out, pt)

    st = rtp.pump(rs, (rip, rpt), pt, a.seconds, on_frame)
    rtp.report(st)
    if a.out:
        open(a.out, "wb").write(bytes(st["in_audio"]))
        print("  inbound audio -> %s" % a.out)
    if a.probe:
        print("  probe %s transmitted" % a.probe)
    print("  final state: %s ; CM sequences sent: %d" % (machine.state, machine.cm_sequences_sent))
    if machine.agreed:
        p = machine.agreed
        print("  *** V.8 NEGOTIATION: JM octets %s ***" % " ".join(p["raw"]))
        print("      call function : %s" % p["call_function"])
        print("      common modes  : %s" % (p["modulations"] or "NONE (8.2.3 all-zero JM)"))
    else:
        print("  no JM received")
    b, _, _ = ua.authed("BYE", target, callid=cid, fromtag=ftag, totag=totag)
    print("  BYE -> %s" % (sipmin.status(b)[0] if b else "no reply"))
    rs.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
