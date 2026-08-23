"""Run our V.32 start-up against a hardware modem: answerer here, dialler on the Pi.

The modem has to be forced to V.32 first. Left in automode both of these pick
V.34 or V.90 and never reach 5.4 at all -- the Conexant lists V32 in AT+MS=? and
the Cirrus takes AT+MS= without advertising it.

  python3 orch_v32.py --port /dev/ttyACM0 --trellis
"""
import argparse, subprocess, sys, time

PI = "raspberrypi"
DIAL = r'''
import serial, sys, time
port, number, ms, secs = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
dialcmd = sys.argv[5] if len(sys.argv) > 5 else "ATD"
sendpat = (sys.argv[6] == "1") if len(sys.argv) > 6 else True
ec = (sys.argv[7] == "1") if len(sys.argv) > 7 else False
flood = (sys.argv[8] == "1") if len(sys.argv) > 8 else False
s = serial.Serial(port, 115200, timeout=0.5, rtscts=False)
# always reset before use: DTR drop, ATH0, ATZ, then options
s.dtr = False; time.sleep(0.6); s.dtr = True; time.sleep(0.3)
s.reset_input_buffer()
def cmd(c, wait=1.0):
    s.write((c + "\r").encode()); time.sleep(wait)
    r = s.read(4000).decode("ascii", "replace")
    print("%-26s -> %s" % (c, r.replace("\r", " ").strip()[:200]), flush=True)
    return r
cmd("ATH0"); cmd("ATZ")
cmd("ATE0V1X4")          # echo OFF, so what comes back is the far end only
# V.32 says nothing about error correction, but both modems default to it, and a
# modem negotiating V.42 with a soft modem that speaks only V.14 never passes
# anything to its DTE. The two use different command sets and neither accepts the
# other's, so the union is offered and ERRORs are expected -- what matters is that
# the resulting profile has no error correction and no compression.
#
#   Conexant  &Q5 -> &Q6, S48=7 -> 128 (LAPM off), S46=138 -> 136 (V.42bis off)
#   Cirrus    \N3 -> \N0 (buffered, no correction), %C1 -> %C0 (no compression)
#
# Direct mode is the obvious choice and is wrong for both: the Conexant's AT\N1
# is an ERROR, and &Q0 / \N1 tie the DTE port speed to the line rate, while the
# Pi opens the port at 115200 against a 9600 line.
took = []
# ATW1 makes the CONNECT string report the line rate and the protocol actually
# in use, instead of just the DTE speed -- which is the difference between
# guessing at the far end's error correction and reading it. AT"H0 and AT\A0 are
# this chipset's V.42bis and MNP controls; its profile came up "H3 and \A3.
#
# With --ec the point is the opposite: V.42 is what we are testing, so LAPM stays
# on and only compression comes off. We have no V.42bis and send no XID, and a
# modem that had negotiated compression would send us BTLZ codewords inside
# perfectly good I frames.
#
#   Conexant  &Q5 error-corrected, S48=7 LAPM negotiation, S46=136 no V.42bis
#   Cirrus    \N3 auto-reliable (so it can still fall back), %C0 "H0 \A0
if ec:
    opts = ("AT&K3", "ATW1", "AT&Q5", "ATS48=7", "ATS46=136", "AT\\N3",
            "AT%C0", "AT\"H0", "AT\\A0")
else:
    opts = ("AT&K0", "ATW1", "AT&Q6", "ATS48=128", "ATS46=136", "AT\\N0",
            "AT%C0", "AT\"H0", "AT\\A0")
for opt in opts:
    r = cmd(opt)
    took.append("%s=%s" % (opt[2:], "ok" if "OK" in r else "err"))
print("  error correction %s: %s" % ("ON (V.42 under test)" if ec else "off",
                                     " ".join(took)), flush=True)
if ec and flood:
    s.rtscts = True      # &K3 above; without this a flood overruns the modem
cmd("AT+MS=" + ms)       # force the modulation, automode off
cmd("AT+MS?")
t0 = time.time()
cmd(dialcmd + number, wait=2.0)
buf = ""
sent = 0
got = 0
printable = 0
pat = b"AAA2BBB "
connected = False
last = 0.0
echoed = 0
while time.time() - t0 < secs:
    # Read only what is waiting. s.read(4000) blocks until 4000 bytes or the
    # 0.5 s port timeout, so at line rates each iteration took half a second and
    # the flood below managed one 256-byte write in that time: 512 byte/s, which
    # is exactly the "42% of the channel" this harness kept reporting for the
    # modem-to-us direction on two different modems. The limit was here.
    n = s.in_waiting
    d = s.read(n) if n else b""

    if d:
        txt = d.decode("ascii", "replace")
        buf += txt
        # Only echo what looks like AT result codes. In direct mode the line
        # payload arrives on the same port, and a modem that cannot decode us
        # emits megabytes of noise -- which drowns the log and, worse, hides
        # whether a CONNECT ever arrived.
        good = sum(1 for c in d if 32 <= c < 127 or c in (10, 13))
        got += len(d)
        printable += good
        # Cap the echo. With error correction working, the far end's data is
        # clean printable ASCII and this dumped 40 000 characters of it into the
        # log, burying the report it was meant to sit beside.
        if good >= 0.9 * len(d) and echoed < 2000:
            echoed += len(d)
            sys.stdout.write(txt); sys.stdout.flush()
        if "CONNECT" in buf:
            connected = True
    now = time.time()
    # once connected, feed a known pattern at 200 byte/s so the far end has
    # something identifiable to recover
    if sendpat and connected and (flood or now - last >= 0.04):
        # flood: write as fast as RTS/CTS allows, so the measured rate is the
        # line's and not the feeder's
        blk = pat * 256 if flood else pat
        sent += s.write(blk)
        last = now
    time.sleep(0.01)
print()
print("sent %d bytes of %r after CONNECT; received %d bytes from the line, "
      "%.1f%% printable" % (sent, pat.decode(), got,
                            100.0 * printable / max(got, 1)), flush=True)
# Did anything the far end sent actually arrive here? Look for what the soft
# modem was told to send, and report the longest printable run either way.
tail = buf[-40000:]
for probe in ("SLOPMODEM", "AAA2BBB"):
    print("  %r seen at the modem's DTE: %d times" % (probe, tail.count(probe)),
          flush=True)
runs = []
cur = ""
for c in tail:
    if 32 <= ord(c) < 127:
        cur += c
    else:
        if len(cur) > len(runs[0]) if runs else len(cur) > 8:
            runs = [cur]
        cur = ""
if cur and (not runs or len(cur) > len(runs[0])):
    runs = [cur]
print("  longest printable run at the modem's DTE: %d chars %r"
      % (len(runs[0]) if runs else 0, (runs[0][:60] if runs else "")), flush=True)
# Character error rate against the pattern the far end was told to send.
# "100% printable" is nearly worthless as an error measure -- most single bit
# errors land on another printable character -- and what matters for HDLC is the
# real bit error rate, because one bad bit destroys a whole frame. Measured on
# the longest printable run so AT result codes are not counted as errors.
probe = sys.argv[9].encode() if len(sys.argv) > 9 else b""
if probe and runs and len(runs[0]) > 4 * len(probe):
    body = runs[0].encode("latin1", "replace")
    best = (-1, 0)
    for off in range(len(probe)):
        exp = (probe * (len(body) // len(probe) + 2))[off:off + len(body)]
        good = sum(1 for u, v in zip(body, exp) if u == v)
        if good > best[0]:
            best = (good, off)
    good, off = best
    n = len(body)
    print("  pattern check: %d of %d characters match -> character error rate "
          "%.2e, so a BER near %.2e"
          % (good, n, (n - good) / float(n), (n - good) / (8.0 * n)), flush=True)
time.sleep(0.5)
# ask the modem why the call ended, rather than guessing
for c in ("ATI11", "AT#UD", "ATI6"):
    cmd(c, wait=1.5)
print()
print("---- result after %.1fs ----" % (time.time() - t0), flush=True)
for line in buf.replace("\r", "\n").split("\n"):
    if line.strip():
        print("  %s" % line.strip())
s.dtr = False; time.sleep(0.4); s.close()
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--number", default="**620")
    ap.add_argument("--ms", default="V32,0,9600,9600",
                    help="AT+MS argument: modulation, automode, min, max")
    ap.add_argument("--seconds", type=float, default=50.0)
    ap.add_argument("--trellis", action="store_true")
    ap.add_argument("--ans", type=float, default=3.3)
    ap.add_argument("--trn", type=int, default=1280)
    ap.add_argument("--send", default="", help="pass through to v32answer.py; "
                    "also used at the modem's DTE to measure the character "
                    "error rate of our transmitted signal")
    ap.add_argument("--bis", action="store_true")
    ap.add_argument("--regain", type=int, default=None)
    ap.add_argument("--rates", default="")
    ap.add_argument("--no-pattern", action="store_true",
                    help="stay idle after CONNECT instead of sending AAA2BBB")
    ap.add_argument("--dial", default="ATD",
                    help="the Cirrus on ttyUSB0 needs ATDT; the Conexant "
                         "takes plain ATD (testrig/modem.md)")
    ap.add_argument("--level", type=float, default=-24.0)
    ap.add_argument("--ec", action="store_true",
                    help="leave the modem's V.42 enabled and run ours against "
                         "it, instead of switching error correction off")
    ap.add_argument("--flood", action="store_true",
                    help="with --ec, feed the modem's DTE at full speed under "
                         "RTS/CTS so the result is a throughput measurement")
    ap.add_argument("--feed", type=int, default=4)
    ap.add_argument("--xid-reps", type=int, default=1)
    ap.add_argument("--xid-probe", action="store_true")
    ap.add_argument("--xid-no-opt", action="store_true")
    ap.add_argument("--echo", action="store_true")
    ap.add_argument("--echo-budget", type=int, default=None)
    a = ap.parse_args()

    cmd = [sys.executable, "v32answer.py", "--seconds", str(a.seconds),
           "--ans", str(a.ans), "--level", str(a.level), "--trn", str(a.trn)]
    if a.send:
        cmd += ["--send", a.send]
    if a.bis:
        cmd.append("--bis")
    if a.regain is not None:
        cmd += ["--regain", str(a.regain)]
    if a.rates:
        cmd += ["--rates", a.rates]
    if a.trellis:
        cmd.append("--trellis")
    if a.ec:
        cmd.append("--ec")
    if a.xid_reps != 1:
        cmd += ["--xid-reps", str(a.xid_reps)]
    if a.xid_probe:
        cmd.append("--xid-probe")
    if a.xid_no_opt:
        cmd.append("--xid-no-opt")
    if a.echo:
        cmd.append("--echo")
    if a.echo_budget is not None:
        cmd += ["--echo-budget", str(a.echo_budget)]
    if a.flood:
        cmd += ["--feed", str(max(a.feed, 30))]
    elif a.feed != 4:
        cmd += ["--feed", str(a.feed)]
    ans = subprocess.Popen(cmd)
    time.sleep(3.0)
    subprocess.run(["ssh", PI, "python3 - %s %s %s %.1f %s"
                    % (a.port, a.number, a.ms, a.seconds + 6, a.dial)
                    + (" 0" if a.no_pattern else " 1")
                    + (" 1" if a.ec else " 0")
                    + (" 1" if a.flood else " 0")
                    + (" '" + a.send + "'" if a.send else "")],
                   input=DIAL.encode())
    ans.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
