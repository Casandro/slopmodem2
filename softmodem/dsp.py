"""DSP primitives and an ANSam analyser.

No SIP/credential imports, so this is testable standalone. Pure Python: there is
no numpy on this host, but the cost is modest (a sliding correlator runs at
~1.4% of realtime per tone at 8 kHz).
"""
import math, cmath

SR = 8000

# ---------------- single-frequency power ----------------

def goertzel(x, f, sr=SR):
    """Mean power of the component at f, comparable to mean square of x.

    Normalised so that a pure sinusoid of amplitude A returns A^2/2.
    """
    n = len(x)
    if n == 0:
        return 0.0
    w = 2 * math.pi * f / sr
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + c * s1 - s2
        s2, s1 = s1, s0
    p = s1 * s1 + s2 * s2 - c * s1 * s2
    return (4.0 * max(p, 0.0) / (n * n)) / 2.0

def mean_square(x):
    return sum(v * v for v in x) / len(x) if x else 0.0

def rms(x):
    return math.sqrt(mean_square(x))

def dbfs(v, full=32768.0):
    return 20 * math.log10(max(v, 1e-9) / full)

def dominant(x, lo=150, hi=3400, coarse=25, fine=1, sr=SR):
    """Dominant frequency by coarse scan then refinement.

    The coarse pass must not integrate longer than its own scan step allows: a
    Goertzel over N samples has a bin roughly sr/N wide, so scanning in `coarse`
    Hz steps over a multi-second record steps straight over the peak and locks
    onto noise. Limit the coarse window so the bin is a fraction of the step,
    then lengthen the window for the refinement passes.
    """
    if not x:
        return lo, 0.0
    n_coarse = min(len(x), max(64, int(4.0 * sr / max(coarse, 1))))
    # Pick the highest-energy window rather than the start of the record: a
    # capture may well begin with silence, and scanning silence locks the
    # coarse pass onto noise.
    best_ms, off = -1.0, 0
    stride = max(1, n_coarse // 2)
    for i in range(0, max(1, len(x) - n_coarse + 1), stride):
        ms = mean_square(x[i:i + n_coarse])
        if ms > best_ms:
            best_ms, off = ms, i
    xc = x[off:off + n_coarse]
    best, bf = -1.0, lo
    f = lo
    while f <= hi:
        p = goertzel(xc, f, sr)
        if p > best:
            best, bf = p, f
        f += coarse
    # refine with progressively longer windows / finer steps
    step = coarse / 2.0
    span = coarse
    while step >= fine:
        n = min(len(x), max(64, int(4.0 * sr / max(step, 0.5))))
        start = min(off, max(0, len(x) - n))
        xr = x[start:start + n]
        best = -1.0
        centre = bf
        f = max(lo, centre - span)
        while f <= min(hi, centre + span):
            p = goertzel(xr, f, sr)
            if p > best:
                best, bf = p, f
            f += step
        span = max(step * 4.0, 4.0)
        step /= 2.0
    return int(round(bf)), best

# ---------------- spectrum ----------------

def periodogram(x, sr=SR, block=800):
    """Average power spectrum on an (sr/block) Hz grid. Returns {freq: power}.

    Uses a Hann window; power is normalised so a full-scale sinusoid sitting on a
    bin centre reads about A^2/2.
    """
    nb = len(x) // block
    if nb == 0:
        return {}
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / block) for i in range(block)]
    wnorm = sum(w * w for w in win) / block
    nbins = block // 2
    acc = [0.0] * nbins
    for b in range(nb):
        seg = x[b * block:(b + 1) * block]
        wd = [seg[i] * win[i] for i in range(block)]
        for k in range(nbins):
            w = 2 * math.pi * k / block
            c = 2 * math.cos(w)
            s1 = s2 = 0.0
            for v in wd:
                s0 = v + c * s1 - s2
                s2, s1 = s1, s0
            p = s1 * s1 + s2 * s2 - c * s1 * s2
            acc[k] += (4.0 * max(p, 0.0) / (block * block)) / 2.0 / wnorm
    step = sr / block
    return {k * step: acc[k] / nb for k in range(nbins)}

