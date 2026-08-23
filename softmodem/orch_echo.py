"""Run echo_probe.py while a modem dials in and stays quiet."""
import subprocess, sys, threading, time
port, dialcmd = sys.argv[1], sys.argv[2]
ep = subprocess.Popen(["python3","-u","echo_probe.py"]+sys.argv[3:],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
ready = threading.Event()
def pa():
    for ln in ep.stdout:
        print("PROBE| "+ln.rstrip(), flush=True)
        if "waiting up to" in ln: ready.set()
threading.Thread(target=pa, daemon=True).start()
if not ready.wait(40): ep.kill(); sys.exit("probe not ready")
time.sleep(0.8)
mp = subprocess.Popen(["ssh","raspberrypi",
    "python3 ~/modemprobe/listen.py %s 30 ATX4 'AT+MS=V32,1,9600,9600' '%s'" % (port, dialcmd)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
threading.Thread(target=lambda:[print("MODEM| "+l.rstrip(), flush=True) for l in mp.stdout], daemon=True).start()
mp.wait(timeout=200); ep.wait(timeout=200)
