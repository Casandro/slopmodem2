"""Offline tests for the DTE-side interface. No hardware, no SIP.

Three layers get exercised separately, because they fail in different ways:
the V.14 transmit converter (bits), the whole transmit-to-receive chain through
our own modem (characters), and the pseudo-terminal with its V.250 command
interpreter (a real character device, opened the way a DTE would open it).
"""
import os, select, sys, time
import dte, fsm, tracking, v22, v22bis

FAIL = []


def check(name, cond, detail=""):
    print("  %-54s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def slave_open(d):
    fd = os.open(d.name, os.O_RDWR | os.O_NOCTTY)
    os.set_blocking(fd, False)
    return fd


def slave_read(fd, want_s=0.25):
    """Everything the modem has written, up to a short deadline."""
    got = bytearray()
    end = time.time() + want_s
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.02)
        if r:
            try:
                got.extend(os.read(fd, 4096))
            except (BlockingIOError, OSError):
                pass
        else:
            if got:
                break
    return bytes(got)


def talk(d, fd, line, ticks=4):
    os.write(fd, line)
    for _ in range(ticks):
        d.poll()
        time.sleep(0.005)
    return slave_read(fd)


if __name__ == "__main__":
    print("V.14 transmit converter")
    e = dte.AsyncEncoder(idle=2)
    e.put(b"A")
    b = e.take(12)
    check("8N1 framing: start, eight LSB-first, stop",
          b == [0, 1, 0, 0, 0, 0, 0, 1, 0, 1, 1, 1],
          "0x41 -> %s" % "".join(str(v) for v in b))
    check("two idle mark bits follow the stop bit", b[-3:] == [1, 1, 1])
    e2 = dte.AsyncEncoder()
    got = e2.take(40)
    check("starved encoder emits mark, not silence", got == [1] * 40,
          "%d idle bits counted" % e2.idled)
    e3 = dte.AsyncEncoder(idle=2)
    e3.put(b"Hi")
    n = len(e3.take(80))
    check("take(n) always returns exactly n bits", n == 80, "%d" % n)
    check("characters framed and idle bits both counted",
          e3.chars == 2 and e3.idled == 80 - 24,
          "%d chars, %d idle bits" % (e3.chars, e3.idled))

    print()
    print("encoder into the framer, bit for bit")
    payload = bytes(range(256)) * 3
    enc = dte.AsyncEncoder(idle=2)
    enc.put(payload)
    fr = tracking.AsyncFramer()
    out = bytearray()
    while enc.pending() or enc.bits:
        out.extend(fr.feed(enc.take(48)))
    out.extend(fr.feed(enc.take(400)))          # flush through the idle tail
    # the framer drops its first frame as unconfirmed, so compare from offset 1
    check("every byte value survives the round trip",
          bytes(out) == payload[1:1 + len(out)],
          "%d of %d bytes, framing errors %d"
          % (len(out), len(payload), fr.bad))
    check("all 256 byte values were exercised",
          len(set(payload)) == 256 and len(out) > 700, "%d bytes" % len(out))

    print()
    print("whole chain: DTE bytes -> line -> characters back")
    # Exactly the code path fsm's DATA state runs: 48 bits per 20 ms frame
    # through one continuous modulator.
    pat = b"SOFT2MODEM "
    enc = dte.AsyncEncoder(idle=2)
    enc.put(pat * 700)
    m = v22bis.Mod("low", level_dbfs=-18.0)
    live = tracking.LiveRx()
    for _ in range(3000):                       # 60 s of frames
        live.feed(m.modulate(enc.take(48), scramble=True))
    got = bytes(live.data)
    hits = max(sum(1 for k, c in enumerate(got) if c == pat[(k + p) % len(pat)])
               for p in range(len(pat))) if got else 0
    check("characters recovered", len(got) > 5000, "%d characters" % len(got))
    check("every character correct", got and hits == len(got),
          "%d/%d, %d framing errors" % (hits, len(got), live.dec.framer.bad))
    check("the encoder ran dry and filled with mark, as V.14 requires",
          enc.idled > 0, "%d characters framed, %d idle bits"
          % (enc.chars, enc.idled))

    print()
    print("the FSM reaches its streaming data phase")
    enc2 = dte.AsyncEncoder(idle=2)
    enc2.put(b"X" * 4000)
    machine = fsm.AnswerV22bis(level_dbfs=-18.0, lead=0.04, ans_s=0.2,
                               tx_source=enc2.take)
    caller = v22bis.Mod("low", level_dbfs=-18.0)
    s1 = caller.modulate(v22.s1_bits(int(0.12 * 1200)), scramble=False, bps=2)
    quiet = [0] * fsm.FRAME
    seq = []
    for _ in range(80):                          # ANS + USB1 running
        seq.append(quiet)
    for i in range(0, len(s1) - fsm.FRAME + 1, fsm.FRAME):
        seq.append(s1[i:i + fsm.FRAME])
    for _ in range(200):
        seq.append(quiet)
    out_samples = 0
    for frame in seq:
        out_samples += len(machine.step(frame))
    check("state machine ends in DATA", machine.state == fsm.DATA,
          "state %s after %.2f s" % (machine.state, out_samples / 8000.0))
    check("data-phase bits were pulled from the encoder",
          machine.data_bits > 0 and enc2.chars > 0,
          "%d bits generated, %d characters framed"
          % (machine.data_bits, enc2.chars))

    print()
    print("pseudo-terminal and V.250")
    d = dte.Dte(s0=1)
    check("a character device exists for the DTE to open",
          d.name.startswith("/dev/pts/") and os.path.exists(d.name), d.name)
    fd = slave_open(d)
    r = talk(d, fd, b"AT\r")
    check("bare AT is answered OK", b"OK" in r, repr(r))
    r = talk(d, fd, b"ATE0\r")
    check("ATE0 turns the echo off", b"OK" in r, repr(r))
    r = talk(d, fd, b"ATI1\r")
    check("ATI1 reports the line rate", b"2400" in r, repr(r))
    r = talk(d, fd, b"AT&V\r")
    check("AT&V reports the active profile", b"S0:" in r and b"S12:" in r, repr(r))
    r = talk(d, fd, b"ATS0=0\r")
    r = talk(d, fd, b"ATS0?\r")
    check("S-registers are written and read back", b"000" in r, repr(r))
    check("...and the value actually changed", d.s[0] == 0, "S0=%d" % d.s[0])
    r = talk(d, fd, b"ATE0Q0V1S0=1\r")
    check("concatenated commands on one line", b"OK" in r and d.s[0] == 1, repr(r))
    # ATZZZ would be legal -- Z three times -- so pick something we really do
    # not implement: %C is the Cirrus's compression control (see modem.md).
    r = talk(d, fd, b"AT%C1\r")
    check("an unimplemented command gives ERROR", b"ERROR" in r, repr(r))
    r = talk(d, fd, b"ATDT12345\r")
    check("dialling is refused - this modem answers", b"ERROR" in r, repr(r))
    r = talk(d, fd, b"AT+GMI\r")
    check("V.250 identification works", b"slopmodem" in r, repr(r))

    print()
    print("answer, data mode, escape and hang up")
    d.ring()
    r = slave_read(fd)
    check("RING reaches the DTE", b"RING" in r, repr(r))
    r = talk(d, fd, b"ATA\r")
    check("ATA is noted for the line side", d.want_answer, repr(r))
    d.want_answer = False
    d.connect(2400)
    r = slave_read(fd)
    check("CONNECT 2400 is reported", b"CONNECT 2400" in r, repr(r))
    check("the modem is now in data mode", d.online)
    os.write(fd, b"hello line")
    d.poll()
    check("DTE data reaches the transmit queue", d.tx.pending() == 10,
          "%d bytes queued" % d.tx.pending())
    d.deliver(b"hello DTE")
    r = slave_read(fd)
    check("line data reaches the DTE verbatim", r == b"hello DTE", repr(r))
    # V.250 5.2.3: idle, three S2 characters, idle
    for _ in range(60):
        d.poll()
    os.write(fd, b"+++")
    d.poll()
    for _ in range(60):
        d.poll()
    r = slave_read(fd)
    check("+++ escapes to command mode with the call up",
          not d.online and d.connected and b"OK" in r, repr(r))
    r = talk(d, fd, b"ATO\r")
    check("ATO returns to data mode", d.want_online, repr(r))
    d.want_online = False
    d.online = True
    r = talk(d, fd, b"+++")
    for _ in range(60):
        d.poll()
    slave_read(fd)
    r = talk(d, fd, b"ATH\r")
    check("ATH is noted for the line side", d.want_hangup, repr(r))
    d.no_carrier()
    r = slave_read(fd)
    check("NO CARRIER is reported", b"NO CARRIER" in r, repr(r))

    print()
    print("back-pressure instead of circuit 106")
    d2 = dte.Dte()
    fd2 = slave_open(d2)
    d2.online = True
    d2.connected = True
    d2.tx.put(b"z" * d2.hiwater)
    os.write(fd2, b"more data")
    d2.poll()
    check("the modem stops reading once its queue is full",
          d2.tx.pending() == d2.hiwater,
          "%d queued, high-water %d" % (d2.tx.pending(), d2.hiwater))
    d2.tx.take(8 * d2.hiwater)                  # drain
    d2.poll()
    check("...and resumes when the queue drains", d2.tx.pending() > 0,
          "%d queued" % d2.tx.pending())
    os.close(fd2)
    d2.close()
    os.close(fd)
    d.close()

    print()
    if FAIL:
        print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
        sys.exit(1)
    print("all DTE tests passed")
