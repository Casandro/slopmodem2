"""Originate to a modem answering in voice mode, and measure at its analog port.

Our SDP is the *offer* here, so V.152 VBD can be negotiated properly rather
than asserted in an answer to an offer that never mentioned it.
"""
import subprocess, threading, sys, time, os
import g711, dsp, amdepth

port, codec, number, probe, level = sys.argv[1:6]
vbd = "--vbd" in sys.argv
codec_arg = None
for i, v in enumerate(sys.argv):
    if v == "--codec" and i + 1 < len(sys.argv):
        codec_arg = sys.argv[i + 1]
tag = "%s_%s%s%s" % (probe, level.replace("-", "m"), "_vbd" if vbd else "",
                     ("_pt" + codec_arg.replace(",", "")) if codec_arg else "")
rx = "ref/op_%s.raw" % tag

mp = subprocess.Popen(["ssh", "raspberrypi",
                       "python3 ~/modemprobe/voiceanswer.py %s %s 8 /tmp/va.raw"
                       % (port, codec)],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
ready = threading.Event()
def pm():
    for ln in mp.stdout:
        print("MODEM| " + ln.rstrip(), flush=True)
        if "READY" in ln:
            ready.set()
threading.Thread(target=pm, daemon=True).start()
if not ready.wait(40):
    mp.kill(); sys.exit("modem not ready")
time.sleep(0.5)
cmd = ["python3", "-u", "run_originate.py", number, "--probe", probe,
       "--level", level, "--seconds", "16"] + (["--vbd"] if vbd else []) \
      + (["--codec", codec_arg] if codec_arg else [])
ans = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, bufsize=1)
for ln in ans.stdout:
    print("SIP  | " + ln.rstrip(), flush=True)
ans.wait(timeout=120)
mp.wait(timeout=120)
subprocess.run(["scp", "-q", "raspberrypi:/tmp/va.raw", rx], check=False)
print()
print("probe=%s level=%s vbd=%s codec=%s" % (probe, level, vbd, codec_arg or "8,0"))
if not os.path.exists(rx):
    sys.exit("no capture")
x = g711.decode(open(rx, "rb").read(), 0 if codec == "131" else 8)
fc, _ = dsp.dominant(x, 800, 3200, coarse=25, fine=1)
seg = dsp.find_tone_segment(x, fc)
y = x[seg[0]:seg[1]] if seg else x
if len(y) < 8000:
    sys.exit("tone too short: %.2f s" % (len(y)/8000.0))
d, r, c = amdepth.am_depth(y, carrier=fc)
print("  at modem analog port: carrier=%d  depth=%.2f%%  rate=%.2f  coh=%.3f  %.1f dBFS"
      % (fc, 100*d, r, c, dsp.dbfs(dsp.rms(y))))
