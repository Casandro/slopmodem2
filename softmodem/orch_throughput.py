"""Throughput both ways at whatever rate the modem picks, from a clean reset.

The measurement this project's rules ask for, as one command: reset a hardware
modem to factory, let it negotiate any V.32bis rate it likes, dial us, and carry
printable ASCII in both directions at once for a minute. Reports the negotiated
line rate and the octets per second each way.

Two things about "all speeds available" are worth stating, because both were
found the hard way and neither is obvious from the AT command.

**Automode stays on.** `AT+MS=V32B,0,...` reads back as a range and does not
behave as one -- with automode 0 the Cirrus advertises `max` and nothing else, so
the "range" is one rate written in four fields. With automode 1 it answers R2 with
the real intersection and 5.4's rate negotiation does its job. The carrier is
still pinned to V32B, because left to choose freely both modems pick V.34 or V.90
and never reach 5.4 at all.

**The Conexant's +MS has six fields**, `<carrier>,<automode>,<min_tx>,<max_tx>,
<min_rx>,<max_rx>`. Setting the first four caps transmit only and leaves receive
at 14400, so it advertises rates it has been told not to use. Both forms are sent;
each modem errors on the one it does not know, which is expected and harmless.

  python3 orch_throughput.py --port /dev/ttyUSB0 --dial ATDT      # Cirrus, **1
  python3 orch_throughput.py --port /dev/ttyACM0 --dial ATD       # Conexant, **2
"""
import argparse, re, subprocess, sys, time

PI = "raspberrypi"

