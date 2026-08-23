"""Interleaved A/B of VBD vs no-VBD, plus a PCMU-only run."""
import subprocess, sys
runs = [
    ("baseline  PCMA", ["/dev/ttyACM0","131","**2","am2100","-24"]),
    ("VBD       PCMA", ["/dev/ttyACM0","131","**2","am2100","-24","--vbd"]),
    ("baseline  PCMA", ["/dev/ttyACM0","131","**2","am2100","-24"]),
    ("VBD       PCMA", ["/dev/ttyACM0","131","**2","am2100","-24","--vbd"]),
    ("VBD       PCMU", ["/dev/ttyACM0","131","**2","am2100","-24","--vbd","--codec","0"]),
    ("baseline  PCMU", ["/dev/ttyACM0","131","**2","am2100","-24","--codec","0"]),
    ("control 1500 Hz", ["/dev/ttyACM0","131","**2","am1500","-24","--vbd"]),
]
for label, args in runs:
    p = subprocess.run(["python3","origprobe.py"]+args, capture_output=True, text=True, timeout=280)
    line = [l for l in p.stdout.splitlines() if "analog port" in l]
    gp = "yes" if "gpmd" in p.stdout else "no"
    print("  %-16s box echoed gpmd=%-4s %s"
          % (label, gp, line[0].strip() if line else "(no result)"), flush=True)
