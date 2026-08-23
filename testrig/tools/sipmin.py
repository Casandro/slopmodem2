"""Minimal SIP UAC: REGISTER / INVITE / ACK / BYE with MD5 digest auth."""
import socket, random, hashlib, re, time

def h(x): return hashlib.md5(x.encode()).hexdigest()
def rid(n=32): return "%0*x" % (n // 4, random.getrandbits(n))

def digest(user, pw, method, uri, chal):
    realm = chal.get("realm", "")
    nonce = chal.get("nonce", "")
    qop   = chal.get("qop")
    ha1 = h("%s:%s:%s" % (user, realm, pw))
    ha2 = h("%s:%s" % (method, uri))
    p = ['username="%s"' % user, 'realm="%s"' % realm, 'nonce="%s"' % nonce, 'uri="%s"' % uri]
    if qop and "auth" in qop:
        nc, cn = "00000001", rid(32)
        resp = h("%s:%s:%s:%s:%s:%s" % (ha1, nonce, nc, cn, "auth", ha2))
        p += ['response="%s"' % resp, "qop=auth", "nc=" + nc, 'cnonce="%s"' % cn]
    else:
        p += ['response="%s"' % h("%s:%s:%s" % (ha1, nonce, ha2))]
    if chal.get("opaque"): p.append('opaque="%s"' % chal["opaque"])
    if chal.get("algorithm"): p.append("algorithm=" + chal["algorithm"])
    return "Digest " + ", ".join(p)

def parse_chal(msg):
    m = re.search(r"^(?:WWW-Authenticate|Proxy-Authenticate):\s*Digest\s*(.+?)$", msg, re.I | re.M)
    if not m: return None, None
    hdr = "Proxy" if "proxy-authenticate" in m.group(0).lower() else "WWW"
    d = dict(re.findall(r'(\w+)\s*=\s*"?([^",]+)"?', m.group(1)))
    return d, hdr

def status(msg):
    m = re.match(r"SIP/2\.0\s+(\d+)\s*(.*)", msg)
    return (int(m.group(1)), m.group(2).strip()) if m else (None, None)

def hget(msg, name):
    m = re.search(r"^%s:\s*(.+?)\s*$" % re.escape(name), msg, re.I | re.M)
    return m.group(1) if m else None

class UA:
    def __init__(self, host, user, pw, port=5060, lport=0):
        self.host, self.user, self.pw, self.port = host, user, pw, port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5.0)
        self.sock.bind(("0.0.0.0", lport))
        t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); t.connect((host, port))
        self.lip = t.getsockname()[0]; t.close()
        self.lport = self.sock.getsockname()[1]
        self.cseq = 0
        self.prov = None
        self.log = []

    def send(self, data):
        self.sock.sendto(data.encode(), (self.host, self.port))

    def recv(self, timeout=5.0, cseq=None, method=None):
        """Return the first final response, optionally matching CSeq/method.

        Matching on CSeq is essential: the FRITZ!Box retransmits 401 responses,
        so an unmatched read can return the challenge to the *previous* request
        instead of the answer to the one just sent.
        """
        end = time.time() + timeout
        while time.time() < end:
            self.sock.settimeout(max(0.2, end - time.time()))
            try:
                d, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                return None
            msg = d.decode("utf-8", "replace")
            self.log.append(("<<", msg))
            code, _ = status(msg)
            if code is None:
                continue                       # a request from the peer
            cs = hget(msg, "CSeq") or ""
            m = re.match(r"\s*(\d+)\s+(\w+)", cs)
            if m:
                if cseq is not None and int(m.group(1)) != cseq:
                    continue                   # stale / retransmitted
                if method is not None and m.group(2).upper() != method.upper():
                    continue
            if 100 <= code < 200:
                self.prov = code               # 180 Ringing etc.
                continue
            return msg
        return None

    def req(self, method, ruri, extra=(), body="", auth=None, callid=None,
            fromtag=None, totag=None, cseq=None, contact=True):
        self.cseq = cseq if cseq is not None else self.cseq + 1
        callid = callid or (rid(64) + "@" + self.lip)
        fromtag = fromtag or rid(32)
        to = "<%s>" % ruri + (";tag=" + totag if totag else "")
        lines = ["%s %s SIP/2.0" % (method, ruri),
                 "Via: SIP/2.0/UDP %s:%d;branch=z9hG4bK%s;rport" % (self.lip, self.lport, rid(32)),
                 "Max-Forwards: 70",
                 "From: <sip:%s@%s>;tag=%s" % (self.user, self.host, fromtag),
                 "To: " + to,
                 "Call-ID: " + callid,
                 "CSeq: %d %s" % (self.cseq, method)]
        if contact:
            lines.append("Contact: <sip:%s@%s:%d>" % (self.user, self.lip, self.lport))
        lines.append("User-Agent: slopmodem-probe")
        if auth: lines.append(auth)
        lines += list(extra)
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append("Content-Length: %d" % len(body))
        msg = "\r\n".join(lines) + "\r\n\r\n" + body
        self.log.append((">>", msg))
        self.send(msg)
        return callid, fromtag

    def authed(self, method, ruri, timeout=8.0, retry_timeout=25.0, **kw):
        """Send request; on 401/407 retry once with digest. Matches by CSeq."""
        callid, ftag = self.req(method, ruri, **kw)
        rsp = self.recv(timeout, cseq=self.cseq, method=method)
        if rsp is None: return None, callid, ftag
        code, _ = status(rsp)
        if code in (401, 407):
            chal, which = parse_chal(rsp)
            if not chal: return rsp, callid, ftag
            a = digest(self.user, self.pw, method, ruri, chal)
            hdr = ("Proxy-Authorization: " if which == "Proxy" else "Authorization: ") + a
            kw2 = dict(kw); kw2["auth"] = hdr
            kw2["callid"] = callid; kw2["fromtag"] = ftag
            self.req(method, ruri, **kw2)
            rsp = self.recv(retry_timeout, cseq=self.cseq, method=method)
        return rsp, callid, ftag
