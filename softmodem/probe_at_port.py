"""Send a probe signal and measure it at a modem's analog port."""
import subprocess, threading, sys, time, os, math
import g711, dsp, amdepth

def capture(port, codec, signal, level, secs=20.0):
    ans = subprocess.Popen(["python3", "-u", "run_answer.py", "--signal", signal,
                            "--level", str(level), "--lead", "0.5",
                            "--ansam-s", "16", "--seconds", str(secs)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, bufsize=1)
    ready = threading.Event()
    threading.Thread(target=lambda: [ready.set() for ln in ans.stdout
                                     if "waiting up to" in ln], daemon=True).start()
    if not ready.wait(40):
        ans.kill(); return None
    time.sleep(1.0)
    mp = subprocess.Popen(["ssh", "raspberrypi",
                           "python3 ~/modemprobe/voicecap.py %s '**620' %s 8 /tmp/vc.raw"
                           % (port, codec)],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = mp.communicate()[0]
    try: ans.wait(timeout=60)
    except Exception: ans.kill()
    if "wrote" not in out: return None
    dst = "ref/p_%s_%s.raw" % (signal, str(level).replace("-", "m"))
    subprocess.run(["scp", "-q", "raspberrypi:/tmp/vc.raw", dst], check=False)
    return dst

if __name__ == "__main__":
    port, codec = sys.argv[1], sys.argv[2]
    level = float(sys.argv[3])
    print("  %-9s %-8s %-9s %-9s %-8s %s"
          % ("signal", "rxLevel", "carrier", "depth%", "amRate", "coherence"))
    for signal in sys.argv[4:]:
        d = capture(port, codec, signal, level)
        if not d:
            print("  %-9s capture failed" % signal, flush=True); continue
        x = g711.decode(open(d, "rb").read(), 0)
        # locate whatever tone is actually present
        fc, _ = dsp.dominant(x, 800, 3200, coarse=25, fine=1)
        seg = dsp.find_tone_segment(x, fc)
        y = x[seg[0]:seg[1]] if seg else x
        dep, rate, coh = amdepth.am_depth(y, carrier=fc)
        print("  %-9s %-8.1f %-9d %-9.2f %-8.2f %.3f"
              % (signal, dsp.dbfs(dsp.rms(y)), fc, 100*dep, rate, coh), flush=True)
