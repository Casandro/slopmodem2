"""One modem process, two calls, opposite directions.

This is what the unification bought. A single `modem.py` places a call, hangs up,
goes back to idle, and then answers one -- which neither run_answer.py nor
run_call.py could do, because each was a script that handled one call and exited.

  call 1  modem.py dials the hardware modem, which answers (v22answer.py)
  call 2  the hardware modem dials **620, and the same process answers it

usage: orch_both.py PORT NUMBER SECS SOFTPAT MODEMPAT "SETUP;..." -- <modem args>
"""
import re, subprocess, sys, threading, time

port, number, secs, softpat, modempat, setup = sys.argv[1:7]
rest = sys.argv[7:]
extra = rest[1:] if rest and rest[0] == "--" else rest


def stream(tag, proc):
    for ln in proc.stdout:
        print("%-5s| %s" % (tag, ln.rstrip()), flush=True)


def pi(script, args):
    p = subprocess.Popen(["ssh", "raspberrypi",
                          "python3 ~/modemprobe/%s %s" % (script, args)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    threading.Thread(target=stream, args=("MODEM", p), daemon=True).start()
    return p


# The modem is started first and stays up for both calls.
sm = subprocess.Popen(["python3", "-u", "modem.py", "--calls", "2",
                       "--dial", number] + extra,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, bufsize=1)
calls = []


def watch_soft():
    for ln in sm.stdout:
        print("SOFT | " + ln.rstrip(), flush=True)
        m = re.search(r"MATCH\s+: ([\d.]+)% of (\d+) characters, (\d+) slip", ln)
        if m:
            calls.append((float(m.group(1)), int(m.group(2)), int(m.group(3))))


threading.Thread(target=watch_soft, daemon=True).start()

# Call 1: the hardware modem answers what we dial.
a1 = pi("v22answer.py", "%s %s '%s' '%s' '%s'"
        % (port, secs, modempat, softpat, setup + ";ATS0=1"))
a1.wait(timeout=200)
print("---- call 1 finished on the modem side ----", flush=True)
time.sleep(3)

# Call 2: the hardware modem dials in, and the same process answers.
a2 = pi("v22data2.py", "%s 'ATDT**620' %s '%s' '%s' '%s'"
        % (port, secs, modempat, softpat, setup))
a2.wait(timeout=200)
print("---- call 2 finished on the modem side ----", flush=True)
try:
    sm.wait(timeout=90)
except Exception:
    sm.kill()
print("==== soft-modem scored %d call(s): %s" % (len(calls), calls), flush=True)
