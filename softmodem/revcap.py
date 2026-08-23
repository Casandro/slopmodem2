"""Reverse-direction test: the modem transmits a known AM tone, we capture it.

Path: modem voice DAC -> analog -> FXS -> FRITZ!Box -> RTP -> us.
The modem's voice *receive* path is not involved, so if the 2100 Hz effect
appears here too, it cannot be caused by the modem's receiver.
"""
import subprocess, threading, sys, time, os
import g711, dsp, amdepth, probes

port, codec = sys.argv[1], sys.argv[2]
level = float(sys.argv[3])
signals = sys.argv[4:]

print("  %-9s %-9s %-9s %-8s %-9s %s"
      % ("signal", "carrier", "depth%", "amRate", "coherence", "rxLevel"))
for sig in signals:
    x = probes.SIGNALS[sig](14.0, level_dbfs=level)
    payload = g711.encode(x, 0) if codec == "131" else g711.encode(x, 8)
    open("/tmp/tx.raw", "wb").write(payload)
    subprocess.run(["scp", "-q", "/tmp/tx.raw", "raspberrypi:/tmp/tx.raw"], check=False)

    rx = "ref/rev_%s_%s.raw" % (sig, str(level).replace("-", "m"))
    ans = subprocess.Popen(["python3", "-u", "run_answer.py", "--signal", "flat",
                            "--level", "-90", "--lead", "0.5", "--ansam-s", "1",
                            "--seconds", "20", "--out", rx],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, bufsize=1)
    ready = threading.Event()
    threading.Thread(target=lambda: [ready.set() for ln in ans.stdout
                                     if "waiting up to" in ln], daemon=True).start()
    if not ready.wait(40):
        ans.kill(); print("  %-9s answerer not ready" % sig, flush=True); continue
    time.sleep(1.0)
    mp = subprocess.Popen(["ssh", "raspberrypi",
                           "python3 ~/modemprobe/voicetx.py %s '**620' %s /tmp/tx.raw 14"
                           % (port, codec)],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    mout = mp.communicate()[0]
    for ln in mout.splitlines():
        print("      MODEM| " + ln.rstrip(), flush=True)
    try: ans.wait(timeout=60)
    except Exception: ans.kill()
    if not os.path.exists(rx):
        print("  %-9s no capture (%s)" % (sig, mout.strip().replace("\n", " ")[:60]), flush=True)
        continue
    y = g711.decode(open(rx, "rb").read(), 8)
    fc, _ = dsp.dominant(y, 800, 3200, coarse=25, fine=1)
    seg = dsp.find_tone_segment(y, fc)
    z = y[seg[0]:seg[1]] if seg else y
    if len(z) < 8000:
        print("  %-9s tone too short (%.2fs) | %s"
              % (sig, len(z)/8000.0, mout.strip().replace("\n"," ")[:50]), flush=True)
        continue
    d, r, c = amdepth.am_depth(z, carrier=fc)
    print("  %-9s %-9d %-9.2f %-8.2f %-9.3f %.1f dBFS"
          % (sig, fc, 100*d, r, c, dsp.dbfs(dsp.rms(z))), flush=True)
