"""Answer a call as **620 and run the answer-DCE side of V.8.

Milestone 2 form: emit a selectable ANSam variant and report what the calling
modem sends back, with particular attention to V.21(L) at 980/1180 Hz (which
would be signal CM) versus 1800 Hz (V.32 signal AA, meaning our tone was taken
for plain ANS).
"""
import argparse, math, random, re, socket, sys, time
import g711, g726, dsp, ansam, rtp, probes, fsm, v8, tracking, dte
from sip_glue import sipmin, raw_recv, resp_for, HOST, USER, PW

V21L_MARK, V21L_SPACE = 980.0, 1180.0     # call DCE -> us
V21H_MARK, V21H_SPACE = 1650.0, 1850.0    # us -> call DCE

def build_outbound(variant, level, ansam_s, lead_s=0.25, total_s=30.0, pt=8,
                   signal="ansam", carrier=2100.0, am_depth=None, ec_prefix=0.0):
    """Pre-render the outbound stream: lead-in silence, ANSam, then silence.

    V.8 8.2 requires no signal for at least 0.2 s after connecting to line.
    """
    lead = [0] * int(lead_s * 8000)
    if ec_prefix > 0:
        lead = lead + ansam.ec_disable_samples(ec_prefix, level_dbfs=level, f=carrier)
    if signal != "ansam":
        tone = probes.SIGNALS[signal](ansam_s, level_dbfs=level)
    else:
        kw = dict(ansam.VARIANTS[variant])
        kw["level_dbfs"] = level
        kw["f"] = carrier
        if am_depth is not None:
            kw["am_depth"] = am_depth
        tone = ansam.ansam_samples(ansam_s, **kw)
    tail = [0] * int(max(0.0, total_s - lead_s - ansam_s) * 8000)
    samples = lead + tone + tail
    if pt in (2, 102):
        # one encoder for the whole stream: G.726 is stateful
        return g726.Encoder().encode_frame(samples)
    return g711.encode(samples, pt)

def vbd_attrs(pt, enable):
    """ITU-T V.152 VBD signalling (RFC 3108 gpmd attribute).

    vbd=yes asks the gateway to treat the stream as voice-band data: no echo
    canceller, no VAD/silence suppression, no comfort noise, and no
    voice-optimised signal processing. silenceSupp:off is belt and braces.
    """
    if not enable:
        return ""
    return ("a=gpmd:%d vbd=yes;ecan=off\r\n"
            "a=silenceSupp:off - - - -\r\n" % pt)

def spectral_timeline(x, sr=8000, win=0.125, floor=250):
    rows = []
    W = int(win * sr)
    for i in range(0, len(x) - W + 1, W):
        seg = x[i:i + W]
        ms = dsp.mean_square(seg)
        if ms < floor:
            rows.append((i / sr, math.sqrt(ms), None, 0.0))
            continue
        f, p = dsp.dominant(seg, 300, 3000, coarse=25, fine=2, sr=sr)
        rows.append((i / sr, math.sqrt(ms), f, p / ms))
    return rows