def band_ratio_db(x, fc=2100.0, halfwidth=200.0, sr=SR, block=800):
    """In-band vs out-of-band power ratio, in dB. V.8 7.2 wants >= 24 dB."""
    spec = periodogram(x, sr, block)
    if not spec:
        return float("nan"), 0.0, 0.0
    inb = sum(p for f, p in spec.items() if abs(f - fc) <= halfwidth)
    tot = sum(spec.values())
    oob = max(tot - inb, 1e-12)
    return 10 * math.log10(max(inb, 1e-12) / oob), inb, oob

# ---------------- sliding complex demodulator ----------------

def demod_envelope_phase(x, f, sr=SR, win=24, step=4):
    """Sliding complex demod at f.

    Returns (times, envelope, phase) sampled every `step` input samples.
    `win` should span a few carrier cycles: long enough for a clean estimate,
    short enough to track 15 Hz AM and 450 ms phase reversals.
    """
    w = 2 * math.pi * f / sr
    cs = [complex(math.cos(w * k), -math.sin(w * k)) for k in range(win)]
    ts, env, ph = [], [], []
    for n in range(0, len(x) - win + 1, step):
        acc = 0j
        for k in range(win):
            acc += x[n + k] * cs[k]
        acc *= 2.0 / win
        ts.append((n + win / 2.0) / sr)
        env.append(abs(acc))
        ph.append(cmath.phase(acc))
    return ts, env, ph

# ---------------- ANSam analysis ----------------

def sliding_mag(x, f, sr=SR, win=16, step=4):
    """Magnitude of the component at f, short window, for envelope tracking.

    A short window keeps a phase reversal localised to a couple of outputs,
    which is what makes reversal detection possible.
    """
    w = 2 * math.pi * f / sr
    cs = [complex(math.cos(w * k), -math.sin(w * k)) for k in range(win)]
    ts, mag = [], []
    for n in range(0, len(x) - win + 1, step):
        acc = 0j
        for k in range(win):
            acc += x[n + k] * cs[k]
        ts.append((n + win / 2.0) / sr)
        mag.append(abs(acc) * 2.0 / win)
    return ts, mag

def _median(v):
    if not v:
        return 0.0
    q = sorted(v)
    n = len(q)
    return q[n // 2] if n % 2 else 0.5 * (q[n // 2 - 1] + q[n // 2])

def find_tone_segment(x, f, sr=SR, max_gap=6):
    """Longest run where f is present.

    Reversal-tolerant: uses a short-window magnitude so a 180 deg flip only
    notches one or two outputs, then fills gaps up to `max_gap` outputs before
    picking the longest run.
    """
    ts, mag = sliding_mag(x, f, sr, win=16, step=8)
    if not mag:
        return None
    thr = max(0.25 * max(mag), 40.0)
    on = [m > thr for m in mag]
    # fill short notches that are bounded by signal on both sides
    i = 0
    while i < len(on):
        if not on[i]:
            j = i
            while j < len(on) and not on[j]:
                j += 1
            if i > 0 and j < len(on) and (j - i) <= max_gap:
                for k in range(i, j):
                    on[k] = True
            i = j
        else:
            i += 1
    # longest True run
    best_a = best_len = 0
    i = 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]:
                j += 1
            if j - i > best_len:
                best_a, best_len = i, j - i
            i = j
        else:
            i += 1
    if best_len == 0:
        return None
    a = int(ts[best_a] * sr)
    b = int(ts[min(best_a + best_len, len(ts) - 1)] * sr)
    return max(0, a), min(len(x), b)

