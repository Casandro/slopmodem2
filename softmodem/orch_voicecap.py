"""Measure what the PBX delivers to the modem's analog port.

We answer the modem's voice call and play a known ANSam; the modem records it
through its own analog front end and hands the samples back over serial. That
is the only way to see what actually arrives on the far side of the FXS port.
"""
import subprocess, threading, sys, time

port, number, codec = sys.argv[1], sys.argv[2], sys.argv[3]
sipargs = sys.argv[4:]

ans = subprocess.Popen(["python3", "-u", "run_answer.py"] + sipargs,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, bufsize=1)
ready = threading.Event()
def pa():
    for ln in ans.stdout:
        print("SIP  | " + ln.rstrip(), flush=True)
        if "waiting up to" in ln:
            ready.set()
threading.Thread(target=pa, daemon=True).start()
if not ready.wait(40):
    print("answerer not ready"); ans.kill(); sys.exit(1)
time.sleep(1.0)
print("=" * 62, flush=True)
mp = subprocess.Popen(
    ["ssh", "raspberrypi",
     "python3 ~/modemprobe/voicecap.py %s '%s' %s 8 /tmp/vc.raw" % (port, number, codec)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
def pm():
    for ln in mp.stdout:
        print("MODEM| " + ln.rstrip(), flush=True)
threading.Thread(target=pm, daemon=True).start()
mp.wait(timeout=180)
ans.wait(timeout=180)
