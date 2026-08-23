"""Capture both sides of the same call.

Saves the outbound stream exactly as transmitted *and* the modem's analog-port
recording, from one call, so the transformation can be bracketed instead of
compared across separate runs.
"""
import subprocess, threading, sys, time, os
import g711, g726, dsp, amdepth

port, codec, signal, level = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
vbd = "--vbd" in sys.argv
ecpre = None
for i, v in enumerate(sys.argv):
    if v == "--ec-prefix" and i + 1 < len(sys.argv):
        ecpre = sys.argv[i + 1]
pt_arg = None
for i, v in enumerate(sys.argv):
    if v == "--pt" and i + 1 < len(sys.argv):
        pt_arg = int(sys.argv[i + 1])
tag = "%s%s_%s%s%s" % (("ecp_" if ecpre else ""), signal, str(level).replace("-", "m"), "_vbd" if vbd else "",
                     ("_pt%d" % pt_arg) if pt_arg is not None else "")
tx = "ref/dual_tx_%s.raw" % tag
rx = "ref/dual_rx_%s.raw" % tag

ans = subprocess.Popen(["python3", "-u", "run_answer.py", "--signal", signal,
                        "--level", str(level), "--lead", "0.5", "--ansam-s", "16",
                        "--seconds", "20", "--save-tx", tx]
                       + (["--vbd"] if vbd else [])
                       + (["--pt", str(pt_arg)] if pt_arg is not None else [])
                       + (["--ec-prefix", ecpre] if ecpre else []),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, bufsize=1)
ready = threading.Event()
lines = []
def pa():
    for ln in ans.stdout:
        lines.append(ln.rstrip())
        if "waiting up to" in ln:
            ready.set()
threading.Thread(target=pa, daemon=True).start()
if not ready.wait(40):
    ans.kill()
    print("answerer not ready; its output was:")
    for ln in lines:
        print("   | " + ln)
    sys.exit(1)
time.sleep(1.0)
mp = subprocess.Popen(["ssh", "raspberrypi",
                       "python3 ~/modemprobe/voicecap.py %s '**620' %s 8 /tmp/vc.raw"
                       % (port, codec)],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
mout = mp.communicate()[0]
try: ans.wait(timeout=60)
except Exception: ans.kill()
subprocess.run(["scp", "-q", "raspberrypi:/tmp/vc.raw", rx], check=False)

print("signal=%s level=%s vbd=%s pt=%s" % (signal, level, vbd, pt_arg if pt_arg is not None else 8))
print("  %-34s %-9s %-9s %-8s %s" % ("capture point", "carrier", "depth%", "amRate", "coherence"))
def show(lbl, path, pt):
    if not os.path.exists(path):
        print("  %-34s MISSING" % lbl); return
    raw = open(path, "rb").read()
    if pt in (2, 102):
        x = g726.decode(g726.unpack(raw))
    else:
        x = g711.decode(raw, pt)
    fc, _ = dsp.dominant(x, 800, 3200, coarse=25, fine=1)
    seg = dsp.find_tone_segment(x, fc)
    y = x[seg[0]:seg[1]] if seg else x
    d, r, c = amdepth.am_depth(y, carrier=fc)
    print("  %-34s %-9d %-9.2f %-8.2f %.3f" % (lbl, fc, 100*d, r, c))
show("A: our TX, as actually sent", tx, pt_arg if pt_arg is not None else 8)
show("B: modem analog port (voice ADC)", rx, 0)
