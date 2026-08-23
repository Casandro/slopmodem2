"""ITU-T V.8 signal coding: CI / CM / JM / CJ.

Coding format (V.8 5): each sequence is 10 ONEs, then 10 synchronisation bits,
then information octets, each preceded by a start bit (ZERO) and followed by a
stop bit (ONE). b0 is transmitted first and is the least significant bit of the
category tag.

Category octets (5.1):   b0..b3 = category tag, b4 = 0,          b5..b7 options
Extension octets (5.2):  b0..b2 options, b3 = 0, b4 = 1, b5 = 0, b6..b7 options
The fixed bits exist to stop the bit stream simulating an HDLC flag (01111110),
so they double as a useful self-check that framing is correct.
"""

PREAMBLE = [1] * 10
SYNC_CI = [0, 0, 0, 0, 0, 0, 0, 0, 0, 1]
SYNC_CM = [0, 0, 0, 0, 0, 0, 1, 1, 1, 1]     # CM and JM (Table 1)

# category tags, as b0..b3 in transmission order (Table 2)
TAG_CALL_FUNCTION = [1, 0, 0, 0]
TAG_MODULATION    = [1, 0, 1, 0]
TAG_PROTOCOLS     = [0, 1, 0, 1]
TAG_PSTN_ACCESS   = [1, 0, 1, 1]
TAG_NONSTANDARD   = [1, 1, 1, 1]
TAG_PCM_MODEM     = [1, 1, 1, 0]
TAG_T66           = [0, 1, 1, 1]

TAGS = {
    tuple(TAG_CALL_FUNCTION): "call_function",
    tuple(TAG_MODULATION):    "modulation",
    tuple(TAG_PROTOCOLS):     "protocols",
    tuple(TAG_PSTN_ACCESS):   "pstn_access",
    tuple(TAG_NONSTANDARD):   "nonstandard",
    tuple(TAG_PCM_MODEM):     "pcm_modem",
    tuple(TAG_T66):           "t66",
}

# call function option bits b5,b6,b7 (Table 3)
CALL_FUNCTIONS = {
    (0, 0, 0): "reserved",
    (1, 0, 0): "PSTN multimedia terminal (H.324)",
    (0, 1, 0): "textphone (V.18)",
    (1, 1, 0): "videotext (T.101)",
    (0, 0, 1): "transmit fax from call terminal (T.30)",
    (1, 0, 1): "receive fax at call terminal (T.30)",
    (0, 1, 1): "data (unspecified application)",
    (1, 1, 1): "see extension octet",
}
CF_DATA = (0, 1, 1)

# Modulation modes (Table 4). Each entry maps a name to (octet index, bit index)
# where the octet index is 0..2 within modn0/modn1/modn2.
MODULATIONS = [
    ("V.34 duplex",      0, 6),
    ("V.34 half-duplex", 0, 7),
    ("V.32bis/V.32",     1, 0),
    ("V.22bis/V.22",     1, 1),
    ("V.17",             1, 2),
    ("V.29 half-duplex", 1, 6),
    ("V.27ter",          1, 7),
    ("V.26ter",          2, 0),
    ("V.26bis",          2, 1),
    ("V.23 duplex",      2, 2),
    ("V.23 half-duplex", 2, 6),
    ("V.21",             2, 7),
]
PCM_PRESENT_BIT = (0, 5)

# ---------------- octet construction ----------------

def category_octet(tag, b5=0, b6=0, b7=0):
    return list(tag) + [0, b5, b6, b7]

def extension_octet(b0=0, b1=0, b2=0, b6=0, b7=0):
    return [b0, b1, b2, 0, 1, 0, b6, b7]

def call_function_octet(cf=CF_DATA):
    return category_octet(TAG_CALL_FUNCTION, *cf)

def modulation_octets(modes):
    """Three octets (modn0/modn1/modn2) advertising `modes` (a set of names)."""
    o = [category_octet(TAG_MODULATION), extension_octet(), extension_octet()]
    for name, oi, bi in MODULATIONS:
        if name in modes:
            o[oi][bi] = 1
    return o

def is_extension(octet):
    return octet[4] == 1

def fixed_bits_ok(octet):
    """b4=0 for a category octet; b3=0,b4=1,b5=0 for an extension octet."""
    if octet[4] == 0:
        return True
    return octet[3] == 0 and octet[5] == 0

# ---------------- framing ----------------

def frame_octet(octet):
    return [0] + list(octet) + [1]