def find_reversals(x, f, sr=SR, win=16, step=4, span=8, thresh=2.0):
    """Phase-reversal instants, from the de-trended phase of the complex demod.

    Envelope-notch detection is not viable: a raised-cosine reversal only dips
    the magnitude to about 2/pi (~0.64) of nominal, which is shallower than the
    0.8 trough of the 15 Hz AM, so any fixed magnitude threshold either misses
    reversals or fires on AM troughs. Phase is unambiguous -- a reversal is a
    pi step regardless of how it is shaped.

    The phase difference is taken over `span` outputs (span*step samples, longer
    than a typical reversal ramp) and de-trended by its median, which absorbs
    any offset between `f` and the true carrier frequency.
    """
    ts, mag, ph = [], [], []
    w = 2 * math.pi * f / sr
    cs = [complex(math.cos(w * k), -math.sin(w * k)) for k in range(win)]
    for n in range(0, len(x) - win + 1, step):
        acc = 0j
        for k in range(win):
            acc += x[n + k] * cs[k]
        ts.append((n + win / 2.0) / sr)
        mag.append(abs(acc) * 2.0 / win)
        ph.append(cmath.phase(acc))
    if len(ph) <= span:
        return []
    floor = 0.15 * _median([m for m in mag if m > 0]) if mag else 0.0

    def wrap(a):
        return ((a + math.pi) % (2 * math.pi)) - math.pi

    d = [wrap(ph[i + span] - ph[i]) for i in range(len(ph) - span)]
    med = _median(d)
    revs = []
    i = 0
    while i < len(d):
        # ignore intervals where the carrier is essentially absent
        if mag[i] < floor or mag[i + span] < floor:
            i += 1
            continue
        if abs(wrap(d[i] - med)) > thresh:
            j = i
            while j + 1 < len(d) and abs(wrap(d[j + 1] - med)) > thresh:
                j += 1
            t = 0.5 * (ts[i] + ts[min(j + span, len(ts) - 1)])
            if not revs or t - revs[-1] > 0.08:
                revs.append(t)
            i = j + 1
        else:
            i += 1
    return revs

def _reversal_free_chunks(n, sr, revs, guard=0.006, minlen=0.15):
    """Sample ranges between reversals, with a guard either side."""
    edges = [0.0] + list(revs) + [n / sr]
    out = []
    for i in range(len(edges) - 1):
        a = edges[i] + (guard if i > 0 else 0.0)
        b = edges[i + 1] - (guard if i + 1 < len(edges) - 1 else 0.0)
        if b - a >= minlen:
            out.append((int(a * sr), int(b * sr)))
    return out

def analyse_ansam(x, sr=SR, verbose=True):
    """Measure a candidate ANSam against ITU-T V.8 7.2."""
    out = {}
    seg = find_tone_segment(x, 2100.0, sr)
    if seg is None:
        if verbose:
            print("  no sustained 2100 Hz segment found")
        return None
    a, b = seg
    y = x[a:b]
    if len(y) < sr // 4:
        if verbose:
            print("  2100 Hz segment too short to analyse (%.2f s)" % (len(y) / sr))
        return None
    out["segment_s"] = (a / sr, b / sr)
    out["duration_s"] = len(y) / sr
    out["level_dbfs"] = dbfs(rms(y))

    revs = find_reversals(y, 2100.0, sr)
    out["n_reversals"] = len(revs)
    if len(revs) >= 2:
        gaps = [revs[i + 1] - revs[i] for i in range(len(revs) - 1)]
        out["reversal_interval_ms"] = 1000.0 * sum(gaps) / len(gaps)
        out["reversal_jitter_ms"] = 1000.0 * (max(gaps) - min(gaps)) / 2
    else:
        out["reversal_interval_ms"] = None
        out["reversal_jitter_ms"] = None

    chunks = _reversal_free_chunks(len(y), sr, revs)
    if not chunks:
        chunks = [(0, len(y))]
    longest = max(chunks, key=lambda c: c[1] - c[0])
    clean = y[longest[0]:longest[1]]
    out["clean_chunk_s"] = (longest[1] - longest[0]) / sr

    # centre frequency and out-of-band ratio measured on reversal-free audio:
    # V.8 7.2 ties the 24 dB figure to the AM envelope approximation, and
    # reversals are a separately permitted feature.
    fc, _ = dominant(clean, 2050, 2150, coarse=5, fine=1, sr=sr)
    out["centre_hz"] = fc
    out["oob_ratio_db"], _, _ = band_ratio_db(clean, fc, 200.0, sr)
    out["oob_ratio_all_db"], _, _ = band_ratio_db(y, fc, 200.0, sr)

    # AM measured across all reversal-free chunks
    env_all = []
    for (ca, cb) in chunks:
        _, e = sliding_mag(y[ca:cb], fc, sr, win=24, step=4)
        env_all.extend(e)
    if len(env_all) > 50:
        avg = sum(env_all) / len(env_all)
        srt = sorted(env_all)
        lo = srt[int(0.02 * len(srt))]
        hi = srt[int(0.98 * len(srt))]
        out["env_avg"] = avg
        out["env_min_rel"] = lo / avg if avg else 0.0
        out["env_max_rel"] = hi / avg if avg else 0.0
        out["am_depth_pct"] = 100.0 * (hi - lo) / (2 * avg) if avg else 0.0
        _, e_long = sliding_mag(clean, fc, sr, win=24, step=4)
        if len(e_long) > 50:
            m = sum(e_long) / len(e_long)
            d = [v - m for v in e_long]
            esr = sr / 4.0
            bestp, bestf = -1.0, 0.0
            f = 5.0
            while f <= 30.0:
                p = goertzel(d, f, esr)
                if p > bestp:
                    bestp, bestf = p, f
                f += 0.1
            out["am_rate_hz"] = bestf
            out["am_purity"] = bestp / max(mean_square(d), 1e-12)
    if verbose:
        report_ansam(out)
    return out

