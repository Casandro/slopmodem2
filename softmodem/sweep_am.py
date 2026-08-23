"""Sweep TX level and measure AM depth at a modem's analog port.

Localisation logic: the capture path is
   us -> RTP -> FRITZ!Box -> FXS -> modem analog front end -> modem voice ADC
so any suppression could be in the box OR in the modem's voice input. If two
very different modem chipsets show the same knee at the same level, the shared
element (the box) is implicated; if the knees differ, it is modem-side.
"""
import subprocess, threading, sys, time, os
import g711, dsp, amdepth

def run(port, codec, level, ansam_s=16.0, secs=20.0):
    ans = subprocess.Popen(["python3", "-u", "run_answer.py",
                            "--variant", "noreversal", "--level", str(level),
                            "--lead", "0.5", "--ansam-s", str(ansam_s),
                            "--seconds", str(secs)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, bufsize=1)
    ready = threading.Event()
    def pa():
        for ln in ans.stdout:
            if "waiting up to" in ln:
                ready.set()
    threading.Thread(target=pa, daemon=True).start()
    if not ready.wait(40):
        ans.kill(); return None
    time.sleep(1.0)
    mp = subprocess.Popen(["ssh", "raspberrypi",
                           "python3 ~/modemprobe/voicecap.py %s '**620' %s 8 /tmp/vc.raw"
                           % (port, codec)],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, bufsize=1)
    out = []
    for ln in mp.stdout:
        out.append(ln.rstrip())
    mp.wait(timeout=120)
    try:
        ans.wait(timeout=60)
    except Exception:
        ans.kill()
    if not any("wrote" in l for l in out):
        return None
    dst = "ref/sw_%s_%s.raw" % (os.path.basename(port), str(level).replace("-", "m"))
    subprocess.run(["scp", "-q", "raspberrypi:/tmp/vc.raw", dst], check=False)
    return dst

if __name__ == "__main__":
    port = sys.argv[1]
    codec = sys.argv[2]
    levels = [float(v) for v in sys.argv[3:]]
    pt = 0 if codec == "131" else None
    print("port=%s codec=%s" % (port, codec))
    print("  %-8s %-11s %-9s %-8s %s" % ("sent", "rx level", "depth", "rate", "coherence"))
    for lv in levels:
        d = run(port, codec, lv)
        if not d:
            print("  %-8.0f capture failed" % lv); continue
        x = g711.decode(open(d, "rb").read(), 0) if pt == 0 else None
        seg = dsp.find_tone_segment(x, 2100.0)
        if not seg:
            print("  %-8.0f no 2100 Hz segment (too quiet?)" % lv); continue
        y = x[seg[0]:seg[1]]
        dep, rate, coh = amdepth.am_depth(y)
        print("  %-8.0f %-11.1f %-9.2f %-8.2f %.3f"
              % (lv, dsp.dbfs(dsp.rms(y)), 100 * dep, rate, coh), flush=True)