def encode_sequence(octets, sync=None):
    bits = list(PREAMBLE) + list(sync if sync is not None else SYNC_CM)
    for o in octets:
        bits += frame_octet(o)
    return bits

def build_cm(modes, cf=CF_DATA):
    return [call_function_octet(cf)] + modulation_octets(modes)

def build_jm(cm_octets, our_modes, cf=None):
    """JM per V.8 8.2.3.

    JM shows the modes present in CM *and* available here. If there is nothing
    in common, JM carries the same number of modulation octets as CM with all
    modulation bits zero -- the caller is then entitled to disconnect after CJ.
    """
    parsed = parse_octets(cm_octets)
    cm_modes = set(parsed["modulations"])
    common = cm_modes & set(our_modes)
    n_mod = parsed["n_modulation_octets"]
    if cf is None:
        cf = parsed["call_function_bits"] or CF_DATA
    out = [call_function_octet(tuple(cf))]
    if n_mod:
        mods = modulation_octets(common)[:max(n_mod, 1)]
        out += mods
    return out, sorted(common)

CJ_OCTETS = [[0] * 8, [0] * 8, [0] * 8]

def encode_cj():
    """CJ: three consecutive all-ZERO octets with start and stop bits (3.5).
    No preamble -- CJ terminates a CM, it is not a new sequence."""
    bits = []
    for o in CJ_OCTETS:
        bits += frame_octet(o)
    return bits

# ---------------- parsing ----------------

def _find(seq, pat, start=0):
    for i in range(start, len(seq) - len(pat) + 1):
        if seq[i:i + len(pat)] == pat:
            return i
    return -1

def find_sequences(bits, sync=None, min_ones=8):
    """Locate sequences in a raw bit stream.

    Returns a list of (index_after_sync, octets). Framing errors truncate that
    sequence rather than aborting the scan, since a repetitive CM gives many
    chances to get a clean copy.
    """
    sync = list(sync if sync is not None else SYNC_CM)
    clean = [b for b in bits if b is not None]
    out = []
    i = 0
    pat = [1] * min_ones + sync
    while True:
        j = _find(clean, pat, i)
        if j < 0:
            break
        p = j + len(pat)
        octets = []
        while p + 10 <= len(clean):
            fr = clean[p:p + 10]
            if fr[0] != 0 or fr[9] != 1:
                break
            oct_ = fr[1:9]
            if not fixed_bits_ok(oct_):
                break
            octets.append(oct_)
            p += 10
        if octets:
            out.append((j, octets))
        i = j + 1
    return out

def parse_octets(octets):
    """Decode a sequence's octets into a capability description."""
    res = {"call_function": None, "call_function_bits": None,
           "modulations": [], "n_modulation_octets": 0,
           "categories": [], "pcm_present": False, "raw": []}
    for o in octets:
        res["raw"].append(octet_hex(o))
    i = 0
    while i < len(octets):
        o = octets[i]
        if is_extension(o):
            i += 1
            continue
        name = TAGS.get(tuple(o[0:4]), "unknown(%s)" % "".join(map(str, o[0:4])))
        res["categories"].append(name)
        group = [o]
        j = i + 1
        while j < len(octets) and is_extension(octets[j]):
            group.append(octets[j])
            j += 1
        if name == "call_function":
            bits = (o[5], o[6], o[7])
            res["call_function_bits"] = bits
            res["call_function"] = CALL_FUNCTIONS.get(bits, "unknown")
        elif name == "modulation":
            res["n_modulation_octets"] = len(group)
            if group[PCM_PRESENT_BIT[0]][PCM_PRESENT_BIT[1]] if len(group) > PCM_PRESENT_BIT[0] else 0:
                res["pcm_present"] = True
            for mname, oi, bi in MODULATIONS:
                if oi < len(group) and group[oi][bi]:
                    res["modulations"].append(mname)
        i = j
    return res

def octet_hex(octet):
    """Hex with b0 as the least significant bit, matching +A8M reporting."""
    v = 0
    for k, b in enumerate(octet):
        v |= (b & 1) << k
    return "%02X" % v

def count_cj(bits):
    """Number of consecutive all-zero framed octets (CJ needs 3)."""
    clean = [b for b in bits if b is not None]
    zero = frame_octet([0] * 8)
    best = run = 0
    i = 0
    while i + 10 <= len(clean):
        if clean[i:i + 10] == zero:
            run += 1
            best = max(best, run)
            i += 10
        else:
            run = 0
            i += 1
    return best
