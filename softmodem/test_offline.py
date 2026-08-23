"""Offline tests: G.711, V.21 modem, V.8 coding. No hardware needed."""
import math, random, sys
import g711, dsp, v21, v8, ansam

FAIL = []
def check(name, cond, detail=""):
    print("  %-52s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)

def bits_str(b):
    return "".join("." if x is None else str(x) for x in b)

# ---------------- G.711 ----------------
print("G.711")
for pt, name in ((8, "A-law"), (0, "mu-law")):
    x = [int(8000 * math.sin(2 * math.pi * 2100 * i / 8000)) for i in range(1600)]
    y = g711.decode(g711.encode(x, pt), pt)
    err = dsp.rms([a - b for a, b in zip(x, y)])
    snr = 20 * math.log10(dsp.rms(x) / max(err, 1e-9))
    check("%s round-trip SNR > 30 dB" % name, snr > 30, "%.1f dB" % snr)
check("A-law silence decodes near zero", abs(g711.ALAW[g711.ALAW_SILENCE]) <= 8)
check("mu-law silence decodes to exactly zero", g711.ULAW[g711.ULAW_SILENCE] == 0)

# ---------------- ANSam generator vs its own analyser ----------------
print("ANSam (V.8 7.2)")
m = dsp.analyse_ansam(ansam.ansam_samples(6.0, reversal_ms=None), verbose=False)
check("no-reversal variant: centre 2100 Hz", abs(m["centre_hz"] - 2100) <= 1, "%d Hz" % m["centre_hz"])
check("no-reversal variant: AM rate 15 Hz", abs(m["am_rate_hz"] - 15) <= 0.2, "%.1f Hz" % m["am_rate_hz"])
check("no-reversal variant: envelope 0.8..1.2", m["env_min_rel"] > 0.72 and m["env_max_rel"] < 1.28,
      "%.2f..%.2f" % (m["env_min_rel"], m["env_max_rel"]))
check("no-reversal variant: out-of-band >= 24 dB", m["oob_ratio_db"] >= 24, "%.1f dB" % m["oob_ratio_db"])
check("no-reversal variant: no reversals detected", m["n_reversals"] == 0)
m = dsp.analyse_ansam(ansam.ansam_samples(6.0, reversal_ms=450.0, shape="cosine"), verbose=False)
check("cosine450: 13 reversals at 450 ms", m["n_reversals"] == 13 and abs(m["reversal_interval_ms"] - 450) <= 25,
      "%d @ %.0f ms" % (m["n_reversals"], m["reversal_interval_ms"] or 0))
# AM sidebands must be exactly -20 dB for 20% depth, and survive A-law
x = ansam.ansam_samples(4.0, reversal_ms=None, am_depth=0.20, level_dbfs=-20)
for lbl, y in (("linear", x), ("after A-law", g711.decode(g711.encode(x, 8), 8))):
    c = dsp.goertzel(y, 2100.0); lo = dsp.goertzel(y, 2085.0); hi = dsp.goertzel(y, 2115.0)
    d1 = 10 * math.log10(lo / c); d2 = 10 * math.log10(hi / c)
    check("20%% AM sidebands at -20 dB (%s)" % lbl, abs(d1 + 20) < 1.0 and abs(d2 + 20) < 1.0,
          "%.1f / %.1f dB" % (d1, d2))

# ---------------- V.21 modem ----------------
print("V.21 modem")
random.seed(7)
payload = [random.randint(0, 1) for _ in range(200)]
for ch in ("L", "H"):
    for lvl in (-12.0, -24.0, -36.0):
        mod = v21.V21Mod(ch, level_dbfs=lvl)
        x = mod.modulate([1] * 20 + payload + [1] * 8)
        dem = v21.V21Demod(ch)
        got = [b for b in dem.feed(x) if b is not None]
        s = "".join(map(str, payload))
        r = "".join(map(str, got))
        check("ch %s @ %.0f dBFS: payload recovered" % (ch, lvl), s in r,
              "%d bits out" % len(got))
# noise and DC offset
mod = v21.V21Mod("L", level_dbfs=-24)
x = mod.modulate([1] * 20 + payload + [1] * 8)
amp = 32768 * 10 ** (-24 / 20.0)
for snr_db in (20.0, 12.0):
    n = amp * 10 ** (-snr_db / 20.0)
    y = [v + random.gauss(0, n) + 300 for v in x]     # noise plus DC offset
    dem = v21.V21Demod("L")
    got = [b for b in dem.feed(y) if b is not None]
    check("ch L: %.0f dB SNR + DC offset" % snr_db,
          "".join(map(str, payload)) in "".join(map(str, got)))

# ---------------- V.8 coding ----------------
print("V.8 coding")
check("category octet has b4=0", v8.category_octet(v8.TAG_MODULATION)[4] == 0)
e = v8.extension_octet()
check("extension octet has b3=0,b4=1,b5=0", e[3] == 0 and e[4] == 1 and e[5] == 0)
cm = v8.build_cm({"V.21"})
p = v8.parse_octets(cm)
check("CM: call function decodes to data", p["call_function"] == "data (unspecified application)")
check("CM: only V.21 advertised", p["modulations"] == ["V.21"], str(p["modulations"]))
check("CM: three modulation octets", p["n_modulation_octets"] == 3)
full = {"V.34 duplex", "V.32bis/V.32", "V.22bis/V.22", "V.21"}
p2 = v8.parse_octets(v8.build_cm(full))
check("CM: full set round-trips", set(p2["modulations"]) == full, str(sorted(p2["modulations"])))
# no HDLC flag can appear anywhere in an encoded sequence
seq = v8.encode_sequence(v8.build_cm(full))
flag = [0, 1, 1, 1, 1, 1, 1, 0]
check("encoded CM contains no HDLC flag", v8._find(seq, flag) < 0)

# ---------------- end-to-end: V.8 over V.21 ----------------
print("V.8 over V.21 (end to end)")
for modes in ({"V.21"}, full):
    seq = v8.encode_sequence(v8.build_cm(modes))
    mod = v21.V21Mod("L", level_dbfs=-26)
    x = mod.modulate([1] * 20 + seq + seq + seq + [1] * 10)
    dem = v21.V21Demod("L")
    bits = dem.feed(x)
    found = v8.find_sequences(bits)
    ok = False
    for _, octs in found:
        pp = v8.parse_octets(octs)
        if set(pp["modulations"]) == modes and pp["call_function_bits"] == v8.CF_DATA:
            ok = True
            break
    check("CM(%s) survives mod->demod->parse" % ",".join(sorted(modes)), ok,
          "%d sequences found" % len(found))
    check("  >= 2 identical sequences (V.8 8.2.2 needs 2)", len(found) >= 2,
          "%d" % len(found))

# JM intersection
cm = v8.build_cm({"V.34 duplex", "V.32bis/V.32", "V.21"})
jm, common = v8.build_jm(cm, {"V.21"})
check("JM: intersection is V.21", common == ["V.21"], str(common))
jm2, common2 = v8.build_jm(v8.build_cm({"V.34 duplex"}), {"V.21"})
check("JM: no common mode -> empty", common2 == [], str(common2))
p3 = v8.parse_octets(jm2)
check("JM: all-zero modulation bits when nothing in common (8.2.3)",
      p3["modulations"] == [] and p3["n_modulation_octets"] == v8.parse_octets(v8.build_cm({"V.34 duplex"}))["n_modulation_octets"],
      "%d octets" % p3["n_modulation_octets"])

# CJ
print("CJ")
mod = v21.V21Mod("L", level_dbfs=-26)
x = mod.modulate([1] * 20 + v8.encode_cj() + [1] * 10)
dem = v21.V21Demod("L")
check("CJ: three zero octets detected", v8.count_cj(dem.feed(x)) >= 3,
      "%d" % v8.count_cj(dem.feed(v21.V21Mod('L', level_dbfs=-26).modulate([1]*20 + v8.encode_cj() + [1]*10))))

print()
if FAIL:
    print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
    sys.exit(1)
print("all offline tests passed")
