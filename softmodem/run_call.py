"""Originate one V.22bis call.

A shim. `modem.py` is the modem now -- it answers and it originates, and this
program is only the convenience of pinning it to one outbound call, kept so the
commands recorded in testrig/v22-modem.md still run. The whole of the originating
path lives in modem.py and fsm.OriginateV22bis.

  python3 run_call.py '**1' --seconds 60 --payload 'SOFT2MODEM '
  python3 run_call.py --dte --seconds 80
"""
import argparse, sys
import modem


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("number", nargs="?", default=None)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--level", type=float, default=-18.0)
    ap.add_argument("--idle", type=int, default=2)
    ap.add_argument("--payload", default="SOFT2MODEM ")
    ap.add_argument("--expect", default=None)
    ap.add_argument("--dte", action="store_true")
    ap.add_argument("--dte-wait", type=float, default=40.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--rx-out", default=None)
    ap.add_argument("--usb1-timeout", type=float, default=25.0)
    a = ap.parse_args()

    argv = ["--calls", "1", "--max-call", "%g" % a.seconds,
            "--level", "%g" % a.level, "--idle", "%d" % a.idle,
            "--payload", a.payload, "--usb1-timeout", "%g" % a.usb1_timeout,
            "--idle-seconds", "%g" % a.dte_wait]
    if a.number:
        argv += ["--dial", a.number]
    if a.dte:
        argv += ["--dte"]
    for opt, val in (("--expect", a.expect), ("--out", a.out),
                     ("--rx-out", a.rx_out)):
        if val:
            argv += [opt, val]
    if not a.number and not a.dte:
        sys.exit("no number given (and no --dte to get one from)")
    return modem.main(argv)


if __name__ == "__main__":
    sys.exit(main())
