"""Offline tests for the V.32 §5.4 start-up, both sides against each other.

The two state machines are run frame by frame, each hearing only what the other
transmitted, and the sequence is checked against §5.4.1 and §5.4.2 clause by
clause. A timing error on one side then cannot hide behind a matching error on
the other.
"""
import sys
import dsp, dte, tracking, v32, v32fsm

FAIL = []


def known(name, cond, detail=""):
    """A check that is expected to fail, with the reason recorded.

    Nothing uses this at the moment -- the three cases that did were all fixed by
    the level gate in v32fsm._Base.step. It stays because the practice is worth
    keeping available: a defect that is measured and printed every run is very
    different from one that is commented out.

    Not counted as a failure, and not quietly dropped either: it prints every
    run so the defect stays visible.
    """
    print("  %-58s %s%s" % (name, "ok" if cond else "KNOWN FAIL",
                            ("  " + detail) if detail else ""))


def check(name, cond, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def at(events, needle):
    for t, s, m in events:
        if needle in m:
            return t
    return None


import math
import random as _r


def converged_at(m, blocks=24):
    """Seconds of data phase before the descrambled stream goes all-ones and
    stays there, or None. Both ends send continuous scrambled binary ones, so
    this measures how long the receiver takes to reconverge after the handshake.
    """
    b = m.rx.data_bits
    if len(b) < blocks * 100:
        return None
    n = len(b) // blocks
    first = None
    for i in range(blocks):
        blk = b[i * n:(i + 1) * n]
        if sum(blk) == len(blk):
            if first is None:
                first = i
        else:
            first = None
    if first is None:
        return None
    bps = 4 if m.rate == 9600 else 2
    return first * n / float(bps) / v32.BAUD


def loopback(frames=3000, ans_s=0.6, rates_a=(4800, 9600), rates_c=(4800, 9600),
             trellis_a=False, trellis_c=False, data_frames=700):
    ans = v32fsm.AnswerStartup(level_dbfs=-24.0, ans_s=ans_s, rates=rates_a,
                               trellis=trellis_a)
    org = v32fsm.OriginateStartup(level_dbfs=-24.0, rates=rates_c,
                                  trellis=trellis_c)
    to_o = to_a = [0] * 160
    done = None
    for k in range(frames):
        out_a = ans.step(to_a)
        out_o = org.step(to_o)
        to_a, to_o = out_o, out_a
        if ans.state == v32fsm.DATA and org.state == v32fsm.DATA:
            if done is None:
                done = k
            if k - done > data_frames:
                break
    return ans, org, done


if __name__ == "__main__":
    print("detectors, calibrated against generated signals")
    m = v32.Mod(level_dbfs=-24.0)
    sig = {"AA": m.modulate_states(["A"] * 200),
           "AC": v32.Mod(level_dbfs=-24.0).modulate_states(
               ["AC"[i % 2] for i in range(200)]),
           "S": v32.Mod(level_dbfs=-24.0).modulate_states(v32.s_states(200))}
    e = {k: v32fsm.energies(v[640:800]) for k, v in sig.items()}
    check("AA is a pure carrier: tone1800 only",
          v32fsm.is_tone1800(e["AA"]) and not v32fsm.is_pair(e["AA"])
          and not v32fsm.is_S(e["AA"]), "e1800 %.3f" % e["AA"][2])
    check("AC is the carrier-suppressed pair only",
          v32fsm.is_pair(e["AC"]) and not v32fsm.is_tone1800(e["AC"])
          and not v32fsm.is_S(e["AC"]),
          "e600 %.3f e3000 %.3f" % (e["AC"][1], e["AC"][3]))
    check("S has all three bands and is neither of the others",
          v32fsm.is_S(e["S"]) and not v32fsm.is_tone1800(e["S"])
          and not v32fsm.is_pair(e["S"]),
          "e600 %.3f e1800 %.3f e3000 %.3f"
          % (e["S"][1], e["S"][2], e["S"][3]))

    print()
    print("5.4.2: the answerer must not miss the caller's S")
    # The caller's S lasts a few hundred milliseconds and can land while the
    # answerer is still transmitting its own conditioning signal. R1TX used to
    # test is_S() against the current frame, so on that timing it never ceased
    # transmitting, never went looking for R2, and the far end reported NO
    # CARRIER. Which side of RC1/R1TX the S falls on is luck, so both are tested.
    Ssig = v32.Mod(level_dbfs=-24.0).modulate_states(v32.s_states(6000))
    ACsig = v32.Mod(level_dbfs=-24.0).modulate_states(
        ["AC"[i % 2] for i in range(6000)])

    def _fr(buf, k):
        i = 640 + (k % 25) * 160
        return buf[i:i + 160]

    def run_r1(early, defeat_latch=False):
        a = v32fsm.AnswerStartup(rates=(4800, 9600), trellis=False)
        a.mt = 192
        a._start_rc1()
        entered = None
        for k in range(1200):
            if early:
                x = _fr(Ssig, k) if a.state == v32fsm.RC1 else _fr(ACsig, k)
            else:
                x = _fr(Ssig, k)
            if defeat_latch:
                a.far_S = False
            a.step(x)
            if a.state == v32fsm.R1TX and entered is None:
                entered = a.tx.nsym
            if a.state == v32fsm.WAITMT:
                return k, entered, a
        return None, entered, a

    k1, _, _ = run_r1(early=False)
    check("S still present while R1 is going out: the answerer ceases",
          k1 is not None, "WAITMT at frame %s" % k1)
    k2, ent, a2 = run_r1(early=True)
    check("S that arrived during RC1 and stopped: it ceases anyway",
          k2 is not None, "WAITMT at frame %s" % k2)
    check("  and only after sending enough R1 for the far end to read it",
          k2 is not None and a2.tx.nsym - ent >= v32fsm.R1_MIN,
          "%dT of R1 sent, minimum %dT"
          % ((a2.tx.nsym - ent) if ent else -1, v32fsm.R1_MIN))
    k3, _, _ = run_r1(early=True, defeat_latch=True)
    check("  and it is the latch that does it, not chance",
          k3 is None, "without the latch: %s"
          % ("stuck in R1TX" if k3 is None else "reached WAITMT at %s" % k3))

    print()
    print("reversal detection by the amplitude null")
    errs = []
    for a in range(120, 300, 2):
        if a % 2:
            continue
        mm = v32.Mod(level_dbfs=-24.0)
        x = mm.modulate_states(["AC"[i % 2] for i in range(a)]
                               + ["CA"[i % 2] for i in range(150)])
        r = v32fsm.Reversal(600.0)
        hits = []
        for i in range(0, len(x) - 159, 160):
            r.armed = True
            hits += r.feed(x[i:i + 160])
        if hits:
            errs.append(hits[0] - a)
    check("every AC->CA reversal on an even boundary is found",
          len(errs) == len(range(120, 300, 2)),
          "%d of %d" % (len(errs), len(range(120, 300, 2))))
    check("and located to within a symbol, so 5.4's +/-2T is reachable",
          max(errs) - min(errs) <= 1.5,
          "offset %+.1f..%+.1f symbols" % (min(errs), max(errs)))
    # the parity requirement is not decoration
    odd = 0
    for a in (121, 137, 155):
        mm = v32.Mod(level_dbfs=-24.0)
        x = mm.modulate_states(["AC"[i % 2] for i in range(a)]
                               + ["CA"[i % 2] for i in range(150)])
        r = v32fsm.Reversal(600.0)
        h = []
        for i in range(0, len(x) - 159, 160):
            r.armed = True
            h += r.feed(x[i:i + 160])
        odd += len(h)
    check("5.4.2's 'even number of symbol intervals' matters: on an odd "
          "boundary AC..CA joins seamlessly", odd == 0,
          "%d reversals found on odd boundaries" % odd)
    # onset must not read as a reversal
    x = [0] * 1600 + v32.Mod(level_dbfs=-24.0).modulate_states(["A"] * 300)
    r = v32fsm.Reversal(1800.0)
    h = []
    for i in range(0, len(x) - 159, 160):
        r.armed = v32fsm.is_tone1800(v32fsm.energies(x[i:i + 160])) or r.armed
        h += r.feed(x[i:i + 160])
    check("the onset of a tone is not a reversal", len(h) == 0,
          "%d reported" % len(h))

    print()
    print("5.4 start-up, back to back")
    ans, org, done = loopback()
    for t, s, msg in ans.events:
        print("    ans  [%6.3f] %-7s %s" % (t, s, msg))
    for t, s, msg in org.events:
        print("    call [%6.3f] %-7s %s" % (t, s, msg))

    check("both sides reach the data phase",
          ans.state == v32fsm.DATA and org.state == v32fsm.DATA,
          "at %.2f s" % ((done or 0) * 0.02))
    check("5.4.1: the caller waits for the pair before transmitting",
          at(org.events, "600/3000 Hz pair detected") is not None)
    check("5.4.1: two phase reversals, and a round-trip estimate from them",
          at(org.events, "first phase reversal") is not None
          and at(org.events, "second phase reversal") is not None
          and org.nt is not None, "NT = %.0fT" % (org.nt or -1))
    check("5.4.2: the answerer estimates MT the same way",
          ans.mt is not None, "MT = %.0fT" % (ans.mt or -1))
    check("5.4.2: the amplitude drop is what starts the conditioning signal",
          at(ans.events, "amplitude drop") is not None)
    check("R1 -> R2 -> R3 all exchanged",
          at(ans.events, "R2 received") is not None
          and at(org.events, "R1 received") is not None
          and at(org.events, "R3 received") is not None)
    check("5.4.2: R3 selects within what R2 offered",
          at(ans.events, "R3: selecting") is not None)
    check("both settle on the same rate", ans.rate == org.rate == 9600,
          "answer %s, call %s" % (ans.rate, org.rate))
    check("both assert 107 and 109",
          ans.c107 and ans.c109 and org.c107 and org.c109)

    print()
    print("rate negotiation actually negotiates")
    ans2, org2, done2 = loopback(rates_a=(4800,), rates_c=(4800, 9600))
    check("an answerer offering only 4800 gets 4800",
          ans2.rate == org2.rate == 4800,
          "answer %s, call %s (states %s / %s)"
          % (ans2.rate, org2.rate, ans2.state, org2.state))
    ans3, org3, done3 = loopback(rates_a=(4800, 9600), rates_c=(4800,))
    check("a caller offering only 4800 also gets 4800",
          ans3.rate == org3.rate == 4800,
          "answer %s, call %s. This used to pick 9600: B5 and B6 are not among "
          "the seven sync bits, so a single bit error in R2 silently changed the "
          "advertised rates. Fixed by the level gate in step()."
          % (ans3.rate, org3.rate))
    check("both sides still agree on whatever rate they pick",
          ans3.rate == org3.rate, "answer %s, call %s" % (ans3.rate, org3.rate))

    print()
    print("2.4.1.2 trellis coding, negotiated and carried")

    def dataq(m, n=20000):
        """Both ends transmit continuous scrambled binary ones (5.4), so the
        descrambled data stream must be all ones. That makes the data phase
        self-checking without needing a payload."""
        b = m.rx.data_bits[-n:]
        z = m.rx.data_syms[-2000:]
        d = sorted(min(abs(v - p) for p in m.rx.mode.points) for v in z)
        return (100.0 * sum(b) / max(len(b), 1), d[len(d) // 2], len(m.rx.data_bits))

    at, ot, _ = loopback(trellis_a=True, trellis_c=True)
    check("B8 on both sides negotiates trellis coding at 9600",
          at.trellis and ot.trellis and at.rate == ot.rate == 9600,
          "answer %s/%s, call %s/%s" % (at.rate, at.trellis, ot.rate, ot.trellis))
    check("and both switch to the 32-point constellation",
          at._data_mode() is v32.TRELLIS9600
          and ot._data_mode() is v32.TRELLIS9600,
          "%s / %s" % (at._data_mode().name, ot._data_mode().name))
    qa, qo = dataq(at), dataq(ot)
    check("the answerer decodes the trellis data phase without error",
          qa[0] == 100.0, "%.2f%% ones over %d bits, median distance %.3f"
          % (qa[0], qa[2], qa[1]))
    check("and so does the caller", qo[0] == 100.0,
          "%.2f%% ones over %d bits, median distance %.3f" % (qo[0], qo[2], qo[1]))

    # B8 means "availability", so one side alone must not select it
    a1, o1, _ = loopback(trellis_a=True, trellis_c=False)
    check("trellis on one side only falls back to the nonredundant alternative",
          not a1.trellis and not o1.trellis and a1.rate == 9600,
          "answer %s, call %s" % (a1.trellis, o1.trellis))
    q1 = dataq(o1)
    check("and that fallback data phase decodes too",
          dataq(a1)[0] == 100.0 and q1[0] == 100.0,
          "answer %.2f%%, call %.2f%%" % (dataq(a1)[0], q1[0]))

    a2, o2, _ = loopback(rates_a=(4800,), trellis_a=True, trellis_c=True,
                         frames=4000, data_frames=1800)
    check("4800 never selects trellis -- B8 refers to the highest rate in B4-6, "
          "and we only have it at 9600",
          not a2.trellis and a2.rate == 4800, "rate %s trellis %s"
          % (a2.rate, a2.trellis))
    check("and the 4800 data phase ends up error free both ways",
          dataq(a2)[0] == 100.0 and dataq(o2)[0] == 100.0,
          "answer %.2f%%, call %.2f%%" % (dataq(a2)[0], dataq(o2)[0]))
    ca, co = converged_at(a2), converged_at(o2)
    check("the answerer is clean from the first block of the data phase",
          ca == 0.0, "converged at %s s" % ca)
    check("and so is the caller", co == 0.0,
          "converged at %s s (it used to need 15 s, entering data with an "
          "equaliser wrecked during the handshake)" % co)

    a3, o3, _ = loopback(trellis_a=False, trellis_c=False)
    check("with neither side offering trellis, the caller decodes 9600 "
          "nonredundant too", dataq(o3)[0] == 100.0,
          "%.2f%% ones, median distance %.3f" % (dataq(o3)[0], dataq(o3)[1]))
    check("the answerer is unaffected either way", dataq(a3)[0] == 100.0,
          "%.2f%% ones" % dataq(a3)[0])

    print()
    print("receiver conformance")
    # 2.1: "The receiver must be able to operate with received frequency offsets
    # of up to +/- 7 Hz." Injected honestly, by transmitting on an offset
    # carrier -- a held state A is a pure tone there, so the stimulus is
    # checkable before the result is believed.
    for d in (7.0, -7.0):
        mm = v32.Mod(level_dbfs=-24.0, fc=v32.CARRIER + d)
        tone = mm.modulate_states(["A"] * 1200)[800:]
        peak = max((dsp.goertzel(tone, f), f)
                   for f in [1780 + 0.25 * i for i in range(160)])[1]
        check("an offset carrier really is offset (%+.0f Hz)" % d,
              abs(peak - (v32.CARRIER + d)) < 0.3,
              "measured %.2f Hz" % peak)
    for who in ("answer", "call"):
        for d in (-7.0, 0.0, 7.0):
            ans = v32fsm.AnswerStartup(level_dbfs=-24.0, ans_s=0.6, trellis=True)
            org = v32fsm.OriginateStartup(level_dbfs=-24.0, trellis=True)
            # offset the *other* end, so the side under test sees the offset
            (org if who == "answer" else ans).mod.fc = v32.CARRIER + d
            to_a = to_o = [0] * 160
            for k in range(2200):
                oa = ans.step(to_a)
                oo = org.step(to_o)
                to_a, to_o = oo, oa
            m = ans if who == "answer" else org
            b = m.rx.data_bits[-20000:] if m.rx and m.rx.data_bits else []
            ones = 100.0 * sum(b) / max(len(b), 1)
            est = (m.rx.rx.c_freq * v32.BAUD / (2 * math.pi)) if m.rx else 0.0
            check("2.1: the %s side works at %+.1f Hz offset" % (who, d),
                  ones == 100.0 and m.rate == 9600,
                  "%s at %s, ones %.2f%%, loop reports %+.2f Hz"
                  % (m._data_mode().name, m.rate, ones, est))
    # Table 5, printed: 00->A 01->B 11->C 10->D, "signal states ... in Figure 1"
    check("Table 5's dibit-to-state map, and its note pointing at Figure 1",
          v32.TABLE5[(0, 0)] == "A" and v32.TABLE5[(0, 1)] == "B"
          and v32.TABLE5[(1, 1)] == "C" and v32.TABLE5[(1, 0)] == "D"
          and v32.ABCD["A"] == (-1, -1))
    # Note 3 to 5.4: a far end may precede its conditioning signal with an echo
    # canceller sequence, whose only defined property is spectral -- the three
    # 200 Hz bands at 600, 1800 and 3000 Hz at least 1 dB below the rest. Such a
    # signal must not be mistaken for S, the pair, or the 1800 Hz tone.
    _r.seed(5)
    ec = [_r.gauss(0, 3000) for _ in range(8000)]
    for f in (600.0, 1800.0, 3000.0):
        w = 2 * math.pi * f / 8000.0
        c = sum(v * math.cos(w * i) for i, v in enumerate(ec)) * 2.0 / len(ec)
        sn = sum(v * math.sin(w * i) for i, v in enumerate(ec)) * 2.0 / len(ec)
        for i in range(len(ec)):
            ec[i] -= c * math.cos(w * i) + sn * math.sin(w * i)
    hits = [0, 0, 0]
    for i in range(0, len(ec) - 160, 160):
        e = v32fsm.energies(ec[i:i + 160])
        hits[0] += v32fsm.is_S(e)
        hits[1] += v32fsm.is_pair(e)
        hits[2] += v32fsm.is_tone1800(e)
    check("Note 3 to 5.4: an echo-canceller training sequence reads as none of "
          "S, the pair or the 1800 Hz tone", hits == [0, 0, 0],
          "is_S %d, is_pair %d, is_tone1800 %d over %d frames"
          % tuple(hits + [(len(ec) - 160) // 160]))

    print()
    print("7.1.2 and 7.2: the V.14 converter")
    # Table 8/V.32: at 9600 the DTE may present 9600-9696 bit/s (basic) or
    # 9600-9821 (extended); at 4800, 4800-4848 and 4800-4910. Deleting one stop
    # bit takes a character from 10 bits to 9, so a deleted fraction p lets the
    # DTE run at 10/(10-p) times the line rate: the basic limit needs p = 0.1 and
    # the extended limit p = 0.226.
    _r.seed(4)
    payload = bytes(_r.randrange(32, 127) for _ in range(9000))
    for line, rate, label in ((9600, 9600, "9600, at the line rate"),
                              (9600, 9696, "9600, Table 8 basic limit"),
                              (9600, 9821, "9600, Table 8 extended limit"),
                              (4800, 4910, "4800, Table 8 extended limit")):
        enc = dte.AsyncEncoder(idle=0, delete_stops=True)
        fr = tracking.AsyncFramer()
        out = bytearray()
        fed = 0
        chunk = line // 20
        for nbits in range(0, 10 * len(payload) + 60000, chunk):
            want = int((nbits + chunk) * rate / (10.0 * line))
            while fed < want and fed < len(payload):
                enc.put(payload[fed:fed + 1])
                fed += 1
            out.extend(fr.feed(enc.take(chunk)))
        got = bytes(out)
        off = payload.find(got[:24]) if len(got) > 24 else -1
        n = len(got) - max(off, 0)
        exact = off >= 0 and payload[off:off + n] == got[:n]
        check("Table 8: %s" % label, exact and fed == len(payload),
              "%d in, %d out, %.1f%% of stop bits deleted, %d restored, "
              "%d resyncs" % (fed, len(got),
                              100.0 * enc.deleted / max(enc.chars, 1),
                              fr.restored, max(fr.locks - 1, 0)))
    # never two deletions in a row: the far framer reads a run of them as lost
    # framing, which is how it still catches a real slip
    enc = dte.AsyncEncoder(idle=0, delete_stops=True)
    enc.put(payload[:2000])
    bits = enc.take(20000)
    runs = 0
    i = 0
    while i + 9 < len(bits):
        if bits[i] == 0:
            if bits[i + 9] == 0:            # stop bit deleted
                if i + 18 < len(bits) and bits[i + 18] == 0:
                    runs += 1
                i += 9
            else:
                i += 10
        else:
            i += 1
    check("stop bits are never deleted twice in a row", runs == 0,
          "%d consecutive-deletion pairs, %.1f%% deleted overall"
          % (runs, 100.0 * enc.deleted / max(enc.chars, 1)))
    e1 = dte.AsyncEncoder(idle=0, delete_stops=True)
    e1.put(b"\x41")                       # 0x41 = 0100 0001
    frame = e1.take(10)
    want = [0] + [(0x41 >> k) & 1 for k in range(8)] + [1]
    check("7: one character is start + 8 data LSB-first + stop = 10 bits, "
          "inside 7's \"8, 9, 10 or 11 bits per character\"",
          frame == want and e1.take(4) == [1, 1, 1, 1],
          "%s, then mark" % "".join(str(b) for b in frame))

    # ... and the whole way through a V.32 call
    ans = v32fsm.AnswerStartup(level_dbfs=-24.0, ans_s=0.6, trellis=True)
    org = v32fsm.OriginateStartup(level_dbfs=-24.0, trellis=True)
    to_a = to_o = [0] * 160
    msg = b"AAA2BBB " * 200
    sa = so = 0
    ga = bytearray()
    go = bytearray()
    for k in range(2600):
        oa = ans.step(to_a)
        oo = org.step(to_o)
        to_a, to_o = oo, oa
        if ans.state == v32fsm.DATA and org.state == v32fsm.DATA:
            if sa < len(msg):
                ans.put(msg[sa:sa + 4])
                sa += 4
            if so < len(msg):
                org.put(msg[so:so + 4])
                so += 4
            ga.extend(ans.received())
            go.extend(org.received())
    for nm, sent, got in (("answer to call", sa, bytes(go)),
                          ("call to answer", so, bytes(ga))):
        i = got.find(b"AAA2BBB ")
        n = len(got) - i if i >= 0 else 0
        exact = i >= 0 and got[i:] == (b"AAA2BBB " * (n // 8 + 2))[:n]
        check("characters cross a 9600 trellis-coded call, %s" % nm,
              exact and n > 1000,
              "sent %d, received %d, pattern from byte %d" % (sent, len(got), i))
    check("and the far framer needed no resync to do it",
          ans.rx.framer.bad == 0 and org.rx.framer.bad == 0,
          "framing errors: answer %d, call %d"
          % (ans.rx.framer.bad, org.rx.framer.bad))

    print()
    print("V.32bis: negotiating and carrying all five rates")

    def bis_call(rates, frames=3000):
        """Both ends V.32bis-capable, both feeding the same pattern continuously
        once the data phase opens."""
        # "all five rates" means all five, so this opts past the gate
        ans = v32fsm.AnswerStartup(level_dbfs=-24.0, ans_s=0.6, rates=rates,
                                   bis=True, allow_14400=True)
        org = v32fsm.OriginateStartup(level_dbfs=-24.0, rates=rates, bis=True,
                                      allow_14400=True)
        to_a = to_o = [0] * 160
        pat = b"V32BIS! "
        ga = bytearray()
        go = bytearray()
        i = 0
        for k in range(frames):
            oa = ans.step(to_a)
            oo = org.step(to_o)
            to_a, to_o = oo, oa
            if ans.state == v32fsm.DATA and org.state == v32fsm.DATA:
                ans.put((pat + pat)[i % 8:i % 8 + 4])
                org.put((pat + pat)[i % 8:i % 8 + 4])
                i += 4
                ga.extend(ans.received())
                go.extend(org.received())
        return ans, org, bytes(ga), bytes(go)

    def tail_exact(got, pat=b"V32BIS! ", n=400):
        """How much of the last n bytes is the repeating pattern. The settling
        period is deliberately excluded: 104 is clamped until the eye opens, so
        characters offered before that are dropped rather than corrupted."""
        tail = got[-n:]
        if len(tail) < 100:
            return 0, len(got)
        i = tail.find(pat)
        if i < 0:
            return 0, len(got)
        exp = pat * (len(tail) // 8 + 2)
        k = 0
        while i + k < len(tail) and tail[i + k] == exp[k]:
            k += 1
        return k, len(got)

    # Three rates rather than five: the un-coded one, the smallest trellis set,
    # and the largest. 9600 and 12 000 are covered by v32bis_rates.py, which
    # sweeps all five -- a loopback call is about 25 seconds of simulated audio at
    # both ends and a test suite should not spend five minutes on it.
    for rates, rate, mode, bps in (
            ((4800,), 4800, "4800", 2),
            ((4800, 7200), 7200, "7200T", 3),
            ((4800, 7200, 9600, 12000, 14400), 14400, "14400T", 6)):
        an, og, ga, go = bis_call(rates)
        check("offering %s picks %d and both ends agree"
              % (list(rates), rate),
              an.rate == og.rate == rate and an.bis and og.bis,
              "answer %s/%s, call %s/%s" % (an.rate, an.bis, og.rate, og.bis))
        check("  constellation %s, %d data bits per symbol" % (mode, bps),
              an._data_mode().name == mode and an._data_bps() == bps
              and og._data_mode().name == mode,
              "%s at %d bits" % (an._data_mode().name, an._data_bps()))
        ka, na = tail_exact(go)
        kb, nb = tail_exact(ga)
        check("  characters cross both ways and the settled tail is exact",
              ka >= 380 and kb >= 380 and na > 2000 and nb > 2000,
              "answer->call %d B, last %d exact; call->answer %d B, last %d"
              % (na, ka, nb, kb))
        eyes = []
        for m in (an, og):
            P = m.rx.mode.points
            d = sorted(min(abs(v - p) for p in P)
                       for v in m.rx.data_syms[-3000:])
            eyes.append(d[len(d) // 2])
        mind = min(abs(an.rx.mode.points[i] - an.rx.mode.points[j])
                   for i in range(len(an.rx.mode.points))
                   for j in range(i + 1, len(an.rx.mode.points)))
        check("  and both eyes are well inside the decision radius %.3f"
              % (mind / 2), max(eyes) < mind / 4,
              "median distances %.3f and %.3f" % tuple(eyes))

    # V.32bis is negotiated by B4 and B8, so one end without it means V.32
    an, og, ga, go = bis_call((4800, 7200, 9600))
    an2 = v32fsm.AnswerStartup(level_dbfs=-24.0, ans_s=0.6,
                               rates=(4800, 7200, 9600), bis=True)
    og2 = v32fsm.OriginateStartup(level_dbfs=-24.0, rates=(4800, 9600),
                                  trellis=True)
    to_a = to_o = [0] * 160
    for k in range(2400):
        oa = an2.step(to_a)
        oo = og2.step(to_o)
        to_a, to_o = oo, oa
    check("one end without V.32bis falls back to V.32, per Note 1 to Table 5",
          not an2.bis and not og2.bis and an2.rate == og2.rate == 9600,
          "answer %s bis %s, call %s bis %s"
          % (an2.rate, an2.bis, og2.rate, og2.bis))
    b = an2.rx.data_bits[-20000:]
    check("  and that fallback still carries data",
          sum(b) == len(b), "ones %.2f%%" % (100.0 * sum(b) / max(len(b), 1)))

    print()
    print("5.5 retrain")

    def retrain_run(side, at=6.0, frames=1400, damage=None):
        """Loopback with a retrain forced at `at`, or with the link damaged."""
        ans = v32fsm.AnswerStartup(level_dbfs=-24.0, ans_s=0.6, trellis=True)
        org = v32fsm.OriginateStartup(level_dbfs=-24.0, trellis=True)
        to_a = to_o = [0] * 160
        fired = False
        marks = []
        mid = {}
        for k in range(frames):
            t = k * 0.02
            oa = ans.step(to_a)
            oo = org.step(to_o)
            to_a, to_o = oo, oa
            if damage and damage[0] <= t < damage[1]:
                # Wreck one direction so the answerer's receiver stops making
                # sense of the caller -- what 5.5 calls unsatisfactory signal
                # reception. It has to be *noise*: a level change is not
                # damage, the equaliser simply follows it, and an 18 dB cut
                # produced no retrain at all.
                rms = (sum(v * v for v in to_a) / len(to_a)) ** 0.5 or 1.0
                to_a = [int(v + _r.gauss(0, 0.8 * rms)) for v in to_a]
            if ans.state == v32fsm.DATA and org.state == v32fsm.DATA and not marks:
                marks.append(t)
            if side and not fired and marks and t >= at:
                m = ans if side == "answer" else org
                if m.state == v32fsm.DATA:
                    (m._retrain_answer if side == "answer"
                     else m._retrain_call)("forced by the test")
                    fired = True
                    mid = {"c106": m.c106, "c107": m.c107, "c109": m.c109,
                           "clamp104": m.clamp104}
            if (fired or damage) and len(marks) == 1 \
                    and ans.state == v32fsm.DATA and org.state == v32fsm.DATA \
                    and (ans.retrains or org.retrains):
                marks.append(t)
        return ans, org, marks, mid

    for side in ("answer", "call"):
        an, og, mk, mid = retrain_run(side)
        back = mk[1] if len(mk) > 1 else None
        check("a retrain initiated by the %s side gets back to the data phase"
              % side, back is not None,
              "data at %.2f s, retrain at 6.00 s, data again at %s"
              % (mk[0], ("%.2f s" % back) if back else "never"))
        check("  and both ends renegotiate 9600 with trellis coding",
              an.rate == og.rate == 9600 and an.trellis and og.trellis,
              "%s/%s, trellis %s/%s" % (an.rate, og.rate, an.trellis, og.trellis))
        check("  and the far end joins the retrain off the carrier state alone, "
              "per 5.5.%s" % ("2" if side == "call" else "1"),
              an.retrains == 1 and og.retrains == 1,
              "retrains %d/%d" % (an.retrains, og.retrains))
        ba = an.rx.data_bits[-20000:]
        bo = og.rx.data_bits[-20000:]
        check("  and the data phase is error free again",
              sum(ba) == len(ba) and sum(bo) == len(bo),
              "ones %.2f%% / %.2f%%" % (100.0 * sum(ba) / max(len(ba), 1),
                                        100.0 * sum(bo) / max(len(bo), 1)))
        check("  5.5: 106 off and 104 clamped during the retrain, 107 and 109 "
              "left on", mid.get("c106") is False and mid.get("clamp104") is True
              and mid.get("c107") and mid.get("c109"), "%s" % mid)
        check("  and 106 back on with 104 unclamped once it completes",
              an.c106 and og.c106 and not an.clamp104 and not og.clamp104)

    # 5.4.2's even-parity rule again, this time on the retrain's AC segment
    an, og, mk, _ = retrain_run("answer")
    seg = [m for t, st, m in an.events if "of AC" in m]
    check("the retrain's AC segment is measured from the segment, not the "
          "absolute symbol counter", len(seg) >= 2,
          "%d AC segments logged" % len(seg))

    # the self-trigger: 5.5 allows a retrain on "unsatisfactory signal reception"
    _r.seed(31)
    an, og, mk, _ = retrain_run(None, frames=2600, damage=(6.0, 9.0))
    why = [m for t, st, m in an.events if "5.5 retrain" in m]
    check("a damaged link makes the answerer call its own retrain",
          bool(why), why[0] if why else "no retrain initiated")
    check("  and it recovers once the damage stops",
          an.state == v32fsm.DATA and og.state == v32fsm.DATA
          and an.rate == 9600,
          "answer %s at %s, call %s" % (an.state, an.rate, og.state))
    ba = an.rx.data_bits[-20000:]
    check("  with the data phase error free after it",
          len(ba) > 1000 and sum(ba) == len(ba),
          "%d bits, ones %.2f%%" % (len(ba), 100.0 * sum(ba) / max(len(ba), 1)))

    print()
    print("the level gate: not adapting while the far end is silent")
    # the defect: the far end stops transmitting several times in 5.4, and the
    # frame in which it stops is part signal and part nothing
    r = v32fsm._Rx(v32.Scrambler.GPC)
    m = v32fsm.OriginateStartup(level_dbfs=-24.0)
    m.rx, m.listening, m.saw_S = r, True, True
    mm = v32.Mod(level_dbfs=-24.0)
    live = mm.modulate_states(v32.trn_states(1400, mm.scr.taps))
    for i in range(0, 1600, 160):
        m.step(live[i:i + 160])
    ref = m.sig_ref
    check("a run of live frames establishes a level reference",
          ref is not None and ref > 1000.0, "reference %.0f" % (ref or 0))
    frozen_live = r.rx.frozen
    # one frame that is 10% signal and 90% silence -- what the old absolute
    # floor of 100 let straight through
    m.step(live[1600:1616] + [0] * 144)
    check("a frame where the signal stops partway is frozen out", r.rx.frozen,
          "energy ratio %.4f against the 0.5 gate"
          % (v32fsm.energies(live[1600:1616] + [0] * 144)[0] / ref))
    check("while genuinely live frames are not", not frozen_live)
    m.step([v * 3 for v in live[1760:1920]])
    check("and a level *rise* is allowed through -- the data phase is 7 dB "
          "hotter than the handshake", not r.rx.frozen)

    print()
    if FAIL:
        print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
        sys.exit(1)
    print("all V.32 start-up tests passed")