def summarise(x, sr=8000):
    rows = spectral_timeline(x, sr)
    print("  %-7s %-8s %-8s %-7s" % ("t(s)", "RMS", "domHz", "purity"))
    prev = None
    for t, r, f, pur in rows:
        tag = "silence" if f is None else "%d" % f
        if tag != prev:
            print("  %-7.3f %-8.0f %-8s %-7.2f" % (t, r, tag, pur))
            prev = tag
    # Per-block band energies. A single Goertzel over the whole capture is
    # wrong here: 20 s of coherent integration is ~0.05 Hz wide, so a real tone
    # that drifts even 1 Hz averages away to nothing. Integrate per 125 ms
    # block (8 Hz wide) and take the strongest block.
    print("  --- band energies, per 125 ms block (peak / n blocks over 0.15) ---")
    W = 1000
    bands = (("V.21(L) mark   980", V21L_MARK), ("V.21(L) space 1180", V21L_SPACE),
             ("V.32 AA        1800", 1800.0), ("ANS/ANSam      2100", 2100.0),
             ("V.21(H) mark  1650", V21H_MARK), ("V.21(H) space 1850", V21H_SPACE))
    peak = {}
    for name, f in bands:
        best, hits = 0.0, 0
        for i in range(0, len(x) - W + 1, W):
            seg = x[i:i + W]
            ms = dsp.mean_square(seg)
            if ms < 250:
                continue
            r = dsp.goertzel(seg, f, sr) / ms
            best = max(best, r)
            if r > 0.15:
                hits += 1
        peak[name] = (best, hits)
        print("      %-20s peak=%.3f  blocks=%d" % (name, best, hits))
    v21l = max(peak["V.21(L) mark   980"][0], peak["V.21(L) space 1180"][0])
    v21l_blocks = peak["V.21(L) mark   980"][1] + peak["V.21(L) space 1180"][1]
    aa = peak["V.32 AA        1800"][0]
    print("  --- verdict ---")
    if v21l > 0.20 and v21l_blocks >= 2:
        print("      V.21(L) PRESENT (%d blocks) -> modem is sending CM. ANSam accepted." % v21l_blocks)
    elif aa > 0.20:
        print("      1800 Hz present (peak %.2f) -> V.32 signal AA: our tone read as plain ANS." % aa)
    else:
        print("      neither V.21(L) nor AA clearly present.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="noreversal", choices=sorted(ansam.VARIANTS))
    ap.add_argument("--level", type=float, default=-24.0, help="ANSam level dBFS")
    ap.add_argument("--ansam-s", type=float, default=5.0, help="ANSam duration (V.8: 5 +/- 1 s)")
    ap.add_argument("--lead", type=float, default=0.25,
                    help="silence before ANSam. V.8 8.2 requires >=0.2s; the call DCE is "
                         "itself silent for 1s (8.1.1), so a later start may be needed for "
                         "it to observe the tone onset")
    ap.add_argument("--seconds", type=float, default=25.0, help="call duration")
    ap.add_argument("--wait", type=float, default=60.0, help="seconds to wait for INVITE")
    ap.add_argument("--out", default=None, help="save inbound audio here")
    ap.add_argument("--save-tx", default=None,
                    help="save the outbound stream as actually transmitted, so what we "
                         "sent can be verified rather than assumed")
    ap.add_argument("--v22bis", action="store_true",
                    help="run the closed-loop V.22bis answer handshake and then send data")
    ap.add_argument("--dte", action="store_true",
                    help="expose a pseudo-terminal the DTE opens, and take the "
                         "transmitted data from it instead of --payload")
    ap.add_argument("--dte-wait", type=float, default=25.0,
                    help="seconds to wait for ATA when S0=0")
    ap.add_argument("--s0", type=int, default=1,
                    help="S0: rings before auto-answer; 0 waits for ATA")
    ap.add_argument("--live-rx", action="store_true", default=True,
                    help="decode the caller's data live in the RTP callback "
                         "(default on for --v22bis)")
    ap.add_argument("--no-live-rx", dest="live_rx", action="store_false")
    ap.add_argument("--expect", default=None,
                    help="repeating pattern the caller is expected to send; "
                         "scored live against what the receiver decodes")
    ap.add_argument("--rx-out", default=None,
                    help="write the live-decoded characters here")
    ap.add_argument("--idle", type=int, default=2,
                    help="extra mark bits between transmitted characters (8N1 = 0)")
    ap.add_argument("--payload", default="SLOPMODEM ",
                    help="bytes to transmit once in the 2400 bit/s data phase")
    ap.add_argument("--v32", action="store_true",
                    help="ANS-only path: send the plain V.25 answer tone and run the "
                         "V.32bis Annex A answer-side start-up (ceiling 14.4 kbit/s)")
    ap.add_argument("--v8", action="store_true",
                    help="run the full V.8 answer-DCE negotiation (ANSam -> CM -> JM -> CJ) "
                         "instead of just transmitting a probe")
    ap.add_argument("--modes", default="V.21",
                    help="modulation modes we declare available, for the JM intersection")
    ap.add_argument("--ec-prefix", type=float, default=0.0,
                    help="seconds of echo-canceller disabling tone (2100 Hz with 450 ms "
                         "phase reversals, per V.25 2.3 / G.168 7) to send before the ANSam")
    ap.add_argument("--am-depth", type=float, default=None,
                    help="ANSam AM depth. V.8 7.2 specifies 0.20 (envelope 0.8..1.2 x "
                         "average); higher is out of spec and purely diagnostic")
    ap.add_argument("--ansam-carrier", type=float, default=2100.0,
                    help="ANSam carrier frequency. V.8 7.2 specifies 2100 +/- 1 Hz, but the "
                         "PBX regenerator only grabs roughly 2100 +/- 5 Hz, so an offset "
                         "carrier may cross intact and still be accepted by the far end, "
                         "whose ANS detector is usually 2100 +/- 15 Hz")
    ap.add_argument("--pt", type=int, default=None,
                    help="payload type to answer with; must be in the caller's offer. "
                         "8=PCMA, 0=PCMU, 2 or 102=G.726-32")
    ap.add_argument("--vbd", action="store_true", help="add ITU-T V.152 voice-band-data signalling to our SDP "
                         "(a=gpmd:<pt> vbd=yes;ecan=off plus a=silenceSupp:off), which asks the "
                         "gateway to treat the stream as data and stop voice-optimised processing")
    ap.add_argument("--signal", default="ansam",
                    choices=["ansam"] + sorted(probes.SIGNALS),
                    help="probe signal to transmit instead of ANSam")
    a = ap.parse_args()

    if a.v22bis:
        # The V.22bis data call lives in modem.py, which does both roles. This
        # program stays as the workbench for the V.8, V.32 and tone experiments
        # that have no place in a modem, and hands the modem work over rather
        # than keeping a second copy of it.
        import modem
        argv = ["--calls", "1", "--max-call", "%g" % a.seconds,
                "--idle-seconds", "%g" % a.wait, "--level", "%g" % a.level,
                "--lead", "%g" % a.lead, "--idle", "%d" % a.idle,
                "--payload", a.payload, "--s0", "%d" % a.s0,
                "--dte-wait", "%g" % a.dte_wait]
        if a.dte:
            argv += ["--dte"]
        if a.pt is not None:
            argv += ["--pt", "%d" % a.pt]
        for opt, val in (("--expect", a.expect), ("--out", a.out),
                         ("--rx-out", a.rx_out)):
            if val:
                argv += [opt, val]
        return modem.main(argv)

    ua = sipmin.UA(HOST, USER, PW)
    r, _, _ = ua.authed("REGISTER", "sip:%s" % HOST, extra=("Expires: 300",))
    code = sipmin.status(r)[0] if r else None
    print("REGISTER -> %s  (contact %s:%d)" % (code, ua.lip, ua.lport), flush=True)
    if code != 200:
        return 1

    rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rs.bind(("0.0.0.0", 0)); rs.settimeout(0.25)
    rport = rs.getsockname()[1]
    totag = sipmin.rid(32)

    print("signal=%s variant=%s carrier=%.0f Hz depth=%s ecpre=%.1fs level=%.1f dBFS dur=%.1fs lead=%.2fs vbd=%s"
          % (a.signal, a.variant, a.ansam_carrier,
             ("%.2f" % a.am_depth) if a.am_depth is not None else "spec",
             a.ec_prefix, a.level, a.ansam_s, a.lead, a.vbd))
    print("waiting up to %.0fs for INVITE ..." % a.wait, flush=True)
    inv = None
    end = time.time() + a.wait
    while time.time() < end:
        msg, _ = raw_recv(ua, max(0.5, end - time.time()))
        if not msg:
            continue
        if msg.startswith("INVITE "):
            inv = msg
            print("INVITE from: %s" % (sipmin.hget(msg, "From") or "").strip(), flush=True)
            break
        if msg.startswith("OPTIONS "):
            ua.send(resp_for(msg, 200, "OK", ua, totag))
    if inv is None:
        print("no INVITE arrived")
        return 1

    body = inv.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in inv else ""
    print("  --- inbound SDP offer ---", flush=True)
    for ln in body.replace("\r", "").split("\n"):
        if ln.strip():
            print("    %s" % ln, flush=True)
    print("  ------------------------", flush=True)
    ip = re.search(r"c=IN IP4 ([\d.]+)", body)
    m = re.search(r"m=audio (\d+) RTP/AVP ([\d ]+)", body)
    rip = ip.group(1) if ip else None
    rpt = int(m.group(1)) if m else None
    pts = m.group(2).split() if m else []
    if a.pt is not None:
        if str(a.pt) not in pts:
            print("  payload type %d not offered (offer was %s)" % (a.pt, pts))
            return 1
        pt = a.pt
    else:
        pt = 8 if "8" in pts else 0
    G726_PTS = (2, 102)
    is726 = pt in G726_PTS
    frame_bytes = 80 if is726 else 160
    codec_name = "G726-32" if is726 else ("PCMA" if pt == 8 else "PCMU")
    print("  caller RTP %s:%s -> PT %d (%s), %d-byte frames"
          % (rip, rpt, pt, codec_name, frame_bytes), flush=True)

    ua.send(resp_for(inv, 100, "Trying", ua, totag))

    # The DTE interface, if asked for. It is created before the call is answered
    # because that is where it belongs in the sequence: V.250 has the modem
    # report RING and the DTE decide, either by ATA or by S0 counting rings.
    d = None
    if a.dte:
        d = dte.Dte(idle=a.idle, s0=a.s0)
        print("  DTE interface on %s  (screen %s 115200, or minicom -D %s)"
              % (d.name, d.name, d.name), flush=True)
        d.ring()
        if a.s0 == 0:
            print("  S0=0: waiting up to %.0f s for ATA" % a.dte_wait, flush=True)
            end = time.time() + a.dte_wait
            tick = 0
            while time.time() < end and not d.want_answer:
                d.poll()
                time.sleep(0.02)
                tick += 1
                if tick % 100 == 0:          # a fresh RING every 2 s
                    d.ring()
            if not d.want_answer:
                print("  no ATA - rejecting the call", flush=True)
                ua.send(resp_for(inv, 486, "Busy Here", ua, totag))
                d.close()
                return
            print("  ATA from the DTE - answering", flush=True)
        else:
            print("  S0=%d: auto-answering" % a.s0, flush=True)
        d.want_answer = False

    sdp = ("v=0\r\no=- %d 1 IN IP4 %s\r\ns=-\r\nc=IN IP4 %s\r\nt=0 0\r\n"
           % (random.getrandbits(30), ua.lip, ua.lip) +
           "m=audio %d RTP/AVP %d\r\na=rtpmap:%d %s/8000\r\n"
           % (rport, pt, pt, codec_name) +
           vbd_attrs(pt, a.vbd) + "a=sendrecv\r\n")
    ua.send(resp_for(inv, 200, "OK", ua, totag, sdp))
    print("  200 OK sent", flush=True)

    machine = None
    if a.v22bis:
        machine = fsm.AnswerV22bis(level_dbfs=a.level, lead=a.lead,
                                   payload=a.payload.encode(),
                                   idle=a.idle,
                                   tx_source=(d.tx.take if d else None),
                                   data_s=max(5.0, a.seconds - 6.0))
        seen = [0]
        # The live receiver runs in the callback. It is only fed once the
        # handshake reaches POST, because before that the caller is transmitting
        # S1 and SB1 at 1200 bit/s -- two bits per symbol on four points -- and
        # there is no 16-QAM constellation there to acquire. Acquisition is
        # measured rather than scheduled, so feeding it the tail of the 1200 bit/s
        # phase would be harmless anyway, just wasted.
        live = tracking.LiveRx() if a.live_rx else None
        report = [0]

        def on_frame(inbound):
            samples = g711.decode(inbound, pt) if inbound else []
            out = machine.step(samples)
            while len(machine.events) > seen[0]:
                t, st, msg = machine.events[seen[0]]
                print("  [%6.3f] %-8s %s" % (t, st, msg), flush=True)
                seen[0] += 1
            if d is not None:
                d.poll()
                if machine.state == fsm.DATA and not d.connected:
                    d.connect(2400)
            if live is not None and samples and machine.state in (fsm.POST, fsm.DATA):
                got = live.feed(samples)
                if d is not None and got:
                    d.deliver(got)
                if live.frames - report[0] >= 250:          # every 5 s
                    report[0] = live.frames
                    q = live.summary()
                    print("  rx   %6d chars | %d sym, acq@%d, %d retrain(s), "
                          "carrier %+.3f Hz | framing %d ok / %d err | "
                          "%.2f ms mean, %.1f ms worst"
                          % (q["chars"], q["symbols"], q["acquired_at"],
                             q["retrains"], q["carrier_hz"], q["framing_good"],
                             q["framing_bad"], q["mean_ms"], q["worst_ms"]),
                          flush=True)
            return g711.encode(out, pt)
    elif a.v32:
        machine = fsm.AnswerV32(level_dbfs=a.level, lead=a.lead)
        seen = [0]

        def on_frame(inbound):
            samples = g711.decode(inbound, pt) if inbound else []
            out = machine.step(samples)
            while len(machine.events) > seen[0]:
                t, st, msg = machine.events[seen[0]]
                print("  [%6.3f] %-8s %s" % (t, st, msg), flush=True)
                seen[0] += 1
            return g711.encode(out, pt)
    elif a.v8:
        modes = set(m.strip() for m in a.modes.split(",") if m.strip())
        machine = fsm.Answer(modes, level_dbfs=a.level, carrier=a.ansam_carrier,
                             lead=a.lead, ansam_max=min(a.seconds - 4.0, 20.0),
                             ec_prefix=a.ec_prefix, am_depth=a.am_depth)
        seen = [0]

        def on_frame(inbound):
            samples = g711.decode(inbound, pt) if inbound else []
            out = machine.step(samples)
            while len(machine.events) > seen[0]:
                t, st, msg = machine.events[seen[0]]
                print("  [%6.3f] %-8s %s" % (t, st, msg), flush=True)
                seen[0] += 1
            return g711.encode(out, pt)
    else:
        stream = build_outbound(a.variant, a.level, a.ansam_s, lead_s=a.lead,
                                total_s=a.seconds + 2, pt=pt, signal=a.signal,
                                carrier=a.ansam_carrier, am_depth=a.am_depth,
                                ec_prefix=a.ec_prefix)
        pos = [0]
        fill = bytes([g711.ALAW_SILENCE if pt == 8 else g711.ULAW_SILENCE])
        if is726:
            fill = b"\x00"

        def on_frame(_inbound):
            i = pos[0]
            pos[0] = i + frame_bytes
            chunk = stream[i:i + frame_bytes]
            if len(chunk) < frame_bytes:
                chunk = chunk + fill * (frame_bytes - len(chunk))
            return chunk

    def on_sip():
        ua.sock.settimeout(0.001)
        try:
            d, _ = ua.sock.recvfrom(65535)
            t = d.decode("utf-8", "replace")
            if t.startswith("BYE "):
                ua.send(resp_for(t, 200, "OK", ua, totag))
                print("  caller sent BYE", flush=True)
                return True
        except Exception:
            pass
        return False

    # For a modem stream the watchdog must be off. It emits an extra outbound
    # frame when the inbound stream goes quiet, which pushes 160 extra samples
    # -- 12 symbols, 48 bits at 2400 bit/s -- at the far end and slips its
    # character framing permanently. Strict 1:1 is the only safe pacing here.
    wd = 1e9 if (a.v22bis or a.v32) else 0.060
    def on_stop():
        if d is not None and d.want_hangup:
            return "dte-ath"
        return None

    st = rtp.pump(rs, (rip, rpt), pt, a.seconds, on_frame, on_sip=on_sip,
                  on_stop=on_stop, frame_bytes=frame_bytes, watchdog=wd)
    rtp.report(st)
    x = g711.decode(st["in_audio"], pt)
    if a.out:
        open(a.out, "wb").write(bytes(st["in_audio"]))
        print("  inbound audio -> %s" % a.out)
    if a.save_tx:
        open(a.save_tx, "wb").write(bytes(st["out_audio"]))
        print("  outbound audio (as sent) -> %s" % a.save_tx)
    if a.v22bis and machine is not None:
        print("  final state: %s" % machine.state)
        print("  caller's S1 seen: %s" % machine.s1_seen)
        if live is not None:
            q = live.summary()
            print("  *** LIVE RECEIVE (tracking receiver, in the RTP callback) ***")
            print("      symbols        : %d (%.1f s of line time)"
                  % (q["symbols"], q["symbols"] / 600.0))
            print("      acquired at    : symbol %d ; %d retrain(s)"
                  % (q["acquired_at"], q["retrains"]))
            print("      carrier offset : %+.3f Hz" % q["carrier_hz"])
            print("      framing        : %d characters, %d framing errors, "
                  "%d lock(s)" % (q["framing_good"], q["framing_bad"],
                                  live.dec.framer.locks))
            print("      characters out : %d" % q["chars"])
            print("      callback cost  : %.2f ms mean, %.1f ms worst, over %d "
                  "frames (20 ms budget)"
                  % (q["mean_ms"], q["worst_ms"], q["frames"]))
            if a.rx_out:
                open(a.rx_out, "wb").write(bytes(live.data))
                print("      characters -> %s" % a.rx_out)
            if d is not None:
                q = d.summary()
                print("  *** DTE INTERFACE ***")
                print("      device         : %s" % q["device"])
                print("      from the DTE   : %d bytes, framed onto the line as "
                      "%d characters" % (q["from_dte"], q["framed"]))
                print("      to the DTE     : %d bytes%s"
                      % (q["to_dte"],
                         "" if not q["dropped"] else
                         " (%d dropped: the DTE was not reading)" % q["dropped"]))
                print("      idle mark bits : %d (V.14 fill while the DTE had "
                      "nothing to send)" % q["idle_bits"])
                print("      still queued   : %d bytes" % q["queued"])
                print("      stopped by     : %s" % st.get("stopped"))
            if a.expect:
                import v22bis_track
                txt = bytes(live.data).decode("latin-1", "replace")
                frac, slips, at = v22bis_track.score_slip(txt, a.expect)
                print("      MATCH          : %.4f%% of %d characters, "
                      "%d slip%s" % (100 * frac, len(txt), slips,
                                     "" if slips == 1 else "s"))
                pr = "".join(c if 32 <= ord(c) < 127 else "." for c in txt[:72])
                print("      decoded        : %r" % pr)
        if d is not None:
            d.no_carrier()
            for _ in range(5):
                d.poll()
                time.sleep(0.02)
            d.close()
    elif a.v32 and machine is not None:
        print("  final state: %s" % machine.state)
        print("  1800 Hz activity seen for %.2f s" % machine.aa_seen_s)
        print("  incoming phase reversal detected: %s" % machine.rev_detected)
    elif machine is not None:
        print("  final state: %s ; JM sequences sent: %d"
              % (machine.state, machine.jm_sequences_sent))
        if machine.cm_parsed:
            print("  *** V.8 NEGOTIATION (answer DCE) ***")
            print("      CM received  : %s" % " ".join(machine.cm_parsed["raw"]))
            print("      call function: %s" % machine.cm_parsed["call_function"])
            print("      caller offers: %s" % machine.cm_parsed["modulations"])
            print("      JM sent      : %s" % " ".join(v8.parse_octets(machine.jm_octets)["raw"]))
            print("      agreed       : %s" % (machine.agreed or "NONE (8.2.3 all-zero JM)"))
        else:
            print("  no CM received")
    else:
        summarise(x)
    rs.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
