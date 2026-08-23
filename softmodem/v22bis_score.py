"""Score a captured V.22bis transmission against the expected repeating payload.

Each segment is decoded independently, so each one pays a blind-equaliser
acquisition transient (CMA + DD-LMS start from a centre spike every time). That
transient is an artefact of decoding a capture in slices, not of the link, so
both figures are reported: the whole segment, and the segment after the first
`SKIP` characters.
"""
import sys
import v22bis_rx

SKIP = 80


def score(text, expect):
    if not text:
        return 0.0, 0
    best = (0, 0)
    for ph in range(len(expect)):
        ok = sum(1 for i, c in enumerate(text) if c == expect[(i + ph) % len(expect)])
        if ok > best[0]:
            best = (ok, ph)
    return best[0] / float(len(text)), best[1]


if __name__ == "__main__":
    path, expect = sys.argv[1], sys.argv[2]
    spans = [(float(a), float(b)) for a, b in
             (s.split("-") for s in sys.argv[3].split(","))]
    tot = totok = tots = totsok = 0
    for a, b in spans:
        text = v22bis_rx.run(path, a, b)
        f, ph = score(text, expect)
        tail = text[SKIP:]
        ft, _ = score(tail, expect[ph % len(expect):] + expect[:ph % len(expect)]) if tail else (0.0, 0)
        ft, _ = score(tail, expect)
        print("     score: %.3f%% of %d chars; after %d-char acquisition %.3f%% of %d"
              % (100 * f, len(text), SKIP, 100 * ft, len(tail)))
        tot += len(text); totok += round(f * len(text))
        tots += len(tail); totsok += round(ft * len(tail))
    print()
    print("TOTAL  %d/%d = %.3f%% correct   |   post-acquisition %d/%d = %.3f%%"
          % (totok, tot, 100.0 * totok / max(tot, 1),
             totsok, tots, 100.0 * totsok / max(tots, 1)))
