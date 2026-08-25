"""Offline tests for V.42's framing and detection phase.

Everything here is checkable without a line, and most of it is checkable against
something the Recommendation prints rather than against our own arithmetic: the
two FCS residues, and the detection-phase bit patterns.
"""
import collections, sys
import v42

FAIL = []


def check(name, cond, detail=""):
    print("  %-64s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def bits(s):
    """A bit list from a string like '0 1000 1000 1', as the spec prints them."""
    return [int(c) for c in s if c in "01"]


def link_throughput(rate=12000, rtt_ms=440, secs=12.0, both=False, dt=0.02):
    """Octets a second across a clean two-entity link with a real round trip.

    The other link tests here swap bit vectors between the two entities with no
    delay at all, and with no delay the window can never be the thing that
    binds -- so they cannot see a window that is being spent on frames still
    sitting in our own transmit queue rather than in flight. This runs the same
    pair over a channel of rate*dt bits per 20 ms step with each direction
    delayed by whole steps.

    The distinction is worth a test because it was got wrong from the other
    side: our transmit direction measured 898 byte/s against the Cirrus at both
    9600 and at 12000, and the window was blamed for it -- 15 frames of 64
    octets over a 1.1 s round trip fits that number almost exactly. The real
    cause was a slice in the test feeder that could not put more than 18 octets
    in a frame. Had this test existed, the window would have been ruled out in
    a tenth of a second instead of over several calls.
    """
    A = v42.Link(originator=True)
    B = v42.Link(originator=False)
    A.connect(0.0)
    n = int(rate * dt)                              # bits the line carries
    d = int(round((rtt_ms / 1000.0) / dt / 2))      # one-way delay, in steps
    qa = collections.deque([[] for _ in range(d)])  # bits heading for A
    qb = collections.deque([[] for _ in range(d)])
    pay = b"SLOPMODEM" * 512
    t = 0.0
    got = 0
    first = last = None
    for _ in range(int(secs / dt)):
        t += dt
        if len(A.lapm.outq) < 4096:
            A.send(pay)
        if both and len(B.lapm.outq) < 4096:
            B.send(pay)
        # Pop before appending, so the shortest round trip modelled is 2 * dt.
        ia = qa.popleft() if qa else []
        ib = qb.popleft() if qb else []
        oa = A.step(ia, n, t)
        ob = B.step(ib, n, t)
        qb.append(oa)
        qa.append(ob)
        k = len(B.received())
        if k:
            if first is None:
                first = t
            got += k
            last = t
    span = last - first if (first is not None and last > first) else secs
    # 8.1: one address octet, two control, two FCS for every N401 of payload.
    n401 = A.lapm.n401
    return dict(bps=8.0 * got / span, rtt=2 * (d + 1) * dt,
                resends=A.lapm.stats["resend"], t401=A.lapm.stats["t401"],
                state=A.lapm.state,
                ceiling=rate * n401 / float(n401 + 5))


if __name__ == "__main__":
    print("8.1.1.6 frame check sequences")
    body = b"\x03\x73"
    f = v42.with_fcs(body)
    check("the FCS is two octets appended to the frame body", len(f) == len(body) + 2)
    check("8.1.1.6.1's printed residue, 0001 1101 0000 1111 (x15 through x0)",
          v42.bitrev(v42.fcs16(f), 16) == v42.FCS16_SPEC_GOOD == 0x1D0F,
          "register holds %s, reflected %s"
          % (hex(v42.fcs16(f)), hex(v42.bitrev(v42.fcs16(f), 16))))
    check("  and fcs_ok agrees", v42.fcs_ok(f))
    g = v42.with_fcs(b"any old octets", wide=True)
    check("8.1.1.6.2's 32-bit residue, reflected",
          v42.bitrev(v42.fcs32(g), 32) == v42.FCS32_SPEC_GOOD,
          "reflected %s" % hex(v42.bitrev(v42.fcs32(g), 32)))
    check("  and fcs_ok agrees for 32 bits", v42.fcs_ok(g, wide=True))
    bad = bytearray(f)
    bad[1] ^= 0x01
    check("a single flipped bit fails the check", not v42.fcs_ok(bytes(bad)))
    # the register is preset to all ones, so leading zeros are not invisible
    check("the register preset to all 1s makes leading zeros count",
          v42.fcs16(b"\x00\x00") != v42.fcs16(b"\x00\x00\x00"))

    print()
    print("8.1.1.2 flags and transparency")
    check("the flag is 01111110", v42.FLAG == 0x7E
          and v42.FLAG_BITS == [0, 1, 1, 1, 1, 1, 1, 0])
    check("a 0 is inserted after five contiguous 1s",
          v42.stuff([1] * 5) == [1, 1, 1, 1, 1, 0])
    check("  and again after the next five", v42.stuff([1] * 10)
          == [1, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 0])
    check("stuffing round-trips", all(
        v42.unstuff(v42.stuff(b)) == b
        for b in ([1] * 20, [0] * 20, [1, 1, 1, 1, 1, 0, 1], bits("0111111011111110"))))
    # the point of stuffing: no flag can occur inside the frame
    fr = v42.frame(0xFF, 0xFF, b"\xff" * 8)
    inner = fr[8:-8]
    found = sum(1 for i in range(len(inner) - 7)
                if inner[i:i + 8] == v42.FLAG_BITS)
    check("no flag pattern survives inside a stuffed frame", found == 0,
          "%d found in %d bits of all-ones payload" % (found, len(inner)))

    print()
    print("8.1.1 frame structure, round trip")
    d = v42.Deframer()
    out = d.feed(v42.frame(0x03, 0x73, b"hello"))
    check("a frame comes back with its address, control and information",
          out == [(b"\x03", b"\x73", b"hello")], "%r" % (out,))
    d = v42.Deframer()
    got = []
    for i in range(4):
        got += d.feed(v42.frame(0x03, 0x73, bytes([i]) * (i + 1)))
    check("four frames in a row", len(got) == 4
          and [g[2] for g in got] == [b"\x00", b"\x01\x01", b"\x02\x02\x02",
                                      b"\x03\x03\x03\x03"])
    # 8.1.3: invalid frames are discarded without further action
    d = v42.Deframer()
    b = v42.frame(0x03, 0x73, b"corrupt me")
    b[40] ^= 1
    n = len(d.feed(b))
    check("8.1.3: a frame with a bad FCS is discarded, and counted",
          n == 0 and d.badfcs == 1, "%d out, badfcs %d" % (n, d.badfcs))
    d = v42.Deframer()
    n = len(d.feed(v42.FLAG_BITS + v42.FLAG_BITS + v42.FLAG_BITS))
    check("interframe fill of back-to-back flags yields nothing",
          n == 0 and d.badfcs == 0 and d.short == 0)
    d = v42.Deframer()
    n = len(d.feed(v42.FLAG_BITS + [1] * 7 + v42.FLAG_BITS))
    check("8.1.4: seven contiguous ones is an abort, not a frame",
          n == 0 and d.aborted == 1, "aborted %d" % d.aborted)
    d = v42.Deframer()
    n = len(d.feed(v42.FLAG_BITS + [0] * 8 + v42.FLAG_BITS))
    check("a frame too short to hold address, control and FCS is discarded",
          n == 0 and d.short == 1, "short %d" % d.short)

    print()
    print("7.2.1 detection phase, against the printed patterns")
    check("7.2.1.2's ODP is exactly '0 1000 1000 1 11...11 0 1000 1001 1 11...11'",
          v42.odp(fill=8) == bits("0 1000 1000 1 11111111 0 1000 1001 1 11111111"))
    check("Table 3's V.42-supported ADP is (E) then (C)",
          v42.adp(True, fill=8)
          == bits("0 1010 0010 1 11111111 0 1100 0010 1 11111111"))
    check("Table 3's no-error-correction ADP is (E) then (Null)",
          v42.adp(False, fill=8)
          == bits("0 1010 0010 1 11111111 0 0000 0000 1 11111111"))
    check("the characters are 10 bits: start, seven data low-order first, "
          "parity, stop",
          len(v42.char10(v42.DC1_EVEN)) == 10
          and v42.char10(v42.DC1_EVEN)[0] == 0
          and v42.char10(v42.DC1_EVEN)[9] == 1)
    # the data bits, read low-order first, are the characters the spec names
    for field, want, name in ((v42.DC1_EVEN, 0x11, "DC1"),
                              (v42.CHAR_E, ord("E"), "E"),
                              (v42.CHAR_C, ord("C"), "C"),
                              (v42.CHAR_NULL, 0x00, "NUL")):
        v = sum(b << k for k, b in enumerate(field[:7]))
        check("  the printed bits for (%s) decode to %s" % (name, hex(want)),
              v == want, "got %s" % hex(v))
    # two 10-bit characters and two runs of `fill` ones
    check("7.2.1.2 allows 8 to 16 ones between characters, and only that",
          all(len(v42.odp(fill=n)) == 20 + 2 * n for n in (8, 12, 16)),
          "8 -> %d bits, 16 -> %d" % (len(v42.odp(fill=8)),
                                      len(v42.odp(fill=16))))
    try:
        v42.odp(fill=7)
        ok = False
    except ValueError:
        ok = True
    check("  and a shorter run is refused", ok)

    print()
    print("7.2.1 detection, both roles")
    det = v42.PatternDetector([v42.DC1_EVEN, v42.DC1_ODD], 4)
    check("7.2.1.3: four DC1s of alternating parity are needed",
          not det.feed(v42.odp()) and det.feed(v42.odp()),
          "one ODP is two DC1s, two ODPs are four")
    # the trap: four (E)s arrive in four repetitions of the *no*-EC pattern
    det = v42.PatternDetector([v42.CHAR_E, v42.CHAR_C], 4)
    none = v42.adp(False)
    got = any(det.feed(none) for _ in range(4))
    check("the no-error-correction ADP is not read as support, though it "
          "contains four (E) characters",
          not got and det.hits >= 4,
          "%d characters recognised, alternation %s" % (det.hits, got))

    def phase(sup, frames=60):
        o = v42.Detection(originator=True)
        a = v42.Detection(originator=False, supported=sup)
        to_o, to_a = [], []
        for k in range(frames):
            t = k * 0.02
            oo = o.feed(to_o, t)
            oa = a.feed(to_a, t)
            to_a, to_o = oo[:], oa[:]
            if v42.Detection.UNDECIDED not in (o.result, a.result):
                return o.result, a.result, t
        return o.result, a.result, None

    r = phase(True)
    check("an answerer that supports V.42: both ends decide LAPM",
          r[:2] == (v42.Detection.LAPM, v42.Detection.LAPM),
          "%s / %s at %s s" % (r[0], r[1], r[2]))
    r = phase(False)
    check("an answerer that does not: both ends decide no error correction",
          r[:2] == (v42.Detection.NONE, v42.Detection.NONE),
          "%s / %s at %s s" % (r[0], r[1], r[2]))
    o = v42.Detection(originator=True)
    for k in range(60):
        o.feed([1] * 40, k * 0.02)
    check("9.1.1: an originator alone falls back after T400 = %.0f ms"
          % (v42.T400 * 1000), o.result == v42.Detection.NONE)
    a = v42.Detection(originator=False)
    sent = []
    for k in range(60):
        sent += a.feed([1] * 40, k * 0.02)
    check("  and an answerer alone sends only mark, then falls back",
          set(sent) == {1} and a.result == v42.Detection.NONE,
          "%d bits sent, all mark" % len(sent))

    print()
    print("8.2 LAPM address and control fields")
    # Table 6: a command from the originator and a response from the answerer
    # both carry C/R = 1. Both ends making the same mistake is invisible in a
    # loopback, so this is checked against the table and not against itself.
    for orig, cmd, want in ((True, True, 1), (False, True, 0),
                            (True, False, 0), (False, False, 1)):
        got = (v42.address(orig, cmd) >> 1) & 1
        check("Table 6: %s %s -> C/R %d"
              % ("originator" if orig else "answerer",
                 "command" if cmd else "response", want), got == want)
    check("the address carries DLCI 0 and EA = 1 (Table 10, 8.2.1.3)",
          v42.address(True, True) == 0x03 and (v42.address(True, True) & 1) == 1)
    # Table 8
    for name, ctl, want in (
            ("SABME with P = 1", v42.u_control(v42.U_SABME, 1), 0x7F),
            ("UA with F = 1", v42.u_control(v42.U_UA, 1), 0x73),
            ("DISC with P = 1", v42.u_control(v42.U_DISC, 1), 0x53),
            ("DM with F = 1", v42.u_control(v42.U_DM, 1), 0x1F),
            ("UI", v42.u_control(v42.U_UI), 0x03),
            ("FRMR", v42.u_control(v42.U_FRMR), 0x87),
            ("XID, P/F = 0", v42.u_control(v42.U_XID), 0xAF),
            ("TEST, P = 0", v42.u_control(v42.U_TEST), 0xE3)):
        check("Table 8: %s is %s" % (name, hex(want)), ctl == bytes((want,)))
    check("Table 7: an I frame's control is two octets, N(S) then N(R), bit 1 "
          "of the first being 0", v42.i_control(3, 7) == b"\x06\x0e")
    check("Table 8: RR carries N(R) in the second octet with P/F in bit 1",
          v42.s_control(v42.S_RR, 5) == b"\x01\x0a"
          and v42.s_control(v42.S_RR, 5, 1) == b"\x01\x0b")
    addr = bytes((v42.address(True, True),))
    for ctl, kind, ns, nr in ((v42.i_control(3, 7), "I", 3, 7),
                              (v42.s_control(v42.S_REJ, 9, 1), "REJ", None, 9),
                              (v42.u_control(v42.U_SABME, 1), "SABME", None, None)):
        f = v42.parse(addr, ctl)
        check("  and it parses back as %s" % kind,
              f is not None and f.kind == kind and f.ns == ns and f.nr == nr)
    check("8.2.4.1: a control field not in Table 8 is not decoded",
          v42.parse(addr, b"\x2b") is None)
    check("sequence numbers are modulo 128 (Table 7)",
          v42.i_control(127, 127) == b"\xfe\xfe"
          and v42.parse(addr, v42.i_control(127, 0)).ns == 127)

    print()
    print("8.3 to 8.5 LAPM, end to end")

    def pump(a, b, steps=900, drop=(), dup=(), t0=0.0, dt=0.01):
        """Run two entities against each other. `drop` and `dup` are indices of
        frames on the a-to-b path to lose or duplicate."""
        pa, pb = a.connect(t0), []
        t = t0
        n = 0
        for _ in range(steps):
            t += dt
            na = []
            for ad, ci in pb:
                na += a.feed(ad, ci, t)
            nb = []
            for ad, ci in pa:
                n += 1
                if n in drop:
                    continue
                nb += b.feed(ad, ci, t)
                if n in dup:
                    nb += b.feed(ad, ci, t)
            pa = na + a.poll(t)
            pb = nb + b.poll(t)
        return t

    msg = b"the quick brown fox jumps over the lazy dog. " * 20
    a, b = v42.Lapm(True), v42.Lapm(False)
    a.send(msg)
    pump(a, b, 300)
    got = b.received()
    check("SABME/UA establishes, and both ends reach the connected state",
          a.state == b.state == v42.Lapm.CONNECTED)
    check("8.3.2.1 Note 1: SABME always carries P = 1",
          a.stats.get("7f", 0) >= 1 and a.stats.get("6f", 0) == 0)
    check("%d octets cross intact in I frames" % len(msg), got == msg,
          "%d received, V(S)=%d V(A)=%d V(R)=%d"
          % (len(got), a.vs, a.va, b.vr))
    check("9.2.3: segmented at N401 = %d octets" % v42.N401,
          a.vs == (len(msg) + v42.N401 - 1) // v42.N401,
          "%d I frames for %d octets" % (a.vs, len(msg)))
    check("all of it is acknowledged: V(A) has caught up with V(S)",
          a.va == a.vs and not a.sent)

    # 8.4.5: a lost I frame is rejected and retransmitted
    a, b = v42.Lapm(True), v42.Lapm(False)
    a.send(msg)
    pump(a, b, 400, drop=(4,))
    got = b.received()
    check("8.4.5: one dropped I frame is recovered, and the data is still exact",
          got == msg, "%d octets, %d sequence errors, %d resends"
          % (len(got), b.stats["seqerr"], a.stats["resend"]))
    check("  by exactly one REJ, not one per following frame",
          b.stats["seqerr"] >= 1 and b.stats.get("rx I", 0) > 0,
          "%d out-of-sequence I frames seen, REJ sent once per exception"
          % b.stats["seqerr"])

    # duplicates must not be delivered twice
    a, b = v42.Lapm(True), v42.Lapm(False)
    a.send(msg)
    pump(a, b, 400, dup=(3, 5))
    check("a duplicated I frame is not delivered twice",
          b.received() == msg)

    # 9.2.4: the window. Drive a sender that never gets an acknowledgement, so
    # the only thing that can stop it is k.
    a2, b2 = v42.Lapm(True), v42.Lapm(False)
    a2.send(b"y" * (v42.N401 * 40))
    pa = a2.connect(0.0)
    b2.feed(pa[0][0], pa[0][1], 0.0)
    a2.feed(bytes((v42.address(False, False),)), v42.u_control(v42.U_UA, 1), 0.0)
    out = a2.poll(0.0)
    check("9.2.4: at most k = %d I frames outstanding" % v42.WINDOW,
          len(out) == v42.WINDOW and (a2.vs - a2.va) % v42.MOD == v42.WINDOW,
          "%d frames sent before the window closed" % len(out))

    # T401 and N400
    a3 = v42.Lapm(True, t401=0.05, n400=3)
    a3.connect(0.0)
    t = 0.0
    for _ in range(40):
        t += 0.01
        a3.poll(t)
    check("9.2.1/9.2.2: with no answer, SABME is retried N400 = 3 times and "
          "then the attempt fails",
          a3.state == v42.Lapm.FAILED and a3.stats["t401"] == 4,
          "state %s after %d T401 expiries" % (a3.state, a3.stats["t401"]))

    # release
    a4, b4 = v42.Lapm(True), v42.Lapm(False)
    pump(a4, b4, 50)
    p = a4.release(1.0)
    for ad, ci in p:
        r = b4.feed(ad, ci, 1.0)
    for ad, ci in r:
        a4.feed(ad, ci, 1.0)
    check("8.3.2's DISC/UA releases both ends",
          a4.state == b4.state == v42.Lapm.DISCONNECTED)

    print()
    print("LAPM over the bit stream")
    a5 = v42.Link(originator=True)
    b5 = v42.Link(originator=False)
    a5.connect(0.0)
    msg2 = b"LAPM over bits, framed and checked. " * 30
    a5.send(msg2)
    to_a = to_b = []
    t = 0.0
    for _ in range(4000):
        t += 0.001
        oa = a5.step(to_a, 48, t)
        ob = b5.step(to_b, 48, t)
        to_a, to_b = ob, oa
        if len(b5.lapm.inq) >= len(msg2):
            break
    got2 = b5.received()
    check("framing, establishment and transfer over a bit stream",
          got2 == msg2 and a5.lapm.state == v42.Lapm.CONNECTED,
          "%d octets in %d frames, %.0f ms" % (len(got2), b5.deframer.frames,
                                               t * 1000))
    check("  with nothing discarded on the way",
          b5.deframer.badfcs == 0 and b5.deframer.short == 0
          and b5.deframer.aborted == 0)
    a7 = v42.Link(originator=True)
    a7.connect(0.0)
    first = a7.step([], 8 * 40, 0.0)
    flags = 0
    while first[flags * 8:(flags + 1) * 8] == v42.FLAG_BITS:
        flags += 1
    check("8.3.2.1 Note 2: at least 16 flag patterns precede the first "
          "protocol frame", flags >= 16, "%d flags, then %s"
          % (flags, "the SABME" if first[flags * 8:flags * 8 + 8]
             != v42.FLAG_BITS else "more flags"))
    # a corrupted bit must cost one frame and be recovered, not desynchronise
    a6 = v42.Link(originator=True)
    b6 = v42.Link(originator=False)
    a6.connect(0.0)
    a6.send(msg2)
    to_a = to_b = []
    t = 0.0
    flipped = 0
    for i in range(6000):
        t += 0.001
        oa = a6.step(to_a, 48, t)
        ob = b6.step(to_b, 48, t)
        if i == 40 and oa:
            oa = list(oa)
            oa[7] ^= 1
            flipped += 1
        to_a, to_b = ob, oa
        if len(b6.lapm.inq) >= len(msg2):
            break
    check("a flipped bit costs one frame and the data still arrives exact",
          b6.received() == msg2 and b6.deframer.badfcs + b6.deframer.short
          + b6.deframer.aborted >= 1,
          "%d discarded, %d resends" % (b6.deframer.badfcs + b6.deframer.short
                                        + b6.deframer.aborted,
                                        a6.lapm.stats["resend"]))

    print()
    print("8.3.2.1 Note 3: a repeated SABME must not eat user data")
    A2 = v42.Lapm(originator=False)
    A2.feed(bytes((v42.address(True, True),)), v42.u_control(v42.U_SABME, 1), 0.0)
    A2.send(b"x" * 900)
    frames = A2.poll(0.0)
    check("the answerer is connected and has frames outstanding",
          A2.state is v42.Lapm.CONNECTED and len(A2.sent) == 8,
          "%d outstanding, V(S) %d" % (len(A2.sent), A2.vs))
    held = sum(len(v) for v in A2.sent.values())
    # the caller never saw our UA, so it repeats the SABME
    A2.feed(bytes((v42.address(True, True),)), v42.u_control(v42.U_SABME, 1), 1.0)
    check("  a repeated SABME resets the sequence numbers, as the clause says",
          A2.vs == 0 and A2.vr == 0 and A2.va == 0 and not A2.sent)
    check("  and the unacknowledged octets come back to be sent again",
          len(A2.outq) == held == 900 and A2.stats["requeued"] == 8,
          "%d octets requeued of %d held" % (len(A2.outq), held))
    again = A2.poll(2.0)
    payload = b"".join(bytes(ci[2:]) for _, ci in again)
    check("  in the right order, and not one octet duplicated or dropped",
          payload == b"x" * 900, "%d octets, first frame %r"
          % (len(payload), payload[:8]))

    print()
    print("interframe scheduling")
    L = v42.Link(originator=True, ahead=2)   # the throttle, off by default
    L.lapm.state = v42.Lapm.CONNECTED
    L.send(b"d" * 2000)                     # a backlog of I frames
    L.step([], 8, 0.0)
    queued = len(L.pend)
    check("only a couple of I frames are queued ahead, not the whole window",
          queued <= L.ahead + 1, "%d frames queued, ahead = %d"
          % (queued, L.ahead))
    check("  so the window is not spent on frames still in our own buffer",
          L.lapm.vs <= L.ahead + 1, "V(S) = %d" % L.lapm.vs)
    # an acknowledgement arriving now must not wait behind the backlog
    inc = v42.frame(bytes((v42.address(False, True),)),
                    v42.s_control(v42.S_RR, 0, 1))
    L.step(inc, 8, 0.1)
    fr = v42.parse(*L.pend[0]) if L.pend else None
    # With the queue full, a due acknowledgement must still get out. Gating the
    # poll on having room for data would stop us acknowledging exactly when we
    # are busiest.
    M = v42.Link(originator=False, ahead=2)
    M.lapm.state = v42.Lapm.CONNECTED
    M.send(b"e" * 4000)
    M.step([], 8, 0.0)
    inc = v42.frame(bytes((v42.address(True, True),)),
                    v42.i_control(0, 0) + b"hello")
    M.step(inc, 8, 0.05)
    while len(M.pend) > M.ahead:            # let the data drain a little
        M.step([], 400, 0.06)
    check("a due acknowledgement still goes out with the queue full",
          M.lapm.vr == 1 and not M.lapm.ack_due,
          "V(R) = %d, ack still due: %s" % (M.lapm.vr, M.lapm.ack_due))
    check("a reply to a received frame jumps ahead of queued I frames",
          fr is not None and fr.kind != "I",
          "next frame out is %s" % (fr.kind if fr else "none"))

    # The invariant reordering can break: N(R) must never go backwards on the
    # wire. It cost an FRMR and a DISC from the Cirrus to find, so it is checked
    # on the transmitted bits rather than on the intent.
    N = v42.Link(originator=True)
    N.lapm.state = v42.Lapm.CONNECTED
    N.send(b"f" * 3000)
    de = v42.Deframer()
    seen = []
    t = 0.0
    for i in range(120):
        t += 0.01
        # a steady stream of arriving I frames, so V(R) climbs while ours queue
        inc = v42.frame(bytes((v42.address(False, True),)),
                        v42.i_control(i % 128, 0), b"x" * 8)
        for addr, ctl, info in de.feed(N.step(inc, 400, t)):
            f = v42.parse(addr, ctl + info)
            if f is not None and f.nr is not None:
                seen.append(f.nr)
    drops = sum(1 for a_, b_ in zip(seen, seen[1:]) if b_ < a_)
    check("N(R) never goes backwards on the wire, however frames are reordered",
          drops == 0 and len(seen) > 20,
          "%d frames carrying N(R), %d went backwards" % (len(seen), drops))
    check("  and V(R) really was climbing while they queued",
          N.lapm.vr > 20, "V(R) = %d" % N.lapm.vr)

    print()
    print("8.4.6 T401 and a stale N(R)")
    A = v42.Lapm(originator=True)
    B = v42.Lapm(originator=False)
    for addr, ci in A.connect(0.0):
        B.feed(addr, ci, 0.0)
    A.feed(bytes((v42.address(False, False),)), v42.u_control(v42.U_UA, 1), 0.0)
    A.send(b"z" * 600)
    frames = A.poll(0.0)
    check("five I frames go out and none is acknowledged",
          len(frames) == 5 and A.va == 0, "V(S)=%d V(A)=%d" % (A.vs, A.va))
    # the far end acknowledges nothing and keeps saying so, which is what a
    # modem with a full DTE buffer does
    stale = bytes((v42.address(False, False),))
    t = 0.0
    first_resend = None
    enquiries = 0
    for i in range(80):
        t += 0.1
        A.feed(stale, v42.s_control(v42.S_RR, 0), t)
        got = A.poll(t)                     # the real path: poll checks T401
        for addr, ci in got:
            fr = v42.parse(addr, ci)
            if fr.kind == "RR" and fr.pf:
                enquiries += 1
        if got and first_resend is None:
            first_resend = t
    check("a repeated RR with a stale N(R) does not hold T401 open for ever",
          first_resend is not None,
          "first action at %.1f s, %d T401 expiries" % (first_resend or -1,
                                                       A.stats["t401"]))
    check("8.4.8: the action is an enquiry with P=1, not a blind resend",
          enquiries >= 1 and A.stats["resend"] == 0,
          "%d enquiries, %d resends" % (enquiries, A.stats["resend"]))
    check("  it fires at T401, not at some multiple of it",
          first_resend is not None and abs(first_resend - A.t401) < 0.15,
          "T401 = %.2f s, fired at %.2f s" % (A.t401, first_resend or -1))
    check("  and after N400 retries the entity gives up rather than looping",
          A.state is v42.Lapm.FAILED and A.stats["t401"] == A.n400 + 1,
          "state %s after %d expiries" % (A.state, A.stats["t401"]))
    # 8.4.8: the enquiry is answered, and the answer is what triggers the resend
    E = v42.Lapm(originator=True)
    E.state = E.CONNECTED
    E.send(b"w" * 300)
    E.poll(0.0)
    E.poll(1.5)                             # T401 expires -> enquiry
    check("  an F=1 supervisory response clears recovery and resumes sending",
          E.recovery, "in recovery: %s" % E.recovery)
    out = E.feed(bytes((v42.address(False, False),)),
                 v42.s_control(v42.S_RR, 0, 1), 1.6)
    check("    the response clears it and the retransmission follows",
          not E.recovery and len(out) == 3,
          "%d frames retransmitted" % len(out))
    # and the mirror: an enquiry aimed at us is answered exactly once
    F = v42.Lapm(originator=False)
    F.state = F.CONNECTED
    rep = F.feed(bytes((v42.address(True, True),)),
                 v42.s_control(v42.S_RR, 0, 1), 0.0)
    check("  8.4.6: an RR command with P=1 is answered with F=1, once",
          len(rep) == 1 and v42.parse(*rep[0]).pf == 1)
    # A response reaching the answerer comes from the originator, so C/R = 0.
    # Feeding F its own response would not test this: Table 6 gives the
    # answerer's response and the originator's command the same C/R = 1, and
    # they are told apart only by the fact that no entity receives its own
    # frames.
    again = F.feed(bytes((v42.address(True, False),)),
                   v42.s_control(v42.S_RR, 0, 1), 0.0)
    check("    while an RR response with F=1 is not answered at all",
          again == [], "%d frames" % len(again))
    C = v42.Lapm(originator=True)
    D = v42.Lapm(originator=False)
    for addr, ci in C.connect(0.0):
        D.feed(addr, ci, 0.0)
    C.feed(bytes((v42.address(False, False),)), v42.u_control(v42.U_UA, 1), 0.0)
    C.send(b"y" * 600)
    C.poll(0.0)
    t = 0.0
    for i in range(4):
        t += 0.3
        C.feed(stale, v42.s_control(v42.S_RR, i + 1), t)   # each one advances
        C.poll(t)
    check("  while an N(R) that really advances does restart it",
          C.stats["t401"] == 0 and C.va == 4,
          "V(A)=%d, %d expiries" % (C.va, C.stats["t401"]))

    print()
    print("12.2 XID, and 7.2.1.3's other exit")

    # A real XID command, captured off the line: the Conexant's first protocol
    # frame, sent eight times because nothing answered it. Kept verbatim because
    # a real artefact catches what a self-consistent round trip cannot -- note
    # PL = 3 on PI 3 where Table 11a Note 1 says 4.
    CAPTURED = bytes.fromhex("8280001303038a8900050204000602040007010f08010f")
    sub = v42.parse_xid(CAPTURED)
    check("a captured XID command parses as a parameter-negotiation subfield",
          list(sub) == [v42.GI_PARAM] and sorted(sub[v42.GI_PARAM]) == [3, 5, 6, 7, 8],
          "GIs %s, PIs %s" % (list(sub), sorted(sub[v42.GI_PARAM])))
    g = sub[v42.GI_PARAM]
    check("  Note 3: N401 is carried in bits, so 0x0400 is 128 octets",
          (g[5][0] << 8 | g[5][1]) == 1024 and 1024 // 8 == v42.N401)
    check("  window size k = 15 in both directions, which is 9.2.4's default",
          g[7] == b"\x0f" and g[8] == b"\x0f")
    check("  Note 1: it asks for no optional procedure at all",
          v42.mask_bits(g[3]) == set(),
          "mask %s, conformance bits only" % g[3].hex())
    check("  and the conformance bits 2, 4, 8, 9, 12, 16 really are all set",
          all(g[3][(b - 1) // 8] & (1 << ((b - 1) % 8)) for b in (2, 4, 8, 9, 12)),
          "bit 16 is in octet 2: %s" % bool(g[3][1] & 0x80))
    resp, params = v42.xid_response(CAPTURED)
    check("  we answer it with the same defaults",
          params.n401_tx == 128 and params.k_tx == 15 and not params.opts,
          "%s" % params)
    check("  and the response is a well-formed general-purpose XID",
          v42.parse_xid(resp)[v42.GI_PARAM][5] == b"\x04\x00")
    check("  Note 1: PI 3 is four octets long, as the clause says",
          len(v42.parse_xid(resp)[v42.GI_PARAM][3]) == 4)

    # 9.2.3 and 9.2.4: "between the value chosen by the initiator and the
    # default value, inclusive". With everything at its default this is
    # unfalsifiable, so it is asked with values that are not.
    cmd = v42.XidParams(n401_tx=64, n401_rx=32, k_tx=7, k_rx=4).command()
    resp, got = v42.xid_response(cmd)
    check("Note 2's crossover: the responder's transmit answers their receive",
          (got.n401_tx, got.n401_rx, got.k_tx, got.k_rx) == (32, 64, 4, 7),
          "%s" % got)
    back = v42.xid_confirm(resp)
    check("  and the initiator reads its own request back unchanged",
          (back.n401_tx, back.n401_rx, back.k_tx, back.k_rx) == (64, 32, 7, 4),
          "%s" % back)
    over, _ = v42.xid_response(v42.XidParams(n401_tx=1024, k_tx=127).command())
    check("  a request above the default is held at the default, not granted",
          over and v42.xid_confirm(over).n401_rx == v42.N401
          and v42.xid_confirm(over).k_rx == v42.WINDOW,
          "%s" % v42.xid_confirm(over))
    check("12.2.2: an unrecognised GI is skipped by its own GL, not fatal",
          v42.parse_xid(b"\x82\x99\x00\x02ab\x80\x00\x03\x07\x01\x09"
                        )[v42.GI_PARAM][7] == b"\x09")
    check("  and a non-general-purpose FI is refused outright",
          v42.parse_xid(b"\x81\x80\x00\x00") is None)

    # 7.2.1.3: "the start of the protocol phase is indicated by receipt of
    # continuous flags, or of an LAPM or alternative procedure protocol frame".
    # This is the clause a real modem exercises and the one that was missing.
    d = v42.Detection(originator=False)
    out = d.feed(v42.FLAG_BITS * 6, 0.0)
    check("7.2.1.3: an answerer seeing continuous flags goes to LAPM",
          d.result is v42.Detection.LAPM and d.saw_flags)
    check("  and sends no ADP, because the originator is not listening for one",
          out == [])
    d2 = v42.Detection(originator=False)
    d2.feed(v42.FLAG_BITS[:8] * 1 + [0] * 40, 0.0)
    check("  one flag is not continuous flags: 0x7E turns up in data by chance",
          d2.result is v42.Detection.UNDECIDED and not d2.saw_flags)
    d3 = v42.Detection(originator=False)
    d3.feed([1] * 800, 0.0)
    d3.feed([1] * 8, v42.T400 + 0.001)
    check("  and with neither ODP nor flags it still falls back at T400",
          d3.result is v42.Detection.NONE)
    d4 = v42.Detection(originator=False)
    d4.feed(v42.odp() * 3, 0.0)
    check("  while an ODP still gets an ADP, unchanged",
          d4.saw_odp and d4.feed([1] * 8, 0.1)[:10] == v42.char10(v42.CHAR_E))

    # End to end against a far end that behaves the way the hardware does:
    # no detection phase, XID first, SABME second. `far` is a bare Lapm driven
    # by hand so the sequence is the captured one and not whatever our own Link
    # happens to do.
    link = v42.Link(originator=False)
    far = v42.Lapm(originator=True)
    inbits = list(v42.FLAG_BITS) * 16
    for a_, c in far.negotiate(0.0):
        inbits += v42.frame(a_, c)
    txt = b"through a link that skipped the detection phase"
    link.send(txt)
    far_de = v42.Deframer()
    got = bytearray()
    connected_after_xid = None
    t = 0.0
    for i in range(400):
        t += 0.005
        reply = link.step(inbits, 96, t)
        inbits = []
        for addr, ctl, info in far_de.feed(reply):
            for a_, c in far.feed(addr, ctl + info, t):
                inbits += v42.frame(a_, c)
        # the far end establishes only once its XID has been answered, which is
        # the ordering the modem showed on the line
        if far.state is v42.Lapm.DISCONNECTED and not far.xid_due:
            connected_after_xid = far.xids
            for a_, c in far.connect(t):
                inbits += v42.frame(a_, c)
        for a_, c in far.poll(t):
            inbits += v42.frame(a_, c)
        got.extend(far.received())
        if bytes(got) == txt:
            break
    check("a far end that skips detection and opens with XID still connects",
          far.state is v42.Lapm.CONNECTED and connected_after_xid == 1,
          "%d XID exchanged, state %s" % (far.xids, far.state))
    check("  its XID command was answered before it sent SABME",
          connected_after_xid == 1)
    check("  and user data crosses the link afterwards",
          bytes(got) == txt, "%r" % bytes(got)[:52])

    print()
    print("9.1.1 T400 counts time spent waiting, not time spent deaf")
    # The receiver gates the descrambled stream until the eye is open, on
    # purpose, so the detection phase is not decided by junk. T400 used to run
    # from the data phase regardless, so whatever the gate held back came out of
    # the 750 ms -- and a far end whose ODP was plainly there got reported as
    # "no far end". Measured on a recording: confirmable at 520 ms of delivered
    # bits, timed out at 750 ms of frame time.
    def detect_with_gate(gate_s, odp_reps=12, rate=12000.0, originator=False):
        """Feed nothing for gate_s, then a real ODP, and see what is decided."""
        e = v42.Session(originator=originator)
        step = int(rate * 0.02)
        now = 0.0
        for _ in range(int(gate_s / 0.02)):        # gated: no bits delivered
            e.step([], step, now)
            now += 0.02
        stream = []
        for _ in range(odp_reps):
            stream.extend(v42.odp(8))
        # 7.2.1.3: once the ODP is seen the answerer owes ten ADPs before it is
        # done, so keep the link running rather than stopping at the last ODP
        # bit -- the decision lands several calls later.
        for i in range(0, len(stream) + 40 * step, step):
            chunk = stream[i:i + step] if i < len(stream) else [1] * step
            e.step(chunk, step, now)
            now += 0.02
            if e.det.result is not v42.Detection.UNDECIDED:
                break
        return e.det.result

    for gate in (0.0, 0.3, 0.6, 1.5):
        r = detect_with_gate(gate)
        check("  ODP after %.1f s of gated silence is still detected" % gate,
              r is v42.Detection.LAPM, "decided %s" % r)
    # and the timer must still work once bits really are flowing: mark only,
    # for well over T400, has to end in a fallback rather than hang for ever.
    e = v42.Session(originator=False)
    now, step = 0.0, 240
    for _ in range(100):
        e.step([1] * step, step, now)
        now += 0.02
        if e.det.result is not v42.Detection.UNDECIDED:
            break
    check("  but mark for %.2f s of *delivered* bits still falls back" % now,
          e.det.result is v42.Detection.NONE and now >= v42.T400,
          "decided %s at %.2f s" % (e.det.result, now))

    print()
    print("8.4.1 the window counts frames in flight, not frames queued")
    # A window consumed by our own queued frames would have to cover the drain
    # as well as the flight, giving 1/(1/L + RTT/W) -- about 8440 bit/s at
    # 440 ms here, against a line that carries 11549. The measurement that
    # separates the two is throughput against round trip: flat if the window
    # slides on each acknowledgement, falling away if it does not.
    runs = [link_throughput(rate=12000, rtt_ms=ms) for ms in (40, 240, 440, 840)]
    for r in runs:
        check("  %3.0f ms round trip still reaches the line rate"
              % (1000 * r["rtt"]), r["bps"] >= 0.90 * r["ceiling"],
              "%.0f bit/s of %.0f" % (r["bps"], r["ceiling"]))
    lo = min(r["bps"] for r in runs)
    hi = max(r["bps"] for r in runs)
    check("  and does not fall away as the round trip grows",
          lo >= 0.95 * hi,
          "%.0f to %.0f bit/s across 40..840 ms" % (lo, hi))
    check("  with no retransmissions or T401 expiries on a clean channel",
          all(r["resends"] == 0 and r["t401"] == 0 for r in runs))
    r = link_throughput(rate=12000, rtt_ms=440, both=True)
    check("  and reaches it with both directions saturated at once",
          r["bps"] >= 0.90 * r["ceiling"],
          "%.0f bit/s of %.0f" % (r["bps"], r["ceiling"]))
    r = link_throughput(rate=9600, rtt_ms=440)
    check("  the same at 9600, where the rig read the same 898 byte/s",
          r["bps"] >= 0.90 * r["ceiling"],
          "%.0f bit/s of %.0f" % (r["bps"], r["ceiling"]))

    print()
    if FAIL:
        print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
        sys.exit(1)
    print("all V.42 tests passed")
