"""G.711 A-law / mu-law codecs.

Self-contained on purpose: `audioop` was removed in Python 3.13, and this module
must stay free of SIP/credential imports so the DSP can be unit-tested without
the rig. Decode tables are built once at import.
"""

# ---------------- decode ----------------

def _alaw2lin(a):
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t = (t + 0x108) << (seg - 1)
    return t if (a & 0x80) else -t

def _ulaw2lin(u):
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if (u & 0x80) else (t - 0x84)

ALAW = [_alaw2lin(i) for i in range(256)]
ULAW = [_ulaw2lin(i) for i in range(256)]

# ---------------- encode ----------------

_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)

def lin2alaw(pcm):
    """16-bit signed linear -> A-law octet."""
    if pcm > 32767: pcm = 32767
    elif pcm < -32768: pcm = -32768
    pcm >>= 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = 8
    for i, e in enumerate(_SEG_AEND):
        if pcm <= e:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    a = seg << 4
    a |= ((pcm >> 1) & 0x0F) if seg < 2 else ((pcm >> seg) & 0x0F)
    return a ^ mask

_SEG_UEND = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)

def lin2ulaw(pcm):
    """16-bit signed linear -> mu-law octet."""
    if pcm > 32767: pcm = 32767
    elif pcm < -32768: pcm = -32768
    pcm >>= 2                      # to 14-bit, per G.711 reference
    if pcm < 0:
        pcm = -pcm
        mask = 0x7F
    else:
        mask = 0xFF
    if pcm > 8159: pcm = 8159
    pcm += 0x84 >> 2               # BIAS >> 2 == 33
    seg = 8
    for i, e in enumerate(_SEG_UEND):
        if pcm <= e:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    return (((seg << 4) | ((pcm >> (seg + 1)) & 0x0F)) ^ mask) & 0xFF

# A-law / mu-law encoding of digital silence.
ALAW_SILENCE = lin2alaw(0)
ULAW_SILENCE = lin2ulaw(0)

# ---------------- frame helpers ----------------

def decode(payload, pt=8):
    """G.711 payload bytes -> list of linear samples."""
    tbl = ALAW if pt == 8 else ULAW
    return [tbl[b] for b in payload]

def encode(samples, pt=8):
    """Iterable of linear samples -> G.711 payload bytes."""
    enc = lin2alaw if pt == 8 else lin2ulaw
    return bytes(enc(int(s)) for s in samples)

def silence(n=160, pt=8):
    return bytes([ALAW_SILENCE if pt == 8 else ULAW_SILENCE]) * n
