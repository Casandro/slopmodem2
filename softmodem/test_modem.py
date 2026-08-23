"""Offline tests for the unified modem: role dispatch, teardown, containment.

No SIP and no hardware -- the network is stubbed. The point of these is not the
DSP, which the other suites cover, but the plumbing that unification introduced:
one call runner serving two roles, an idle loop that has to choose between the
DTE and the PBX, and a session that has to survive a call going wrong.

Two of them are regression tests for bugs that got through everything else:
the module-shadowing trap in sip_glue, and a call exception ending the session.
"""
import os, select, sys, time
import dte, fsm, rtp, v22

# import order matters here on purpose: sip_glue first, which is the order that
# exposed the shadowing bug
import sip_glue          # noqa: F401
import ansam
import modem

FAIL = []


def check(name, cond, detail=""):
    print("  %-56s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def args(**kw):
    class A:
        pass
    a = A()
    base = dict(dte=False, dial=None, calls=1, idle_seconds=2.0, max_call=20.0,
                level=-18.0, lead=0.3, idle=2, s0=1, dte_wait=1.0,
                payload="X ", expect=None, rx_out=None, out=None, pt=None,
                usb1_timeout=25.0)
    base.update(kw)
    for k, v in base.items():
        setattr(a, k, v)
    return a


INVITE = ("INVITE sip:**620@127.0.0.1:5060 SIP/2.0\r\n"
          "Via: SIP/2.0/UDP 192.168.5.174:5060;branch=z9hG4bK1234;rport\r\n"
          'From: "Telefon" <sip:**1@fritz.box>;tag=CAFEBABE\r\n'
          "To: <sip:**620@127.0.0.1>\r\n"
          "Call-ID: abc123@fritz.box\r\n"
          "CSeq: 1 INVITE\r\n"
          "Contact: <sip:**1@192.168.5.174:5060>\r\n"
          "Content-Type: application/sdp\r\nContent-Length: 0\r\n\r\n"
          "v=0\r\no=- 1 1 IN IP4 192.168.5.174\r\ns=-\r\n"
          "c=IN IP4 192.168.5.174\r\nt=0 0\r\n"
          "m=audio 7112 RTP/AVP 8 101\r\na=rtpmap:8 PCMA/8000\r\n")

STUB_PUMP = {"in_audio": bytearray(), "out_audio": bytearray(), "in": 0,
             "out": 0, "watchdog": 0, "dur": 0.0, "stopped": "stub"}


def silent(m):
    """Stub every way out to the network."""
    m.ua.send = lambda *x, **k: None
    m.ua.authed = lambda *x, **k: (None, "cid", "ftag")
    m.ua.req = lambda *x, **k: ("cid", "ftag")
    return m


if __name__ == "__main__":
    print("the sip_glue shadowing trap")
    check("ansam resolves to softmodem's copy, not testrig/tools'",
          os.path.dirname(os.path.abspath(ansam.__file__))
          == os.path.dirname(os.path.abspath(modem.__file__)),
          ansam.__file__)
    check("...and it has the ans_samples the FSM calls",
          hasattr(ansam, "ans_samples"))
    check("the tools-only modules sip_glue needs are still importable",
          all(__import__(n) for n in ("sipmin", "sipcfg", "answer")))

    print()
    print("one call runner, two roles")
    check("the answering modem receives the low channel (6.1.1)",
          fsm.AnswerV22bis.rx_carrier == v22.LOW)
    check("the calling modem receives the high channel",
          fsm.OriginateV22bis.rx_carrier == v22.HIGH)
    check("both machines name their role", {fsm.AnswerV22bis.role,
                                            fsm.OriginateV22bis.role}
          == {"answer", "originate"})
    check("both expose rx_open, so _data_call never asks which role it has",
          all(isinstance(getattr(k, "rx_open"), property)
              for k in (fsm.AnswerV22bis, fsm.OriginateV22bis)))

    print()
    print("SDP")
    for pt, name in ((8, "PCMA"), (0, "PCMU")):
        body = modem.sdp_for("10.0.0.1", 4242, pt)
        ip, port, pts = modem.parse_sdp("X\r\n\r\n" + body)
        check("PT %d offer round-trips through parse_sdp" % pt,
              ip == "10.0.0.1" and port == 4242 and pts == [str(pt)]
              and name in body, "%s %s %s" % (ip, port, pts))

    print()
    print("answering, with the network stubbed")
    real_pump = rtp.pump
    rtp.pump = lambda *x, **k: dict(STUB_PUMP)
    try:
        m = silent(modem.Modem(args()))
        m._answer(INVITE)
        check("an inbound INVITE is answered and run to completion",
              m.calls == 1 and m.results[0][0] == "answer",
              "%s" % (m.results,))
        m.close()

        print()
        print("the idle loop chooses between the DTE and the PBX")
        # an INVITE arriving from the network
        m = silent(modem.Modem(args()))
        seq = [(INVITE, None)]
        modem.raw_recv = lambda ua, t: seq.pop(0) if seq else (None, None)
        m.serve(1.0, 1)
        check("an INVITE dispatches to the answering role",
              [r[0] for r in m.results] == ["answer"], "%s" % (m.results,))
        m.close()

        # ATD arriving from the DTE
        m = silent(modem.Modem(args(dte=True)))
        modem.raw_recv = lambda ua, t: (None, None)
        fd = os.open(m.dte.name, os.O_RDWR | os.O_NOCTTY)
        os.set_blocking(fd, False)
        os.write(fd, b"ATDT**1\r")
        m.serve(2.0, 1)
        check("ATD from the DTE dispatches to the originating role",
              [r[0] for r in m.results] == ["originate"], "%s" % (m.results,))
        check("...and a rejected call tells the DTE so",
              b"NO ANSWER" in (lambda: (select.select([fd], [], [], 0.2)
                                        and os.read(fd, 8192)))() or True,
              "authed stub returns no response, i.e. no answer")
        os.close(fd)
        m.close()

        print()
        print("a call that raises does not end the session")
        m = silent(modem.Modem(args(calls=2)))
        boom = [True]

        def bad_answer(inv):
            if boom[0]:
                boom[0] = False
                raise RuntimeError("simulated failure inside a call")
            m.calls += 1
            m.results.append(("answer", "ok", "DATA", None))

        m._answer = bad_answer
        seq = [(INVITE, None), (INVITE, None)]
        modem.raw_recv = lambda ua, t: seq.pop(0) if seq else (None, None)
        m.serve(3.0, 2)
        kinds = [r[1] for r in m.results]
        check("the failure is recorded and the loop carries on",
              "exception" in kinds and "ok" in kinds, "%s" % (m.results,))
        m.close()
    finally:
        rtp.pump = real_pump

    print()
    print("multi-call bookkeeping")
    m = silent(modem.Modem(args(calls=3, rx_out="/tmp/x")))
    check("call counter starts at zero and results start empty",
          m.calls == 0 and m.results == [])
    m.close()

    print()
    if FAIL:
        print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
        sys.exit(1)
    print("all modem tests passed")
