"""Every rate, over a line whose impairments are known exactly.

The rig cannot answer the 14 400 question any more: its best instrument floors at
7.1% of the rms radius on a pure tone, and the margin being argued about is 11.0%.
So this asks the question from the other end -- put a channel we designed between
two soft modems and find how much impairment each rate survives. If 14 400 falls
over on a line that 12 000 walks through, the difference is ours and it is here,
not out on the ATA.

What is scored, and why each is scored the way it is:

  reached    seconds from the start of the call to both ends in DATA, or "-" if
             the handshake never completed. A rate that cannot finish 5.4 at all
             is a different failure from one that finishes and then collapses.
  eye        median distance from each received symbol to its nearest
             constellation point, as a percentage of the rms radius. The decision
             margin is the number to beat: 70.7% at 4800 down to 11.0% at 14 400.
  match      the longest run of correct characters in the tail, which is the only
             part of the call where circuit 104 is unclamped.
  retrains   how many times either end asked for one. Zero is the healthy answer;
             the rig gives us one every 3.6 s at 14 400.

  python3 chansweep.py                      # every rate over every preset
  python3 chansweep.py --channel typical    # one line
  python3 chansweep.py --rate 14400 --sweep delay_ms=0,0.5,1,1.5,2
"""
import argparse
import sys

import channel
import v32fsm

PAT = b"V32BIS! "


class Echo:
    """Our own transmission, leaking back into our own receiver.

    The one thing the channel pair does not model. V.32 is full duplex in a
    single band on a single pair, so every real installation puts some of what a
    modem sends back into what it hears, and cancelling it is the receiver's
    problem. Two paths, because a real hybrid has two: a near echo a few ms out,
    and a talker echo at the round trip. The rig measured 43.5 dB of return loss,
    so the default is deliberately harsher than that -- the point is to find
    where our receiver breaks, not to reproduce one line.

    This is a coupling between the two directions rather than a property of
    either, which is why it lives here and not in channel.py.
    """

    def __init__(self, near_db=-30.0, near_ms=4.0,
                 far_db=-45.0, far_ms=40.0):
        self.g1 = 10.0 ** (near_db / 20.0)
        self.g2 = 10.0 ** (far_db / 20.0)
        self.d1 = int(near_ms * 8.0)
        self.d2 = int(far_ms * 8.0)
        self.buf = [0.0] * (self.d2 + 8000)

    def step(self, mine, heard):
        """heard + whatever of `mine` the hybrid sends back."""
        self.buf.extend(float(v) for v in mine)
        n = len(self.buf)
        out = []
        base = n - len(mine)
        for i, v in enumerate(heard):
            k = base + i
            e = 0.0
            if k - self.d1 >= 0:
                e += self.g1 * self.buf[k - self.d1]
            if k - self.d2 >= 0:
                e += self.g2 * self.buf[k - self.d2]
            out.append(int(round(v + e)))
        if len(self.buf) > self.d2 + 16000:
            del self.buf[:len(self.buf) - (self.d2 + 8000)]
        return out


def call(rate, ch_a, ch_o, frames=1500, level=-24.0, echo=None,
         cancel=False):
    """One soft-to-soft call at one rate, each direction through its own channel.

    Two channels, not one: a line impairs both directions, and giving each its
    own instance keeps their drift and noise independent, as two real clocks are.
    """
    rates = (rate,)
    ans = v32fsm.AnswerStartup(level_dbfs=level, ans_s=0.6, rates=rates,
                               bis=True, cancel_echo=cancel)
    org = v32fsm.OriginateStartup(level_dbfs=level, rates=rates, bis=True,
                                  cancel_echo=cancel)
    to_a = to_o = [0] * 160
    ga, go = bytearray(), bytearray()
    i = 0
    entered = None
    for k in range(frames):
        oa = ans.step(to_a)
        oo = org.step(to_o)
        # answerer's output crosses to the originator and vice versa
        to_o = ch_o.step(oa)
        to_a = ch_a.step(oo)
        if echo is not None:
            ea, eo = echo
            to_a = ea.step(oa, to_a)      # the answerer hears its own transmit
            to_o = eo.step(oo, to_o)
        if ans.state == v32fsm.DATA and org.state == v32fsm.DATA:
            if entered is None:
                entered = k * 0.02
            ans.put((PAT + PAT)[i % 8:i % 8 + 4])
            org.put((PAT + PAT)[i % 8:i % 8 + 4])
            i += 4
            ga.extend(ans.received())
            go.extend(org.received())
    return ans, org, bytes(ga), bytes(go), entered