def report_ansam(m):
    if not m:
        return
    def spec(ok):
        return "OK " if ok else "OFF"
    print("  segment          %.2f-%.2f s (%.2f s), clean chunk %.2f s"
          % (m["segment_s"][0], m["segment_s"][1], m["duration_s"], m.get("clean_chunk_s", 0)))
    print("  level            %.1f dBFS" % m["level_dbfs"])
    print("  centre freq      %d Hz          %s  (spec 2100 +/- 1)"
          % (m["centre_hz"], spec(abs(m["centre_hz"] - 2100) <= 1)))
    print("  out-of-band      %.1f dB down    %s  (spec >= 24; %.1f dB incl. reversals)"
          % (m["oob_ratio_db"], spec(m["oob_ratio_db"] >= 24), m["oob_ratio_all_db"]))
    if m.get("am_rate_hz") is not None:
        print("  AM rate          %.1f Hz         %s  (spec 15 +/- 0.1)"
              % (m["am_rate_hz"], spec(abs(m["am_rate_hz"] - 15) <= 0.5)))
        print("  AM envelope      %.2f..%.2f x avg  %s  (spec 0.8..1.2)"
              % (m["env_min_rel"], m["env_max_rel"],
                 spec(m["env_min_rel"] > 0.72 and m["env_max_rel"] < 1.28)))
        print("  AM purity        %.2f" % m["am_purity"])
    else:
        print("  AM               not measurable")
    print("  phase reversals  %d" % m["n_reversals"], end="")
    if m["reversal_interval_ms"]:
        print("  every %.0f ms +/-%.0f  %s  (spec 450 +/- 25)"
              % (m["reversal_interval_ms"], m["reversal_jitter_ms"],
                 spec(abs(m["reversal_interval_ms"] - 450) <= 25)))
    else:
        print("   (none detected - permitted by 7.2)")

if __name__ == "__main__":
    import sys, g711
    args = [a for a in sys.argv[1:]]
    if "--analyse-ansam" in args:
        path = args[args.index("--analyse-ansam") + 1]
        pt = 0 if "--ulaw" in args else 8
        raw = open(path, "rb").read()
        x = g711.decode(raw, pt)
        print("%s: %d samples (%.1f s)" % (path, len(x), len(x) / 8000.0))
        analyse_ansam(x)
    else:
        print(__doc__)
        print("usage: python3 -m dsp --analyse-ansam FILE.raw [--ulaw]")
