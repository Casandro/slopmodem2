"""Tests for the echo canceller.

The case that matters most is the one where there is *no* echo, because a
canceller is only worth having if it cannot make a clean line worse. So that is
tested first and hardest -- including across several independent signal pairs,
since "it did not false-lock this once" is luck and not a property.

Double talk is tested as the normal condition rather than an edge case: on a full
duplex circuit the far end is always talking, and here its signal arrives about
19 dB *above* the echo being removed.
"""
import math
import random
import sys

import echo

FAIL = []


def check(name, cond, detail=""):
    print("  %-66s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


def v32ish(n, seed, amp=2000.0):
    """Something with the statistics of a V.32 line signal: an 1800 Hz carrier
    stepped through random quadrant phases at roughly the symbol rate."""
    rnd = random.Random(seed)
    out = []
    sym = 0
    for i in range(n):
        if i % 3 == 0:
            sym = rnd.randrange(4)
        out.append(amp * math.cos(2.0 * math.pi * 1800.0 * i / echo.SR
                                  + sym * math.pi / 2.0))
    return out


def run(ec, near, far, ir, frame=160):
    """Push `far` as what we emitted, deliver `near` plus the echo of `far`.

    `ir` is the echo path as (delay, gain) pairs. The transmit history is pushed
    *after* the inbound frame is fed, which is the order the state machine
    imposes: what we send this frame is decided by what we just heard.
    """
    n = min(len(near), len(far))
    got = []
    for k in range(0, n - frame, frame):
        inb = []
        for j in range(k, k + frame):
            v = near[j]
            for d, g in ir:
                if j - d >= 0:
                    v += g * far[j - d]
            inb.append(v)
        got.extend(ec.feed(inb))
        ec.push_tx(far[k:k + frame])
    return got


def power(xs):
    return sum(v * v for v in xs) / len(xs) if xs else 0.0


def erle_db(got, near, far, gain, delay, frac=3):
    """Echo return loss enhancement over the last 1/frac of the run."""
    tail = len(got) // frac
    lo = len(got) - tail
    resid = [a - b for a, b in zip(got[-tail:], near[lo:len(got)])]
    only = [gain * far[j - delay] for j in range(lo, len(got))]
    pr, po = power(resid), power(only)
    return 10.0 * math.log10(po / pr) if pr > 0 and po > 0 else 0.0


GAIN = 10 ** (-19.0 / 20.0)      # the 19 dB return loss measured on this rig
# A physically possible delay. The echo cannot return in less than one RTP frame:
# our frame k goes out only after inbound frame k is read, and the far end has to
# packetise what it reflects. 237 samples is 29.6 ms.
DELAY = 237


if __name__ == "__main__":
    print("no echo at all -- the case that must not break anything")
    far = v32ish(8000 * 8, 1)
    near = v32ish(8000 * 8, 2)
    ec = echo.EchoCanceller(budget=64, win=8192)
    got = run(ec, near, far, [])
    err = [a - b for a, b in zip(got, near[:len(got)])]
    check("with no echo the output is the input, sample for sample",
          power(err) == 0.0,
          "residual %.3e" % (power(err) / max(power(near), 1e-30)))
    check("  not one tap ever moved, because it never locked",
          not ec.locked and max(abs(v) for v in ec.w) == 0.0)
    check("  and it did search, so the negative is a decision not inaction",
          ec.searches >= 2, "%d searches, best rho %.3f against threshold %.3f"
          % (ec.searches, ec.best_rho, ec.last_thresh))
    check("  no guard resets were needed", ec.resets == 0)

    # One clean run is luck. The threshold is a statistical claim, so test it
    # against several independent pairs.
    locks = 0
    rhos = []
    for seed in range(10, 16):
        e2 = echo.EchoCanceller(budget=64, win=8192)
        run(e2, v32ish(8000 * 5, seed), v32ish(8000 * 5, seed + 100), [])
        locks += 1 if e2.locked else 0
        rhos.append(e2.best_rho)
    check("  six independent echo-free pairs and not one false lock",
          locks == 0, "peak rho seen %.3f, threshold %.3f"
          % (max(rhos), echo.NULL_K * echo.null_rho(
              echo.SEARCH_MAX - echo.SEARCH_MIN, 8192 // 2)))

    # The threshold is a claim about the shipped configuration, so check that
    # configuration's numbers rather than the cheap one the tests above use.
    lags = echo.SEARCH_MAX - echo.SEARCH_MIN
    terms = echo.SEARCH_WIN // echo.SEARCH_STEP
    null = echo.null_rho(lags, terms)
    check("  the shipped threshold clears the shipped null with margin",
          echo.NULL_K * null < 0.06 and null < 0.03,
          "%d lags, %d terms: null %.4f, threshold %.4f, against 0.12 measured "
          "for a real echo" % (lags, terms, null, echo.NULL_K * null))
    # The range runs from the physical floor -- one RTP frame -- to 100 ms.
    # It deliberately does not encode particular delays measured on the rig:
    # those numbers were taken from the capture files, which lead the receive
    # stream by the pump's two priming frames, so every one of them was 320
    # samples too small.
    check("  and the range runs from one frame to 100 ms",
          echo.SEARCH_MIN == echo.FRAME and echo.SEARCH_MAX >= 800,
          "range %d..%d samples = %.1f..%.1f ms"
          % (echo.SEARCH_MIN, echo.SEARCH_MAX,
             echo.SEARCH_MIN / 8.0, echo.SEARCH_MAX / 8.0))

    print()
    print("the stream it produces has to be usable by an equaliser")
    check("nothing is held back: out equals in, sample for sample",
          echo.HOLD == 0 and abs(len(got) - (8000 * 8 - 160)) <= 160,
          "%d samples out for %d in, hold %d"
          % (len(got), 8000 * 8, echo.HOLD))
    ec3 = echo.EchoCanceller(budget=64, win=8192)
    lens = []
    for k in range(0, 8000, 160):
        lens.append(len(ec3.feed(near[k:k + 160])))
        ec3.push_tx(far[k:k + 160])
    check("  and it comes out in steady 160-sample frames from the first one",
          sorted(set(lens)) == [160],
          "frame sizes: %s" % sorted(set(lens)))

    # A correlation peak below one frame is not a short echo, it is noise, and
    # fitting the filter to it can only add to the residual.
    check("the search will not even look below one RTP frame",
          echo.SEARCH_MIN >= echo.FRAME,
          "search starts at %d samples, one frame is %d"
          % (echo.SEARCH_MIN, echo.FRAME))
    sub = echo.EchoCanceller(budget=64, win=8192)
    fr2 = v32ish(8000 * 6, 21)
    nr2 = v32ish(8000 * 6, 22)
    out2 = run(sub, nr2, fr2, [(40, 0.30)])      # a big "echo" at 5 ms
    check("  so an impossible 5 ms reflection is ignored, not chased",
          not sub.locked and max(abs(v) for v in sub.w) == 0.0,
          "locked %s, best rho %.3f" % (sub.locked, sub.best_rho))

    print()
    print("a real echo: 9.6 ms at 19 dB down, both ends talking")
    N = 8000 * 20
    far = v32ish(N, 1)
    near = v32ish(N, 2)
    ec4 = echo.EchoCanceller(budget=64, win=8192)
    got4 = run(ec4, near, far, [(DELAY, GAIN)])
    check("it finds the delay", ec4.locked, "bulk %s, rho %.3f"
          % (ec4.bulk, ec4.best_rho))
    check("  and places the tap window over it",
          ec4.bulk is not None and ec4.bulk <= DELAY < ec4.bulk + ec4.span,
          "taps cover %s..%s, echo at %d"
          % (ec4.bulk, (ec4.bulk or 0) + ec4.span - 1, DELAY))
    e_dt = erle_db(got4, near, far, GAIN, DELAY)
    check("  and cancels it by at least 10 dB under double talk",
          e_dt >= 10.0, "ERLE %.1f dB, with the near end 19 dB above the echo"
          % e_dt)
    check("  the tap it learned matches the echo it was given",
          abs(max(abs(v) for v in ec4.w) - GAIN) < 0.25 * GAIN,
          "|w|max %.4f against %.4f" % (max(abs(v) for v in ec4.w), GAIN))

    print()
    print("with the far end quiet it does much better, as it should")
    ec5 = echo.EchoCanceller(budget=64, win=8192)
    quiet = [0.0] * N
    got5 = run(ec5, quiet, far, [(DELAY, GAIN)])
    e_st = erle_db(got5, quiet, far, GAIN, DELAY)
    check("single talk reaches at least 18 dB", e_st >= 18.0,
          "ERLE %.1f dB" % e_st)
    check("  which is better than under double talk, by the expected margin",
          e_st > e_dt + 4.0, "%.1f dB against %.1f dB" % (e_st, e_dt))

    print()
    print("an echo that only appears once the call is under way")
    ec6 = echo.EchoCanceller(budget=64, win=8192)
    got6 = []
    at = N // 3
    for k in range(0, N - 160, 160):
        inb = [near[j] + (GAIN * far[j - DELAY]
                          if j >= at and j >= DELAY else 0.0)
               for j in range(k, k + 160)]
        got6.extend(ec6.feed(inb))
        ec6.push_tx(far[k:k + 160])
    check("it keeps looking, and locks when the echo turns up", ec6.locked,
          "bulk %s after %d searches" % (ec6.bulk, ec6.searches))

    print()
    print("an echo that goes away again")
    ec7 = echo.EchoCanceller(budget=64, win=8192)
    got7 = []
    until = (2 * N) // 3
    for k in range(0, N - 160, 160):
        inb = [near[j] + (GAIN * far[j - DELAY]
                          if j < until and j >= DELAY else 0.0)
               for j in range(k, k + 160)]
        got7.extend(ec7.feed(inb))
        ec7.push_tx(far[k:k + 160])
    tail = len(got7) // 4
    lo = len(got7) - tail
    resid = [a - b for a, b in zip(got7[-tail:], near[lo:len(got7)])]
    hurt = 10.0 * math.log10(power(resid) / power(near)) if power(resid) else -99
    check("the clean stretch afterwards is not degraded",
          power(resid) < 0.05 * power(near),
          "residual %.1f dB below the near-end signal" % -hurt)
    check("  and the guard noticed, rather than persisting with a stale filter",
          ec7.resets > 0 or max(abs(v) for v in ec7.w) < 0.3 * GAIN,
          "%d resets, |w|max %.4f, ERLE last window %.1f dB"
          % (ec7.resets, max(abs(v) for v in ec7.w), ec7.erle_db))

    print()
    print("robustness")
    check("no NaN or infinity ever reaches the output",
          all(v == v and abs(v) < 1e12 for v in got4))
    ec8 = echo.EchoCanceller(budget=64, win=8192)
    loud = [12.0 * v for v in far]          # far past A-law full scale
    got8 = run(ec8, near, loud, [(DELAY, GAIN)])
    check("  a reference 12x too loud does not blow the filter up",
          all(v == v and abs(v) < 1e12 for v in got8)
          and max(abs(v) for v in ec8.w) < echo.WMAX,
          "|w|max %.4f" % max(abs(v) for v in ec8.w))
    ec9 = echo.EchoCanceller(budget=64, win=8192)
    got9 = run(ec9, [0.0] * 8000 * 4, [0.0] * 8000 * 4, [])
    check("  total silence in both directions is not a division by zero",
          all(v == v for v in got9) and not ec9.locked)
    ecA = echo.EchoCanceller(enabled=False)
    gotA = run(ecA, near, far, [(DELAY, GAIN)])
    check("  disabled, it costs nothing and touches nothing",
          len(gotA) > 0 and not ecA.locked and ecA.searches == 0
          and max(abs(v) for v in ecA.w) == 0.0)

    print()
    if FAIL:
        print("%d FAILURES: %s" % (len(FAIL), "; ".join(FAIL)))
        sys.exit(1)
    print("all echo canceller tests passed")
