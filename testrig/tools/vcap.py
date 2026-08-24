"""voicecap with the DTE link fast enough for the codec.

16-bit linear at 8 kHz is 16000 byte/s and a 115200 baud port carries 11520, so
the stock capture silently dropped about 28% of its samples -- 229987 bytes for
20 s where 320000 were due. A capture with holes in it is not evidence about a
channel, so the port is opened faster and the achieved rate is reported next to
the expected one rather than left to be assumed.
"""
import serial, time, sys

port, number, codec, secs, out = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]), sys.argv[5]
baud = int(sys.argv[6]) if len(sys.argv) > 6 else 460800
width = int(sys.argv[7]) if len(sys.argv) > 7 else 2      # bytes per sample

s = serial.Serial(port, baud, timeout=0.2)
s.dtr = False; s.rts = False; time.sleep(0.7)
s.dtr = True;  s.rts = True;  time.sleep(0.6)
s.reset_input_buffer()


def cmd(c, w=3.0, until=(b"OK\r\n", b"ERROR\r\n")):
    s.reset_input_buffer(); s.write((c + "\r").encode()); s.flush()
    b = b""; dl = time.time() + w
    while time.time() < dl:
        ch = s.read(4096)
        if ch:
            b += ch
        if any(t in b for t in until):
            break
    return b.decode("latin-1", "replace").replace("\r", " ").strip()[:70]


print("port %s at %d baud (%d byte/s)" % (port, baud, baud // 10), flush=True)
print("ATH0:%s ATZ:%s" % (cmd("ATH0"), cmd("ATZ")), flush=True)
print("FCLASS=8 :%s" % cmd("AT+FCLASS=8"), flush=True)
print("VSM      :%s" % cmd("AT+VSM=%s,8000" % codec), flush=True)
print("dial     :%s" % cmd("ATDT" + number, 25.0,
                           (b"VCON", b"CONNECT", b"OK\r\n", b"NO CARRIER", b"BUSY")), flush=True)
time.sleep(0.4)
print("VRX      :%s" % cmd("AT+VRX", 4.0, (b"CONNECT",)), flush=True)
t0 = time.time(); buf = bytearray()
while time.time() - t0 < secs:
    ch = s.read(16384)
    if ch:
        buf.extend(ch)
el = time.time() - t0
print("captured %d bytes in %.1fs = %.0f byte/s (expected %d)"
      % (len(buf), el, len(buf) / el, 8000 * width), flush=True)
s.write(b"\x10\x18"); s.flush(); time.sleep(0.3); s.read(1 << 20)
s.dtr = False; time.sleep(0.8); s.dtr = True; time.sleep(0.5); s.reset_input_buffer()
cmd("ATH0"); cmd("AT+FCLASS=0"); cmd("ATZ")
print("reset done", flush=True)
o = bytearray(); i = 0
while i < len(buf):
    if buf[i] == 0x10 and i + 1 < len(buf):
        n = buf[i + 1]
        if n == 0x10:
            o.append(0x10); i += 2; continue
        if n == 0x03:
            break
        i += 2; continue
    o.append(buf[i]); i += 1
open(out, "wb").write(bytes(o))
print("wrote %d payload bytes (%.0f samples/s) to %s"
      % (len(o), len(o) / float(width) / el, out), flush=True)
s.close()
