"""Measure the coding gain of the recovered V.32 trellis code.

A free distance is an asymptote. What a modem actually cares about is how much
less signal-to-noise it needs for a usable error rate, so that is what this
measures: BER against SNR for the trellis code and for the uncoded 16-point set
at the same rate (4 bit/symbol) and the same mean power (10), which is what makes
the comparison fair.

Both curves are wanted, because a trellis code is not uniformly better. Below its
threshold, error events merge and it loses to the uncoded set; the gain appears
where the error rate is low enough to be worth having.

  python3 v32_gain.py --symbols 200000
"""
import argparse, math, random, sys
import v32


def trellis_ber(snr_db, nsym, seed):
    random.seed(seed)
    g = random.gauss
    sigma = math.sqrt(10.0 / (2 * 10 ** (snr_db / 10.0)))
    bits = [random.randint(0, 1) for _ in range(4 * nsym)]
    pts = v32.TrellisEncoder().encode(bits)
    dec = v32.TrellisDecoder(depth=24)
    got = []
    for z in pts:
        for q in dec.feed(complex(z.real + g(0, sigma), z.imag + g(0, sigma))):
            got.extend(q)
    for q in dec.flush():
        got.extend(q)
    off = len(bits) - len(got)
    err = sum(1 for a, b in zip(bits[off:], got) if a != b)
    return err / float(len(got)), err


def uncoded_ber(snr_db, nsym, seed):
    random.seed(seed ^ 0x5EED)
    g = random.gauss
    sigma = math.sqrt(10.0 / (2 * 10 ** (snr_db / 10.0)))
    items = [(k, complex(*v)) for k, v in v32.NONRED.items()]
    labels = [k for k, _ in items]
    err = 0
    for _ in range(nsym):
        lab = labels[random.randrange(16)]
        z = dict(items)[lab] + complex(g(0, sigma), g(0, sigma))
        best = min(items, key=lambda kv: abs(kv[1] - z))[0]
        err += sum(a != b for a, b in zip(lab, best))
    return err / (4.0 * nsym), err


def cross(curve, target):
    """SNR at which a BER curve crosses a target, log-linear between points."""
    for (s0, b0), (s1, b1) in zip(curve, curve[1:]):
        if b0 > target >= b1 and b1 > 0:
            f = (math.log(b0) - math.log(target)) / (math.log(b0) - math.log(b1))
            return s0 + f * (s1 - s0)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", type=int, default=200000)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    d2 = v32.trellis_free_distance()
    u2 = min(abs(complex(*p) - complex(*q)) ** 2
             for i, p in enumerate(v32.NONRED.values())
             for q in list(v32.NONRED.values())[i + 1:])
    print("free distance of the recovered code : d^2 = %.3f" % d2)
    print("uncoded 16-point minimum distance   : d^2 = %.3f" % u2)
    print("mean power, both sets               : %.3f / %.3f"
          % (sum(x * x + y * y for x, y in v32.NONRED.values()) / 16.0,
             sum(abs(z) ** 2 for z in v32.TRELLIS_POINTS) / 32.0))
    print("=> asymptotic coding gain           : %.2f dB"
          % (10 * math.log10(d2 / u2)))
    print()
    print(" SNR dB |    trellis BER    |   uncoded BER")
    tc, uc = [], []
    for snr in [11 + 0.5 * i for i in range(21)]:
        t, te = trellis_ber(snr, a.symbols, a.seed)
        u, ue = uncoded_ber(snr, a.symbols, a.seed)
        tc.append((snr, t))
        uc.append((snr, u))
        print("  %4.1f  |  %.3e (%5d) |  %.3e (%5d)" % (snr, t, te, u, ue),
              flush=True)
    print()
    for tgt in (1e-2, 1e-3, 1e-4, 1e-5):
        st, su = cross(tc, tgt), cross(uc, tgt)
        if st and su:
            print("  BER %.0e needs %.2f dB coded, %.2f dB uncoded"
                  "  -> gain %+.2f dB" % (tgt, st, su, su - st))
        else:
            print("  BER %.0e not bracketed by this sweep" % tgt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
