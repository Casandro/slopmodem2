"""Arm a hardware modem to answer, then originate a V.8 call to it."""
import subprocess, threading, sys, time
port = sys.argv[1]
number = sys.argv[2]
sep = sys.argv.index("--sip") if "--sip" in sys.argv else len(sys.argv)
at = sys.argv[3:sep]
sipargs = sys.argv[sep + 1:]
mp = subprocess.Popen(["ssh", "raspberrypi",
                       "python3 ~/modemprobe/listen.py %s 50 %s" % (port, " ".join(at))],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
ready = threading.Event()
def pm():
    for ln in mp.stdout:
        print("MODEM| " + ln.rstrip(), flush=True)
        if "LISTENING" in ln:
            ready.set()
threading.Thread(target=pm, daemon=True).start()
if not ready.wait(40):
    print("modem not armed"); mp.kill(); sys.exit(1)
time.sleep(0.5)
print("=" * 62, flush=True)
ans = subprocess.Popen(["python3", "-u", "run_originate.py", number] + sipargs,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
for ln in ans.stdout:
    print("SIP  | " + ln.rstrip(), flush=True)
ans.wait(timeout=180)
mp.wait(timeout=180)
