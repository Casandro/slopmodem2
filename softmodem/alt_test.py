"""Alternate noise and AM-2100 for a minute; measure each tone slab in order.

Tests whether the answer-tone detector only latches near the start of a call,
or re-acquires every time the tone reappears after an interruption.
"""
import subprocess, threading, sys, time, os
import g711, dsp, amdepth

port, codec = sys.argv[1], sys.argv[2]
signal = sys.argv[3] if len(sys.argv) > 3 else "alt2100"
level = sys.argv[4] if len(sys.argv) > 4 else "-24"
cap_s = float(sys.argv[5]) if len(sys.argv) > 5 else 60.0
rx = "ref/alt_%s_%s.raw" % (signal, level.replace("-", "m"))

ans = subprocess.Popen(["python3", "-u", "run_answer.py", "--signal", signal,
                        "--level", level, "--lead", "0.5",
                        "--ansam-s", str(cap_s + 12), "--seconds", str(cap_s + 14)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, bufsize=1)
ready = threading.Event()
threading.Thread(target=lambda: [ready.set() for ln in ans.stdout
                                 if "waiting up to" in ln], daemon=True).start()
if not ready.wait(40):
    ans.kill(); sys.exit("answerer not ready")
time.sleep(1.0)
mp = subprocess.Popen(["ssh", "raspberrypi",
                       "python3 ~/modemprobe/voicecap.py %s '**620' %s %d /tmp/vc.raw"
                       % (port, codec, int(cap_s))],
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
out = mp.communicate()[0]
for ln in out.splitlines():
    print("MODEM| " + ln.rstrip(), flush=True)
try: ans.wait(timeout=90)
except Exception: ans.kill()
subprocess.run(["scp", "-q", "raspberrypi:/tmp/vc.raw", rx], check=False)
if not os.path.exists(rx):
    sys.exit("no capture")

x = g711.decode(open(rx, "rb").read(), 0 if codec == "131" else 8)
got, want = len(x), int(cap_s * 8000)
print()
pct = 100.0 * got / want
print("capture integrity: %d samples, expected ~%d (%.1f%%) -- %s"
      % (got, want, pct,
         "OK" if pct > 98.0 else "SHORT: dropped serial bytes, timing unreliable"))

# classify 0.5 s windows as tone or noise, group into slabs
W = 4000
cls = []
for i in range(0, len(x) - W + 1, W):
    y = x[i:i + W]
    ms = dsp.mean_square(y)
    # classify on whatever tone is present, not a hardcoded frequency, so the
    # same analysis works for the off-2100 control
    if ms > 200:
        fw, pw = dsp.dominant(y, 1200, 2600, coarse=50, fine=10)
        pur = pw / ms
    else:
        pur = 0.0
    cls.append(pur > 0.30)
slabs = []
i = 0
while i < len(cls):
    if cls[i]:
        j = i
        while j < len(cls) and cls[j]:
            j += 1
        slabs.append((i * W, j * W))
        i = j
    else:
        i += 1
print()
print("tone slabs found: %d" % len(slabs))
print("  %-4s %-13s %-7s %-9s %-8s %s" % ("#", "time", "dur", "depth%", "amRate", "coherence"))
for n, (a, b) in enumerate(slabs, 1):
    trim = 4000
    y = x[a + trim:b - trim]
    if len(y) < 8000:
        print("  %-4d %-13s %-7.1f (too short to measure)"
              % (n, "%.1f-%.1fs" % (a / 8000.0, b / 8000.0), (b - a) / 8000.0))
        continue
    fc, _ = dsp.dominant(y, 1200, 2600, coarse=25, fine=1)
    d, r, c = amdepth.am_depth(y, carrier=fc)
    print("  %-4d %-13s %-7.1f %-9.2f %-8.2f %.3f"
          % (n, "%.1f-%.1fs" % (a / 8000.0, b / 8000.0), (b - a) / 8000.0, 100 * d, r, c))