def tail(got, n=400):
    t = got[-n:]
    if len(t) < 100:
        return 0
    i = t.find(PAT)
    if i < 0:
        return 0
    exp = PAT * (len(t) // 8 + 2)
    k = 0
    while i + k < len(t) and t[i + k] == exp[k]:
        k += 1
    return k


def eye(m, n=3000):
    """Median symbol error as a percentage of the rms radius.

    Median, not mean: a handful of symbols taken during a retrain would drag a
    mean anywhere, and the question is what the typical symbol looks like.
    """
    syms = getattr(m.rx, "data_syms", None)
    if not syms:
        return None
    P = m.rx.mode.points
    if not P:
        return None
    v = syms[-n:]
    d = sorted(min(abs(s - p) for p in P) for s in v)
    if not d:
        return None
    rms = (sum(abs(p) ** 2 for p in P) / len(P)) ** 0.5
    return 100.0 * d[len(d) // 2] / rms


MARGIN = {4800: 70.7, 7200: 31.6, 9600: 22.4, 12000: 15.4, 14400: 11.0}


def run(rate, name, over, frames, level, echo_db=None, cancel=False):
    ch_a = channel.make(name, **over)
    ch_o = channel.make(name, seed=2, **over)
    ec = None
    if echo_db is not None:
        ec = (Echo(near_db=echo_db), Echo(near_db=echo_db))
    ans, org, ga, go, entered = call(rate, ch_a, ch_o, frames, level, ec,
                                    cancel)
    rt = getattr(ans, "retrains", 0) + getattr(org, "retrains", 0)
    return dict(rate=rate, entered=entered,
                eye_a=eye(ans), eye_o=eye(org),
                match=min(tail(ga), tail(go)),
                got=min(len(ga), len(go)), retrains=rt)


def line(r):
    e = "%5.2f s" % r["entered"] if r["entered"] is not None else "    -  "
    ea = "%5.1f%%" % r["eye_a"] if r["eye_a"] is not None else "    - "
    eo = "%5.1f%%" % r["eye_o"] if r["eye_o"] is not None else "    - "
    m = MARGIN[r["rate"]]
    ok = (r["eye_a"] is not None and r["eye_a"] < m and
          r["eye_o"] is not None and r["eye_o"] < m and r["match"] >= 200)
    return ("  %5d  %s  %s %s  (margin %4.1f%%)  %6d B  %5d ok  %2d rt   %s"
            % (r["rate"], e, ea, eo, m, r["got"], r["match"], r["retrains"],
               "PASS" if ok else "fail"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=None, help="one preset, else all")
    ap.add_argument("--rate", type=int, default=None, help="one rate, else all")
    ap.add_argument("--frames", type=int, default=1500, help="20 ms each")
    ap.add_argument("--level", type=float, default=-24.0)
    ap.add_argument("--echo", type=float, default=None,
                    help="near-echo return loss in dB, e.g. -30. The rig "
                         "measured 43.5 dB of return loss; the channel pair "
                         "models no echo at all, which is the one thing a real "
                         "full-duplex pair always has")
    ap.add_argument("--cancel", action="store_true",
                    help="run the echo canceller, which --echo otherwise leaves "
                         "with nothing to do and no way to show itself")
    ap.add_argument("--sweep", default=None,
                    help="vary one parameter, e.g. delay_ms=0,0.5,1,2")
    a = ap.parse_args()

    rates = [a.rate] if a.rate else [4800, 7200, 9600, 12000, 14400]

    if a.sweep:
        key, vals = a.sweep.split("=", 1)
        base = a.channel or "perfect"
        print("sweeping %s over the %r channel\n" % (key, base))
        for v in [float(x) for x in vals.split(",")]:
            print("%s = %g" % (key, v))
            for rate in rates:
                print(line(run(rate, base, {key: v}, a.frames, a.level, a.echo, a.cancel)))
            print()
        return 0

    names = [a.channel] if a.channel else ["perfect", "drift", "delay", "tilt",
                                           "noise", "mild", "typical", "poor"]
    for name in names:
        ch = channel.make(name)
        print("=" * 72)
        print("channel %-11s %s" % (name, channel.describe(name)))
        band = ch.realised((600, 1800, 3000))
        print("  realised: " + "   ".join("%d Hz %+.1f dB / %+.2f ms" % t
                                          for t in band))
        print("   rate   reached    eye (ans / org)                data   match")
        for rate in rates:
            print(line(run(rate, name, {}, a.frames, a.level, a.echo, a.cancel)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
