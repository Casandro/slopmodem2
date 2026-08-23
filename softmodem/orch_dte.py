"""Three-way test: soft-modem with a DTE attached, hardware modem dialling in.

  run_answer.py --dte      the soft-modem; prints the pty it created
  dtechat.py               a DTE, attached to that pty
  v22data2.py (on the Pi)  the hardware modem, dialling **620

usage: orch_dte.py PORT DIALCMD SECS MODEMPAT DTEPAT "SETUP;..." -- <run_answer args>
"""
import re, subprocess, sys, threading, time

port, dialcmd, secs, modempat, dtepat, setup = sys.argv[1:7]
rest = sys.argv[7:]
# an optional DTE data duration, so the DTE can finish first and hang up with
# ATH instead of the far modem ending the call
dte_secs = float(secs)
if rest and rest[0] != "--":
    dte_secs = float(rest.pop(0))
sipargs = rest[1:] if rest and rest[0] == "--" else rest

ans = subprocess.Popen(["python3", "-u", "run_answer.py", "--v22bis", "--dte"]
                       + sipargs,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, bufsize=1)
ready = threading.Event()
device = [None]


def pump_answer():
    for ln in ans.stdout:
        print("SIP  | " + ln.rstrip(), flush=True)
        m = re.search(r"DTE interface on (\S+)", ln)
        if m:
            device[0] = m.group(1)
        if "waiting up to" in ln:
            ready.set()


threading.Thread(target=pump_answer, daemon=True).start()
if not ready.wait(40):
    ans.kill()
    sys.exit("answerer not ready")

# The pty only exists once the INVITE arrives, so the DTE is started after the
# modem dials -- same order as a real installation, where the terminal is
# already sitting on the port waiting for RING.
time.sleep(0.5)
mp = subprocess.Popen(["ssh", "raspberrypi",
                       "python3 ~/modemprobe/v22data2.py %s '%s' %s '%s' '%s' '%s'"
                       % (port, dialcmd, secs, modempat, dtepat, setup)],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, bufsize=1)
threading.Thread(target=lambda: [print("MODEM| " + l.rstrip(), flush=True)
                                 for l in mp.stdout], daemon=True).start()

dc = None
deadline = time.time() + 60
while time.time() < deadline:
    if device[0]:
        time.sleep(0.4)
        dc = subprocess.Popen(["python3", "-u", "dtechat.py", device[0],
                               str(dte_secs), dtepat, modempat, "ATE0"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1)
        threading.Thread(target=lambda: [print("DTE  | " + l.rstrip(), flush=True)
                                         for l in dc.stdout], daemon=True).start()
        break
    time.sleep(0.1)
if dc is None:
    print("no DTE device was announced", flush=True)

try:
    mp.wait(timeout=300)
except Exception:
    pass
try:
    if dc:
        dc.wait(timeout=60)
except Exception:
    pass
try:
    ans.wait(timeout=120)
except Exception:
    ans.kill()
