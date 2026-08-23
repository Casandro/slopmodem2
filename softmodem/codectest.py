"""Ask the FRITZ!Box to accept a given codec list, with the modem properly armed."""
import subprocess, threading, sys, time

def arm_and_call(codeclist, number="**2", port="/dev/ttyACM0"):
    mp = subprocess.Popen(["ssh", "raspberrypi",
                           "python3 ~/modemprobe/listen.py %s 25 ATX4 ATS0=1" % port],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, bufsize=1)
    ready = threading.Event()
    def pm():
        for ln in mp.stdout:
            if "LISTENING" in ln:
                ready.set()
    threading.Thread(target=pm, daemon=True).start()
    if not ready.wait(40):
        mp.kill(); return "modem not armed", ""
    time.sleep(0.5)
    p = subprocess.run(["python3", "run_originate.py", number, "--probe", "flat",
                        "--codec", codeclist, "--level", "-40", "--seconds", "3"],
                       capture_output=True, text=True, timeout=120)
    try: mp.wait(timeout=40)
    except Exception: mp.kill()
    return p.stdout, p.stderr

for cl in sys.argv[1:]:
    out, err = arm_and_call(cl)
    status = [l.strip() for l in out.splitlines() if l.strip().startswith("->")]
    m = [l.strip() for l in out.splitlines() if l.strip().startswith(("m=audio", "a=rtpmap"))]
    extra = [l.strip() for l in out.splitlines()
             if l.strip().startswith(("Warning:", "Reason:", "Retry-After:"))]
    if extra:
        m = m + extra
    print("  offer=%-8s  result=%-12s  answer: %s"
          % (cl, status[0] if status else "(none)", "; ".join(m) if m else "(no SDP)"), flush=True)
