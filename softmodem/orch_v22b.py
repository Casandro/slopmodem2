"""Answer with the closed-loop V.22bis handshake while a modem sends data.
usage: orch_v22b.py PORT DIALCMD SECS PATTERN EXPECT SETUP -- <run_answer args>"""
import subprocess, threading, sys, time
port, dialcmd, secs, pattern, expect, setup = sys.argv[1:7]
sipargs = sys.argv[8:] if len(sys.argv) > 7 and sys.argv[7] == "--" else sys.argv[7:]
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
    "python3 ~/modemprobe/v22data2.py %s '%s' %s '%s' '%s' '%s'"
    % (port, dialcmd, secs, pattern, expect, setup)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
def pm():
    for ln in mp.stdout: print("MODEM| "+ln.rstrip(), flush=True)
threading.Thread(target=pm, daemon=True).start()
mp.wait(timeout=300); ans.wait(timeout=300)
