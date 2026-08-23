"""Originate: the soft-modem calls a hardware modem, which answers.

  v22answer.py (on the Pi)  the hardware modem, waiting with S0 set
  run_call.py               the soft-modem, placing the call

The Pi side is started first, because it has to be sitting on the line before
the INVITE arrives.

usage: orch_call.py PORT NUMBER SECS SOFTPAT MODEMPAT "SETUP;..." -- <run_call args>
"""
import re, subprocess, sys, threading, time

port, number, secs, softpat, modempat, setup = sys.argv[1:7]
rest = sys.argv[7:]
sipargs = rest[1:] if rest and rest[0] == "--" else rest

mp = subprocess.Popen(["ssh", "raspberrypi",
                       "python3 ~/modemprobe/v22answer.py %s %s '%s' '%s' '%s'"
                       % (port, secs, modempat, softpat, setup)],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, bufsize=1)
ready = threading.Event()


def pump_modem():
    for ln in mp.stdout:
        print("MODEM| " + ln.rstrip(), flush=True)
        if "waiting for a call" in ln:
            ready.set()


threading.Thread(target=pump_modem, daemon=True).start()
if not ready.wait(40):
    mp.kill()
    sys.exit("modem not listening")
time.sleep(0.5)

# With --dte, run_call takes the number from the DTE's ATD rather than the
# command line, and a DTE program is attached to the pty it announces.
want_dte = "--dte" in sipargs
argv = ["python3", "-u", "run_call.py"] + ([] if want_dte else [number]) + sipargs
sm = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, bufsize=1)
device = [None]


def pump_soft():
    for ln in sm.stdout:
        print("SOFT | " + ln.rstrip(), flush=True)
        m = re.search(r"DTE interface on (\S+)", ln)
        if m:
            device[0] = m.group(1)


threading.Thread(target=pump_soft, daemon=True).start()

dc = None
if want_dte:
    end = time.time() + 30
    while time.time() < end and not device[0]:
        time.sleep(0.1)
    if device[0]:
        time.sleep(0.4)
        dc = subprocess.Popen(["python3", "-u", "dtechat.py", device[0],
                               str(float(secs) - 6.0), softpat, modempat,
                               "ATE0", number],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1)
        threading.Thread(target=lambda: [print("DTE  | " + l.rstrip(), flush=True)
                                         for l in dc.stdout], daemon=True).start()
    else:
        print("no DTE device was announced", flush=True)
try:
    if dc:
        dc.wait(timeout=300)
except Exception:
    pass
try:
    sm.wait(timeout=300)
except Exception:
    sm.kill()
try:
    mp.wait(timeout=120)
except Exception:
    mp.kill()
