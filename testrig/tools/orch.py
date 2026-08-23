import subprocess, threading, sys, time
import rtpcall

port, number, extra = sys.argv[1], sys.argv[2], sys.argv[3:]
cmd = ["ssh", "raspberrypi", "python3 ~/modemprobe/listen.py %s 40 %s" % (port, " ".join(extra))]
p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, bufsize=1)
lines, ready = [], threading.Event()
def pump():
    for ln in p.stdout:
        ln = ln.rstrip()
        lines.append(ln)
        print("MODEM| " + ln, flush=True)
        if "LISTENING" in ln: ready.set()
threading.Thread(target=pump, daemon=True).start()
if not ready.wait(30):
    print("listener never became ready"); p.kill(); sys.exit(1)
time.sleep(1.0)
print("=" * 60, flush=True)
print("SIP side: calling %s from **620" % number, flush=True)
print("=" * 60, flush=True)
rtpcall.call(number, 14.0)
print("=" * 60, flush=True)
p.wait(timeout=60)
