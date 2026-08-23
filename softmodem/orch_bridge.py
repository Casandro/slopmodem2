"""Bridge a call between the two hardware modems and record both directions.

  bridge.py            answers **1's call, then calls **2, relays between them
  v22data2.py  on **1  dials **620 and exchanges a pattern
  v22answer.py on **2  waits with S0 set and exchanges a pattern

usage: orch_bridge.py PORT_A PORT_B NUMBER_B SECS PAT_A PAT_B "SETUP;..." -- <bridge args>
"""
import re, subprocess, sys, threading, time
pa, pb, numb, secs, pat_a, pat_b, setup = sys.argv[1:8]
rest = sys.argv[8:]
bargs = rest[1:] if rest and rest[0] == "--" else rest

def stream(tag, p):
    for ln in p.stdout: print("%-6s| %s" % (tag, ln.rstrip()), flush=True)

def pi(script, args, tag):
    p = subprocess.Popen(["ssh", "raspberrypi", "python3 ~/modemprobe/%s %s" % (script, args)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    threading.Thread(target=stream, args=(tag, p), daemon=True).start()
    return p

br = subprocess.Popen(["python3","-u","bridge.py", numb] + bargs,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
ready = threading.Event()
def pump():
    for ln in br.stdout:
        print("BRIDGE| " + ln.rstrip(), flush=True)
        if "waiting up to" in ln: ready.set()
threading.Thread(target=pump, daemon=True).start()
if not ready.wait(40): br.kill(); sys.exit("bridge not ready")

# the answering modem must be listening before the bridge places its leg
mb = pi("v22answer.py", "%s %s '%s' '%s' '%s'" % (pb, secs, pat_b, pat_a, setup + ";ATS0=1"), "MODEM-B")
time.sleep(6)
ma = pi("v22data2.py", "%s 'ATDT**620' %s '%s' '%s' '%s'" % (pa, secs, pat_a, pat_b, setup), "MODEM-A")
for p in (ma, mb):
    try: p.wait(timeout=300)
    except Exception: p.kill()
try: br.wait(timeout=120)
except Exception: br.kill()
