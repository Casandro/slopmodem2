import serial, time, sys
port, secs, extra = sys.argv[1], float(sys.argv[2]), sys.argv[3:]
s = serial.Serial(port, 115200, timeout=0.2)
# mandatory reset: DTR bounce -> ATH0 -> ATZ
s.dtr = False; s.rts = False; time.sleep(0.7)
s.dtr = True;  s.rts = True;  time.sleep(0.6)
s.reset_input_buffer()
def cmd(c, w=3.0):
    s.reset_input_buffer(); s.write((c+"\r").encode()); s.flush()
    b=b""; dl=time.time()+w
    while time.time()<dl:
        ch=s.read(4096)
        if ch: b+=ch
        if b"OK\r\n" in b or b"ERROR\r\n" in b: break
    return b.decode("latin-1","replace").replace("\r"," ").strip()[:60]
print("RESET ATH0:%s ATZ:%s" % (cmd("ATH0"), cmd("ATZ")), flush=True)
for c in extra:
    print("SET %s -> %s" % (c, cmd(c)), flush=True)
print("LISTENING %.0fs (DTR held high)" % secs, flush=True)
t0=time.time(); buf=b""
while time.time()-t0 < secs:
    ch = s.read(4096)
    if ch:
        buf += ch
        txt = ch.decode("latin-1","replace").replace("\r","\\r").replace("\n","\\n")
        print("  [%6.2fs] %s" % (time.time()-t0, txt[:200]), flush=True)
print("TOTAL %d bytes from modem" % len(buf), flush=True)
# leave clean
s.dtr=False; time.sleep(0.6); s.dtr=True; time.sleep(0.4)
cmd("ATH0"); cmd("ATZ")
print("RESET done", flush=True)
s.close()
