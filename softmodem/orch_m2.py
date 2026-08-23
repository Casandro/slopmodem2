"""Start run_answer.py, then make a hardware modem dial **620.

usage: orch_m2.py PORT --at 'CMD' ['CMD' ...] [--sip <run_answer args>]

The AT commands are issued to the modem in order after a reset, so the dial
string goes last. Setting +A8E=6,... first makes the modem emit +A8A/+A8M/+A8J
indications (ITU-T V.251 6.x), which report exactly which V.8 signals it saw.
"""
import subprocess, threading, sys, time

argv = sys.argv[1:]
port = argv[0]
at, sipargs = [], []
mode = None
for a in argv[1:]:
    if a == "--at":
        mode = "at"; continue
    if a == "--sip":
        mode = "sip"; continue
    (at if mode == "at" else sipargs).append(a)

ans = subprocess.Popen(["python3", "-u", "run_answer.py"] + sipargs,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, bufsize=1)
ready = threading.Event()
def pump_ans():
    for ln in ans.stdout:
        print("SIP  | " + ln.rstrip(), flush=True)
        if "waiting up to" in ln:
            ready.set()
threading.Thread(target=pump_ans, daemon=True).start()
if not ready.wait(40):
    print("answerer not ready"); ans.kill(); sys.exit(1)
time.sleep(1.0)
print("=" * 62, flush=True)
print("modem %s: %s" % (port, " ; ".join(at)), flush=True)
print("=" * 62, flush=True)
mp = subprocess.Popen(["ssh", "raspberrypi",
                       "python3 ~/modemprobe/listen.py %s 45 %s" % (port, " ".join(at))],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, bufsize=1)
def pump_m():
    for ln in mp.stdout:
        print("MODEM| " + ln.rstrip(), flush=True)
threading.Thread(target=pump_m, daemon=True).start()
ans.wait(timeout=200)
mp.wait(timeout=200)
