"""Reuse the working SIP stack from testrig/tools rather than re-deriving it.

The CSeq-matched digest handling (the FRITZ!Box retransmits 401, so an unmatched
read returns the stale challenge) and the authenticated-BYE fix were established
by measurement; duplicating them here would risk regressing both.
"""
import os, sys

_TOOLS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      os.pardir, "testrig", "tools"))
# Appended, not inserted. Both directories contain an `ansam.py`, and prepending
# made the tools copy win -- which nothing here wants, since the tools-only
# modules sip_glue actually needs (sipmin, sipcfg, answer) have no namesakes.
#
# Prepending was a live trap rather than a theoretical one. It only bit when a
# program imported sip_glue *before* anything that pulls in ansam: run_answer.py
# imports ansam at the top and so cached the right module, but modem.py imports
# in the other order, and fsm.AnswerV22bis's lazy `import ansam` then resolved to
# the tools copy and died on a missing ans_samples -- one call into a two-call
# session, with the traceback landing in a filtered log.
if _TOOLS not in sys.path:
    sys.path.append(_TOOLS)

import sipmin          # noqa: E402
import sipcfg          # noqa: E402
from answer import raw_recv, resp_for   # noqa: E402

HOST, USER, PW = sipcfg.load()

__all__ = ["sipmin", "sipcfg", "raw_recv", "resp_for", "HOST", "USER", "PW", "TOOLS_DIR"]
TOOLS_DIR = _TOOLS
