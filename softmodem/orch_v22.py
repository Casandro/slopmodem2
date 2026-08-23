"""Answer with the closed-loop V.22bis handshake while the modem sends data."""
import subprocess, threading, sys, time
port, secs, pattern, expect = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sipargs = sys.argv[5:]
ans = subprocess.Popen(["python3","-u","run_answer.py","--v22bis"]+sipargs,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
ready = threading.Event()
def pa():
    for ln in ans.stdout:
        print("SIP  | "+ln.rstrip(), flush=True)
        if "waiting up to" in ln: ready.set()
threading.Thread(target=pa, daemon=True).start()
if not ready.wait(40):
    ans.kill(); sys.exit("answerer not ready")
time.sleep(0.8)
mp = subprocess.Popen(["ssh","raspberrypi",
                       "python3 ~/modemprobe/v22data.py %s '**620' %s '%s' '%s'" % (port, secs, pattern, expect)],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
def pm():
    for ln in mp.stdout: print("MODEM| "+ln.rstrip(), flush=True)
threading.Thread(target=pm, daemon=True).start()
mp.wait(timeout=200); ans.wait(timeout=200)
