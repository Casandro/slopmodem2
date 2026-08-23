"""A DTE. Opens the soft-modem's serial port, waits for CONNECT, exchanges data.

Deliberately written as an ordinary serial program that knows nothing about the
modem's internals: it opens a character device, sets it raw, talks V.250 at it,
watches for result codes, then reads and writes bytes. The same script would work
against the hardware modems on the Pi.

usage: dtechat.py DEVICE SECONDS PATTERN EXPECT [SETUP;...] [DIALNUMBER]

With a dial number it originates (ATD); without one it waits for RING and
answers (ATA).
"""
import os, select, sys, termios, time, tty


class Port:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        try:
            tty.setraw(self.fd)
        except termios.error:
            pass
        os.set_blocking(self.fd, False)
        self.buf = bytearray()

    def write(self, data):
        n = 0
        while n < len(data):
            r, w, _ = select.select([], [self.fd], [], 1.0)
            if not w:
                break
            n += os.write(self.fd, data[n:])
        return n

    def read(self, timeout=0.05):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            return os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
            return b""

    def expect(self, tokens, timeout):
        """Read until one of `tokens` appears. Returns (token, text)."""
        end = time.time() + timeout
        got = bytearray()
        while time.time() < end:
            got.extend(self.read(0.05))
            for t in tokens:
                if t in got:
                    return t, bytes(got)
        return None, bytes(got)

    def command(self, cmd, timeout=2.0):
        self.write(cmd.encode() + b"\r")
        tok, txt = self.expect((b"OK", b"ERROR"), timeout)
        return (tok or b"TIMEOUT").decode(), txt.decode("latin-1", "replace").strip()

    def close(self):
        os.close(self.fd)


def score(data, expect):
    """Best match of `data` against a repeating pattern, and the slip count."""
    if not data:
        return 0.0, 0, 0
    n = len(expect)
    ph = max(range(n), key=lambda p: sum(
        1 for i, c in enumerate(data) if c == expect[(i + p) % n]))
    ok, slips, i = 0, 0, 0
    while i < len(data):
        if data[i] == expect[(i + ph) % n]:
            ok += 1
            i += 1
            continue
        found = None
        m = min(40, len(data) - i)
        if m >= 20:
            for sh in range(1, n):
                if all(data[i + k] == expect[(i + k + ph + sh) % n]
                       for k in range(m)):
                    found = sh
                    break
        if found is not None:
            ph = (ph + found) % n
            slips += 1
        i += 1
    return ok / float(len(data)), slips, ok


if __name__ == "__main__":
    path, secs = sys.argv[1], float(sys.argv[2])
    pattern = sys.argv[3].encode()
    expect = sys.argv[4].encode()
    setup = [c for c in (sys.argv[5].split(";") if len(sys.argv) > 5 else []) if c]
    dial = sys.argv[6] if len(sys.argv) > 6 else None

    p = Port(path)
    print("opened %s" % path, flush=True)
    for c in setup:
        code, txt = p.command(c)
        print("  %s -> %s %r" % (c, code, txt[:60]), flush=True)

    if dial:
        # Originating. V.250 answers ATD with a result code, not with OK, so
        # wait for one of those rather than for the command to "complete".
        print("dialling %s" % dial, flush=True)
        p.write(("ATD%s\r" % dial).encode())
        tok, txt = p.expect((b"CONNECT", b"NO CARRIER", b"BUSY", b"NO ANSWER"),
                            90.0)
        print("dial -> %s: %r" % (tok, txt[-40:] if txt else b""), flush=True)
    else:
        # Answering. If the modem was configured to auto-answer we will see
        # CONNECT without having to send ATA at all.
        tok, txt = p.expect((b"RING", b"CONNECT"), 30.0)
        print("saw %s: %r" % (tok, txt[-40:] if txt else b""), flush=True)
        if tok == b"RING":
            p.write(b"ATA\r")
        if tok != b"CONNECT":
            tok, txt = p.expect((b"CONNECT", b"NO CARRIER"), 40.0)
            print("then %s: %r" % (tok, txt[-40:] if txt else b""), flush=True)
    if tok != b"CONNECT":
        print("no connection", flush=True)
        p.close()
        sys.exit(1)

    # Data mode. Pace just under the line rate: 2400 bit/s with 12 bits per
    # character is 200 characters a second, and feeding at exactly the line rate
    # leaves the modem's transmit buffer no margin.
    time.sleep(0.3)
    rx = bytearray()
    sent = 0
    t0 = time.time()
    rate = 190.0
    while time.time() - t0 < secs:
        p.write(pattern)
        sent += len(pattern)
        nxt = t0 + sent / rate
        while time.time() < nxt - 0.002:
            rx.extend(p.read(0.002))
    el = time.time() - t0
    for _ in range(20):
        rx.extend(p.read(0.02))
    print("sent %d bytes in %.1f s (%.0f byte/s)" % (sent, el, sent / el), flush=True)
    print("received %d bytes; first 100: %r"
          % (len(rx), bytes(rx[:100]).decode("latin-1", "replace")), flush=True)
    frac, slips, ok = score(bytes(rx), expect)
    print("MATCH %d/%d = %.4f%% correct against %r, %d slip%s"
          % (ok, len(rx), 100 * frac, expect, slips, "" if slips == 1 else "s"),
          flush=True)

    # Escape to command mode with the call still up, then hang up. V.250 5.2.3
    # wants an idle period either side of the three plusses.
    time.sleep(1.2)
    p.write(b"+++")
    tok, txt = p.expect((b"OK",), 3.0)
    print("escape -> %s %r" % (tok, txt[-20:] if txt else b""), flush=True)
    code, txt = p.command("AT&V", 2.0)
    print("AT&V while online -> %s %r" % (code, txt[:70]), flush=True)
    p.write(b"ATH\r")
    tok, txt = p.expect((b"OK", b"NO CARRIER"), 5.0)
    print("ATH -> %s %r" % (tok, txt[-30:] if txt else b""), flush=True)
    p.close()
    print("done", flush=True)
