import subprocess, threading, sys, time
port, dial = sys.argv[1], sys.argv[2]
ans = subprocess.Popen(["python3", "-u", "ansam.py", "22"], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, bufsize=1)
ready = threading.Event()
def pump_ans():
    for ln in ans.stdout:
        print("SIP  | " + ln.rstrip(), flush=True)
        if "waiting up to" in ln: ready.set()
threading.Thread(target=pump_ans, daemon=True).start()
if not ready.wait(30):
    print("answerer not ready"); ans.kill(); sys.exit(1)
time.sleep(1.0)
print("=" * 60, flush=True)
print("modem on %s dials %s" % (port, dial), flush=True)
print("=" * 60, flush=True)
mp = subprocess.Popen(["ssh", "raspberrypi",
                       "python3 ~/modemprobe/listen.py %s 35 ATX4 %s" % (port, dial)],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
def pump_m():
    for ln in mp.stdout:
        print("MODEM| " + ln.rstrip(), flush=True)
threading.Thread(target=pump_m, daemon=True).start()
ans.wait(timeout=120); mp.wait(timeout=120)
