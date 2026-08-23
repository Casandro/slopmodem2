"""Sweep every V.32bis rate through a soft-to-soft call and score the data.

The suite checks three rates; this checks all five and prints the numbers that
matter for each -- what was negotiated, the constellation, the eye, and whether
characters cross intact once the link has settled.

Two things about the measurement are deliberate. Characters are fed continuously
rather than as one block, because the interesting question is whether a settled
link is clean, not whether the first second of it is. And only the *tail* is
scored, because circuit 104 is clamped until the eye opens: characters offered
during settling are dropped, which is correct behaviour and not an error.

  python3 v32bis_rates.py
"""
import sys
import v32, v32fsm

PAT = b"V32BIS! "


def call(rates, frames=3000, level=-24.0):
    ans = v32fsm.AnswerStartup(level_dbfs=level, ans_s=0.6, rates=rates,
                               bis=True)
    org = v32fsm.OriginateStartup(level_dbfs=level, rates=rates, bis=True)
    to_a = to_o = [0] * 160
    ga, go = bytearray(), bytearray()
    i = 0
    entered = None
    for k in range(frames):
        oa = ans.step(to_a)
        oo = org.step(to_o)
        to_a, to_o = oo, oa
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


def eye(m):
    P = m.rx.mode.points
    d = sorted(min(abs(v - p) for p in P) for v in m.rx.data_syms[-3000:])
    mind = min(abs(P[a] - P[b]) for a in range(len(P))
               for b in range(a + 1, len(P)))
    return (d[len(d) // 2], d[int(0.9 * len(d))], mind / 2)


def main():
    print("V.32bis: every rate, soft to soft, characters both ways")
    print()
    print(" rate    mode     bits  data at  eye median  p90    radius  "
          "a->c  c->a  tail")
    bad = 0
    for rates in ((4800,), (4800, 7200), (4800, 7200, 9600),
                  (4800, 7200, 9600, 12000),
                  (4800, 7200, 9600, 12000, 14400)):
        an, og, ga, go, at = call(rates)
        med, p90, rad = eye(an)
        ka, kb = tail(go), tail(ga)
        ok = ka >= 380 and kb >= 380 and med < rad / 2
        bad += not ok
        print(" %-6s  %-8s %4d  %5.2f s  %9.3f  %5.3f  %6.3f  %5d %5d  %s"
              % (an.rate, an._data_mode().name, an._data_bps(), at or -1,
                 med, p90, rad, len(go), len(ga),
                 "%d/%d ok" % (ka, kb) if ok else "%d/%d FAIL" % (ka, kb)))
    print()
    print("  'a->c' and 'c->a' are total characters recovered; 'tail' is how many"
          " of the\n  last 400 bytes are the exact repeating pattern.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
