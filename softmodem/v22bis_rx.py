"""Decode async characters from a captured V.22bis transmission.

The line is synchronous 2400 bit/s; V.22 §4 mode ii) carries start-stop
characters over it, so after descrambling the bit stream is 8N1 framing:
start bit 0, eight data bits LSB first, stop bit 1.
"""
import math, sys
import g711, v22, v22bis, v22bis_const as C, equalise


def deframe(bits):
    """Find 8N1 character framing in a descrambled bit stream."""
    best = None
    for off in range(10):
        chars, good, bad = [], 0, 0
        i = off
        while i + 10 <= len(bits):
            if bits[i] == 0 and bits[i + 9] == 1:
                b = 0
                for k in range(8):
                    b |= bits[i + 1 + k] << k
                chars.append(b)
                good += 1
                i += 10
            else:
                bad += 1
                i += 1
        score = good / max(good + bad, 1)
        if best is None or score > best[0]:
            best = (score, off, chars, good, bad)
    return best


def run(path, a, b, carrier=v22.LOW, taps=11):
    x = g711.decode(open(path, "rb").read(), 8)
    seg = x[int(a * 8000):int(b * 8000)]
    syms, ph, rot, sc = C.extract(seg, carrier)
    hz, _ = equalise.estimate_freq_offset(syms)
    syms = equalise.derotate(syms, hz)
    eq, disp, w = equalise.cma(syms, taps=taps)
    al, r2 = equalise.align(eq)
    out = []
    for extra in (0.0, math.pi / 4):
        cand = [v * complex(math.cos(extra), math.sin(extra)) for v in al]
        tr, _ = equalise.carrier_track(cand)
        ref, mse, _ = equalise.dd_lms(tr, w=[1.0 + 0j if k == taps // 2 else 0j
                                             for k in range(taps)], taps=taps)
        bits = v22bis.decode(ref)
        d = v22.Scrambler().descramble(bits)
        score, off, chars, good, bad = deframe(d)
        text = bytes(c & 0xFF for c in chars).decode("latin-1", "replace")
        out.append((score, good, bad, text, equalise.quality(ref), mse[-1]))
    out.sort(key=lambda t: -t[0])
    score, good, bad, text, q, mse = out[0]
    print("  %.0f-%.0f s: offset %+.3f Hz, median dist %.3f, DD MSE %.4f"
          % (a, b, hz, q[0], mse))
    print("     framing: %d chars accepted, %d bit positions rejected (%.1f%% clean)"
          % (good, bad, 100 * score))
    printable = "".join(ch if 32 <= ord(ch) < 127 else "." for ch in text)
    print("     decoded: %r" % printable[:96])
    return text


if __name__ == "__main__":
    path = sys.argv[1]
    for a, b in ((6.0, 9.0), (9.0, 12.0)):
        run(path, a, b)