# Dialled on the Pi. The ASCII pattern is built here rather than passed in:
# 33..126 contains quotes, backslashes, dollars and semicolons, and this script
# is handed to a remote shell.
DIAL = r'''
import serial, sys, time
port, number, secs, dialcmd = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
auto = sys.argv[5] if len(sys.argv) > 5 else "1"
factory = (sys.argv[6] != "0") if len(sys.argv) > 6 else True
top = sys.argv[7] if len(sys.argv) > 7 else "14400"
PAT = bytes(range(33, 127))          # printable ASCII, 94 octets
s = serial.Serial(port, 115200, timeout=0.2, rtscts=False)

# A fresh reset every time: DTR down, hang up, ATZ, then AT&F. Back-to-back
# calls otherwise leave the port in a state where every command returns ERROR.
s.dtr = False; time.sleep(0.8); s.dtr = True; time.sleep(0.5)
s.reset_input_buffer()

def cmd(c, wait=1.0):
    s.write((c + "\r").encode()); time.sleep(wait)
    r = s.read(4000).decode("ascii", "replace")
    print("  %-34s -> %s" % (c, r.replace("\r", " ").strip()[:120]), flush=True)
    return r

cmd("ATH0"); cmd("ATZ")
if factory: cmd("AT&F")
cmd("ATE0V1X4")
# Error correction on: V.42 carries more than V.14 (no start/stop bits) and both
# modems default to it anyway. The two command sets do not overlap, so ERRORs on
# the foreign half are expected -- what matters is that one of them lands.
for c in ("AT&K3", "ATW1", "AT&Q5", "ATS48=7", "ATS46=136",
          "AT\\N3", "AT%C0", 'AT"H0', "AT\\A0"):
    cmd(c, 0.6)
# every V.32bis rate, negotiated rather than pinned; see the module docstring
cmd("AT+MS=V32B,%s,4800,%s" % (auto, top))
cmd("AT+MS=V32B,%s,4800,%s,4800,%s" % (auto, top, top))
rd = cmd("AT+MS?")
print("MS-READBACK %s" % rd.replace("\r", " ").strip(), flush=True)

s.rtscts = True                      # &K3 above; a flood overruns the port without it
t0 = time.time()
con = cmd(dialcmd + number, 2.0)
buf = con
connected = "CONNECT" in con
sent = 0
got = bytearray()
first = None
last = None
i = 0
while time.time() - t0 < secs:
    n = s.in_waiting
    d = s.read(n) if n else b""
    if d:
        buf += d.decode("ascii", "replace")
        if connected:
            if first is None:
                first = time.time()
            last = time.time()
            got.extend(d)
        elif "CONNECT" in buf:
            connected = True
    if connected:
        # keep the line full; RTS/CTS decides the real rate
        blk = (PAT * 6)[i:i + 512]
        i = (i + 512) % len(PAT)
        sent += s.write(blk)
    time.sleep(0.005)

span = (last - first) if (first and last and last > first) else 0.0
print("DATA-SPAN %.2f" % span, flush=True)
print("SENT %d" % sent, flush=True)
print("GOT %d" % len(got), flush=True)
# How much of what arrived is the pattern, checked over the longest run so the
# CONNECT string and any result codes are not scored as errors.
best = 0
if got:
    body = bytes(got)
    for off in range(len(PAT)):
        exp = (PAT * (len(body) // len(PAT) + 2))[off:off + len(body)]
        good = sum(1 for u, v in zip(body, exp) if u == v)
        best = max(best, good)
print("MATCH %d of %d" % (best, len(got)), flush=True)
for line in con.replace("\r", "\n").split("\n"):
    if line.strip().startswith("CONNECT"):
        print("CONNECT-LINE %s" % line.strip(), flush=True)
s.dtr = False; time.sleep(0.4); s.close()
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--dial", default="ATDT",
                    help="the Cirrus on ttyUSB0 needs ATDT, the Conexant ATD")
    ap.add_argument("--number", default="**620")
    ap.add_argument("--seconds", type=float, default=95.0,
                    help="call length; the handshake costs about 10 s, so 95 "
                         "leaves well over the minute of data phase asked for")
    ap.add_argument("--level", type=float, default=-18.0)
    ap.add_argument("--feed", type=int, default=64)
    ap.add_argument("--max", default="14400",
                    help="the modem's own +MS ceiling. With automode 0 the "
                         "Cirrus advertises this rate and nothing else, so it "
                         "is the only way to pin that modem to a rate, and an "
                         "R1 from us that omits it draws a cleardown")
    ap.add_argument("--trn", type=int, default=1280,
                    help="TRN symbols we send. 5.2.3 permits 1280 to 8192 and "
                         "we have always sent the floor, at every rate. The far "
                         "end trains its equaliser on this and nothing else, and "
                         "at 14 400 it has to slice 128 points on an 11%% margin "
                         "with it -- our own receiver does not care, because it "
                         "trains on the TRN reference rather than blindly")
    ap.add_argument("--no-factory", action="store_true",
                    help="skip AT&F, leaving the stored profile in place")
    ap.add_argument("--rates", default="4800,7200,9600,12000,14400",
                    help="the rates our R1 offers. 5.4.2 has us select "
                         "max(offered & ours) and nothing in the code looks at "
                         "how good the line is, though 6.2 recommends R3 "
                         "'take also account of the likely performance of the "
                         "answer modem receiver' -- so with everything enabled "
                         "we always choose the top rate whether it holds or not")
    ap.add_argument("--automode", default="1", choices=("0", "1"),
                    help="AT+MS's second field. The two modems need different "
                         "values to offer their whole range, which is a "
                         "property of the modems and not of the rate: the "
                         "Cirrus with automode 0 advertises max and nothing "
                         "else, so only 1 gets a negotiable range out of it; "
                         "the Conexant offers all five rates at automode 0 and "
                         "with 1 goes off attempting V.8 and never reaches 5.4")
    a = ap.parse_args()

    ans = subprocess.Popen(
        [sys.executable, "-u", "v32answer.py",
         "--seconds", str(a.seconds), "--level", str(a.level),
         "--bis", "--rates", a.rates,
         "--ec", "--echo", "--send-ascii", "--feed", str(a.feed),
         "--trn", str(a.trn)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    out = []

    import threading

    def pump():
        for ln in ans.stdout:
            out.append(ln.rstrip())
            print("SOFT | " + ln.rstrip(), flush=True)
    threading.Thread(target=pump, daemon=True).start()
    time.sleep(3.0)

    far = subprocess.run(
        ["ssh", PI, "python3 - %s %s %.1f %s %s %s %s"
         % (a.port, a.number, a.seconds + 6, a.dial, a.automode,
            "0" if a.no_factory else "1", a.max)],
        input=DIAL.encode(), capture_output=True)
    ftxt = far.stdout.decode("ascii", "replace")
    for ln in ftxt.splitlines():
        print("MODEM| " + ln, flush=True)
    ans.wait(timeout=180)

    soft = "\n".join(out)

    def find(pat, hay, cast=str, default=None):
        m = re.search(pat, hay)
        return cast(m.group(1)) if m else default

    rate = find(r"final state DATA, rate (\d+)", soft, int)
    span_us = find(r"throughput: \d+ octets in ([\d.]+) s", soft, float)
    got_us = find(r"throughput: (\d+) octets in", soft, int)
    if got_us is None:
        got_us = find(r"7\.2 V\.14: (\d+) characters", soft, int, 0)
    v42up = "LAPM connected" in soft
    span_far = find(r"DATA-SPAN ([\d.]+)", ftxt, float, 0.0)
    sent_far = find(r"SENT (\d+)", ftxt, int, 0)
    got_far = find(r"GOT (\d+)", ftxt, int, 0)
    match = find(r"MATCH (\d+) of", ftxt, int, 0)
    match_of = find(r"MATCH \d+ of (\d+)", ftxt, int, 0)
    conline = find(r"CONNECT-LINE (.*)", ftxt)
    msback = find(r"MS-READBACK (.*)", ftxt)

    span = span_us or span_far or 0.0
    print()
    print("=" * 68)
    print("  port %s   %s" % (a.port, "V.42/LAPM" if v42up else "V.14 (no V.42)"))
    print("  automode            %s   our R1 offered %s" % (a.automode, a.rates))
    print("  +MS readback        %s" % (msback or "?"))
    print("  CONNECT             %s" % (conline or "?"))
    print("  negotiated line rate %s bit/s" % (rate or "?"))
    print("  data phase           %.1f s" % span)
    if span > 0:
        print("  modem -> softmodem   %6d octets  %6.0f byte/s = %5.0f bit/s%s"
              % (got_us or 0, (got_us or 0) / span, 8 * (got_us or 0) / span,
                 "  (%.0f%% of line)" % (100.0 * 8 * (got_us or 0) / span / rate)
                 if rate else ""))
        sp2 = span_far or span
        print("  softmodem -> modem   %6d octets  %6.0f byte/s = %5.0f bit/s%s"
              % (got_far, got_far / sp2, 8 * got_far / sp2,
                 "  (%.0f%% of line)" % (100.0 * 8 * got_far / sp2 / rate)
                 if rate else ""))
    print("  pattern check        %d of %d octets correct%s"
          % (match, match_of,
             " (%.4f%%)" % (100.0 * match / match_of) if match_of else ""))
    print("  the modem's DTE offered %d octets to the line" % sent_far)
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
