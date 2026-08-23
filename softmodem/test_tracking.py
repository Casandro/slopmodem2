"""Offline tests for the single-pass tracking receiver. No hardware needed.

Each case impairs a locally generated V.22bis signal in one specific way and
asks for the bit stream back. The point of the set is that every impairment
targets one loop: a timing offset and a sample-clock error for the timing loop,
a carrier frequency offset for the carrier loop, a multipath channel for the
equaliser, and a channel that *changes* halfway for the tracking behaviour that
the block equaliser cannot have.
"""
import cmath, math, random, sys, time
import v22, v22bis, tracking

FAIL = []


def check(name, cond, detail=""):
    print("  %-52s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def tx(bits, channel="low", level=-18.0):
    m = v22bis.Mod(channel, level_dbfs=level)
    return m.modulate(bits, scramble=True) + m.flush()


def ber(sent, got, skip=0, probe=200):
    """Bit error rate of `got[skip:]` against `sent`, at the best alignment.

    The alignment is not small and it is not fixed: the differential decode drops
    a quadbit, acquisition consumes an arbitrary number of symbols, and the
    timing loop can settle a symbol or two either way. So find the offset by
    locating a probe taken from the received stream inside the sent stream,
    rather than assuming the shift is within a couple of bits -- an earlier
    version of this helper capped the search at 24 bits and reported BER 0.48 on
    a receiver that was actually decoding perfectly.
    """
    g = got[skip:]
    if len(g) < probe + 400:
        return 1.0, -1, 0
    pat = g[:probe]
    for off in range(0, len(sent) - probe):
        if sent[off:off + probe] == pat:
            n = min(len(sent) - off, len(g))
            e = sum(1 for i in range(n) if sent[off + i] != g[i])
            return e / n, off, n
    # no exact probe match: fall back to the least-bad alignment on the probe
    best = (1.0, -1, 0)
    for off in range(0, len(sent) - probe):
        e = sum(1 for i in range(probe) if sent[off + i] != pat[i])
        if e / probe < best[0]:
            n = min(len(sent) - off, len(g))
            best = (sum(1 for i in range(n) if sent[off + i] != g[i]) / n, off, n)
    return best


def receive(x, carrier=v22.LOW, **kw):
    rx = tracking.TrackingRx(**kw)
    syms, info = rx.run(x, carrier)
    return syms, info, tracking.decode(syms)


def run_case(name, x, bits, skip_syms=1400, carrier=v22.LOW, thresh=0.0,
             **kw):
    syms, info, d = receive(x, carrier, **kw)
    q = tracking.quality(syms, skip=skip_syms)
    r, off, n = ber(bits, d, skip=skip_syms * 4)
    check(name, r <= thresh,
          "BER %.5f over %d bits, median lattice dist %.3f, %d symbols"
          % (r, n, q[0] if q else -1, info["nsym"]))
    return r, info, q


if __name__ == "__main__":
    print("polyphase matched filter and carrier tables")
    tab, d, n = tracking.polyphase()
    check("filter span 81 taps, half-span 40", n == 81 and d == 40, "%d, %d" % (n, d))
    check("sub-phase 0 matches the sampled srrc() table",
          max(abs(a - b) for a, b in zip(tab[0], v22.srrc())) < 1e-9,
          "max diff %.2e" % max(abs(a - b) for a, b in zip(tab[0], v22.srrc())))
    for fc, per in ((1200.0, 20), (2400.0, 10)):
        lut, p = tracking.carrier_lut(fc)
        check("%.0f Hz carrier table is exactly %d samples" % (fc, per), p == per,
              "period %d, wrap error %.2e" % (p, abs(lut[0] - cmath.exp(
                  -1j * 2 * math.pi * fc * p / v22.SR))))
    check("lattice fourth-moment reference is +45 deg",
          abs(math.degrees(tracking.M4_REF) - 45.0) < 1e-9,
          "%.6f deg" % math.degrees(tracking.M4_REF))

    random.seed(7)
    bits = [random.randint(0, 1) for _ in range(24000)]     # 6000 symbols, 10 s
    clean = tx(bits)

    print()
    print("clean channel")
    run_case("no impairment: zero errors after acquisition", clean, bits)

    print()
    print("timing loop")
    # a static sub-sample offset: drop whole samples so the symbol instants land
    # between our sample grid points
    for drop in (3, 7):
        run_case("static offset of %d samples" % drop, clean[drop:], bits)
    # sample-clock error: resample the transmitted signal so the far end's baud
    # rate is wrong by 200 ppm, far beyond V.22bis's +/-0.01% (100 ppm)
    for ppm in (-200, 200):
        rate = 1.0 + ppm * 1e-6
        y, t = [], 0.0
        while t + 1 < len(clean):
            i = int(t); f = t - i
            y.append(clean[i] * (1 - f) + clean[i + 1] * f)
            t += rate
        run_case("sample clock off by %+d ppm" % ppm, y, bits)

    print()
    print("carrier loop")
    for hz in (-7.0, 7.0):
        # V.22bis 2.6 allows +/-7 Hz. A real offset moves the transmit carrier;
        # it is not a multiplication of the passband signal by a cosine.
        m = v22bis.Mod("low", level_dbfs=-18.0)
        m.fc += hz
        y = m.modulate(bits, scramble=True) + m.flush()
        run_case("carrier offset %+.0f Hz (2.6 allows 7)" % hz, y, bits)

    print()
    print("equaliser")
    def conv(x, h):
        out = []
        for i in range(len(x)):
            acc = 0.0
            for k, c in enumerate(h):
                if i - k >= 0:
                    acc += x[i - k] * c
            out.append(acc)
        return out
    run_case("3-tap multipath channel", conv(clean, [1.0, 0.45, -0.2]), bits)
    run_case("5-tap channel with a long echo",
             conv(clean, [1.0, 0.3, 0.15, -0.25, 0.1]), bits)

    print()
    print("tracking a channel that changes under the receiver")
    # These are the cases a block equaliser cannot serve: one tap set has to be
    # a compromise across the whole window.
    #
    # First the realistic one -- a channel that drifts continuously. A tracking
    # equaliser should follow it with no retrain and no errors at all.
    def drift(x, h0, h1, period_s):
        out = []
        for i in range(len(x)):
            f = 0.5 - 0.5 * math.cos(2 * math.pi * i / (period_s * v22.SR))
            h = [a + (b - a) * f for a, b in zip(h0, h1)]
            acc = 0.0
            for k, c in enumerate(h):
                if i - k >= 0:
                    acc += x[i - k] * c
            out.append(acc)
        return out
    r, info, q = run_case("channel drifting continuously: zero errors",
                          drift(clean, [1.0, 0.4, -0.15], [1.0, -0.3, 0.2], 8.0),
                          bits)
    check("  ...and it never needed to retrain", info["retrains"] == 0,
          "%d retrains" % info["retrains"])

    # Then the abrupt one. A discontinuous channel change costs data in any
    # modem -- V.22bis 6.4 clamps circuit 104 through a retrain for exactly this
    # reason -- so what is asserted is that it notices, re-acquires, and is clean
    # afterwards, not that the change is free.
    h1, h2 = [1.0, 0.45, -0.2], [1.0, -0.35, 0.25]
    a, b = conv(clean, h1), conv(clean, h2)
    mid = len(clean) // 2
    ramp = int(0.25 * v22.SR)                      # 250 ms crossfade
    y = []
    for i in range(len(clean)):
        if i < mid:
            y.append(a[i])
        elif i < mid + ramp:
            f = (i - mid) / float(ramp)
            y.append(a[i] * (1 - f) + b[i] * f)
        else:
            y.append(b[i])
    syms, info, d = receive(y)
    check("channel swapped halfway: loss of lock is detected",
          info["retrains"] >= 1, "%d retrains, re-acquired at symbol %d"
          % (info["retrains"], info["dd_at"]))
    r, off, n = ber(bits, d, skip=(info["dd_at"] + 400) * 4)
    check("  ...and it is error-free after re-acquiring", r == 0.0,
          "BER %.5f over %d bits, median %.3f"
          % (r, n, tracking.quality(syms, skip=info["dd_at"] + 400)[0]))

    print()
    print("noise")
    for snr in (30.0, 24.0):
        random.seed(11)
        amp = 32768.0 * 10 ** (-18.0 / 20.0)
        nz = amp * 10 ** (-snr / 20.0)
        y = [v + random.gauss(0, nz) for v in clean]
        run_case("%.0f dB SNR" % snr, y, bits, thresh=0.0 if snr >= 30 else 0.001)

    print()
    print("streaming equivalence: RTP frames vs one array")
    # The live path feeds 160-sample frames through StreamRx; the batch path
    # feeds the whole array. They are the same code, so they must agree exactly --
    # this is the test that makes the offline suite cover the live receiver.
    ra = tracking.StreamRx()
    sa = ra.feed(clean) + ra.close()
    rb = tracking.StreamRx()
    sb = []
    t_worst = 0.0
    for i in range(0, len(clean), 160):
        t0 = time.perf_counter()
        sb += rb.feed(clean[i:i + 160])
        t_worst = max(t_worst, (time.perf_counter() - t0) * 1000.0)
    sb += rb.close()
    check("same symbol count from 160-sample frames", len(sa) == len(sb),
          "%d vs %d" % (len(sa), len(sb)))
    m = min(len(sa), len(sb))
    worst = max((abs(sa[i] - sb[i]) for i in range(m)), default=0.0)
    check("symbols are bit-identical", worst == 0.0, "max difference %.3e" % worst)
    ia, ib = ra.info(), rb.info()
    check("same acquisition point and retrain count",
          ia["dd_at"] == ib["dd_at"] and ia["retrains"] == ib["retrains"],
          "dd_at %d/%d, retrains %d/%d"
          % (ia["dd_at"], ib["dd_at"], ia["retrains"], ib["retrains"]))
    # An RTP frame is 20 ms of audio, so the callback has 20 ms to do everything.
    # The spike to watch for is assess(), which runs a 241-candidate frequency
    # search on a 600-symbol tail during acquisition.
    check("worst frame stays inside the 20 ms RTP budget", t_worst < 20.0,
          "worst %.1f ms per 160-sample frame" % t_worst)

    print()
    print("end-to-end live chain: frames in, characters out")
    # Exactly what run_answer.py does: async characters, scrambled, modulated,
    # then fed to LiveRx a frame at a time.
    import fsm
    pat = b"SOFT2MODEM "
    body = fsm._async_bits(pat, idle=2) * 240        # ~8 s at 200 char/s
    m = v22bis.Mod("low", level_dbfs=-18.0)
    sig = m.modulate(body, scramble=True) + m.flush()
    live = tracking.LiveRx()
    for i in range(0, len(sig), 160):
        live.feed(sig[i:i + 160])
    got = bytes(live.data)
    su = live.summary()
    hits = max(sum(1 for k, c in enumerate(got) if c == pat[(k + p) % len(pat)])
               for p in range(len(pat))) if got else 0
    check("characters recovered", len(got) > 1000, "%d characters" % len(got))
    check("every character correct", got and hits == len(got),
          "%d/%d correct, %d framing errors" % (hits, len(got), su["framing_bad"]))
    check("real-time margin on the live chain", su["worst_ms"] < 20.0,
          "mean %.2f ms, worst %.1f ms per frame (budget 20 ms)"
          % (su["mean_ms"], su["worst_ms"]))

    print()
    print("carrier drop at the end of a call")
    # A live receiver has to stop handing characters on when the far signal dies.
    # V.22bis 6.4 a) clamps circuit 104 on loss of equalisation, and the slow
    # loss-of-lock detector is far too late for it: measured on a real call, the
    # far carrier decayed over about 100 ms and eight wrong characters reached the
    # output before the slow detector noticed.
    tail_body = fsm._async_bits(pat, idle=2) * 240
    m2 = v22bis.Mod("low", level_dbfs=-18.0)
    dying = m2.modulate(tail_body, scramble=True) + m2.flush()
    random.seed(23)
    # 100 ms of decay, then a second of the residual noise floor the box sends
    for k in range(800):
        dying.append(int(dying[-1] * (1.0 - k / 800.0)))
    dying += [int(random.gauss(0, 60)) for _ in range(8000)]
    live2 = tracking.LiveRx()
    for i in range(0, len(dying) - 159, 160):
        live2.feed(dying[i:i + 160])
    got2 = bytes(live2.data)
    hits2 = max(sum(1 for k, c in enumerate(got2) if c == pat[(k + p) % len(pat)])
                for p in range(len(pat))) if got2 else 0
    check("nothing wrong is emitted as the carrier dies",
          got2 and hits2 == len(got2),
          "%d/%d correct, %d symbols withheld by the gate"
          % (hits2, len(got2), live2.summary()["gated"]))

    print()
    if FAIL:
        print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
        sys.exit(1)
    print("all tracking tests passed")
