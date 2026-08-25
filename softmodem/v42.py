"""V.42: HDLC framing and the detection phase.

Both hardware modems default to V.42 and have to be told not to, with a different
command set each -- see testrig/modem.md. This is the start of not needing to.

Two layers here, both testable without a line:

  * 8.1.1's frame structure: flags, bit stuffing, and the 16- and 32-bit frame
    check sequences. Ordinary HDLC, with one useful gift: 8.1.1.6.1 prints the
    receiver's residue, "0001 1101 0000 1111", which is a conformance value
    rather than a thing to be derived.
  * 7.2.1's detection phase: the patterns each end sends to find out whether the
    other speaks V.42 at all. These are *asynchronous characters* on a
    synchronous link -- start bit, seven data bits, parity, stop bit -- which is
    why they can be recognised by a DCE that has no idea what a frame is.

LAPM itself (8.2 onwards) is not here yet.
"""
import collections

FLAG = 0x7E
T400 = 0.750                    # 9.1.1: detection phase timer, seconds


# ---------------------------------------------------------------------------
# 8.1.1.6 Frame check sequences
# ---------------------------------------------------------------------------
#
# 16-bit: x^16 + x^12 + x^5 + 1, register preset to all ones, the ones
# complement of the remainder transmitted. The receiver's residue in the absence
# of errors is printed in the Recommendation and is asserted in the tests.
FCS16_POLY = 0x8408             # x^16+x^12+x^5+1, bit-reversed
FCS16_INIT = 0xFFFF
# 8.1.1.6.1 prints the residue as "0001 1101 0000 1111 (x15 through x0)", which
# is 0x1D0F written most-significant-power first. A table-driven CRC holds the
# register reflected, so the same number appears there as 0xF0B8. They are one
# value in two bit orders, and the test asserts the relationship rather than
# picking whichever constant makes the code pass.
FCS16_SPEC_GOOD = 0x1D0F        # as printed, x15 through x0
FCS16_GOOD = 0xF0B8             # the same value in the reflected register
FCS32_POLY = 0xEDB88320         # x^32+...+1, bit-reversed
FCS32_INIT = 0xFFFFFFFF
FCS32_SPEC_GOOD = 0xC704DD7B    # as printed, x31 through x0
FCS32_GOOD = 0xDEBB20E3         # reflected

_T16 = []
for _b in range(256):
    _c = _b
    for _ in range(8):
        _c = (_c >> 1) ^ (FCS16_POLY if _c & 1 else 0)
    _T16.append(_c)
_T32 = []
for _b in range(256):
    _c = _b
    for _ in range(8):
        _c = (_c >> 1) ^ (FCS32_POLY if _c & 1 else 0)
    _T32.append(_c)


def bitrev(v, width):
    """Reverse `width` bits, for moving between the two conventions above."""
    out = 0
    for k in range(width):
        if v & (1 << k):
            out |= 1 << (width - 1 - k)
    return out


def fcs16(data, init=FCS16_INIT):
    """The register value over `data`; the FCS transmitted is its complement."""
    c = init
    for b in data:
        c = (c >> 8) ^ _T16[(c ^ b) & 0xFF]
    return c


def fcs32(data, init=FCS32_INIT):
    c = init
    for b in data:
        c = (c >> 8) ^ _T32[(c ^ b) & 0xFF]
    return c


def with_fcs(data, wide=False):
    """Append the frame check sequence, low octet first."""
    if wide:
        v = fcs32(data) ^ 0xFFFFFFFF
        return bytes(data) + bytes((v & 0xFF, (v >> 8) & 0xFF,
                                    (v >> 16) & 0xFF, (v >> 24) & 0xFF))
    v = fcs16(data) ^ 0xFFFF
    return bytes(data) + bytes((v & 0xFF, (v >> 8) & 0xFF))


def fcs_ok(frame, wide=False):
    """True if the residue matches the value 8.1.1.6 prints."""
    if wide:
        return len(frame) > 4 and fcs32(frame) == FCS32_GOOD
    return len(frame) > 2 and fcs16(frame) == FCS16_GOOD


# ---------------------------------------------------------------------------
# 8.1.1.2 Flags and transparency
# ---------------------------------------------------------------------------

def bits_of(data):
    """Octets to bits, low-order bit first -- the order 8.1.2 transmits in."""
    out = []
    for b in data:
        for k in range(8):
            out.append((b >> k) & 1)
    return out


def bytes_of(bits):
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        v = 0
        for k in range(8):
            if bits[i + k]:
                v |= 1 << k
        out.append(v)
    return bytes(out)


def stuff(bits):
    """Insert a 0 after every five contiguous 1s (8.1.1.2)."""
    out = []
    run = 0
    for b in bits:
        out.append(b)
        run = run + 1 if b else 0
        if run == 5:
            out.append(0)
            run = 0
    return out


def unstuff(bits):
    """Discard a 0 that directly follows five contiguous 1s."""
    out = []
    run = 0
    for b in bits:
        if run == 5:
            run = 0
            if b == 0:
                continue        # inserted for transparency
        out.append(b)
        run = run + 1 if b else 0
    return out


FLAG_BITS = bits_of(bytes((FLAG,)))


def frame(address, control, info=b"", wide=False):
    """One delimited, stuffed frame as a bit list.

    `control` may be one or two octets: 8.2.2 makes it two for the frame types
    that carry sequence numbers and one for those that do not.
    """
    ctl = bytes((control,)) if isinstance(control, int) else bytes(control)
    addr = bytes((address,)) if isinstance(address, int) else bytes(address)
    body = with_fcs(addr + ctl + bytes(info), wide)
    return FLAG_BITS + stuff(bits_of(body)) + FLAG_BITS


class Deframer:
    """Bits in, (address, control, information) out, per 8.1.1.

    Hunts for the flag, unstuffs, checks the FCS, and drops anything that fails
    -- 8.1.3 calls those invalid frames and says to discard them without further
    action. Counts what it discarded and why, because "the far end is silent" and
    "the far end is sending frames we are throwing away" look identical otherwise.
    """

    def __init__(self, wide=False, addr_len=1, max_octets=1024):
        self.wide = wide
        self.addr_len = addr_len
        self.max_octets = max_octets
        self.buf = []
        self.insync = False
        self.frames = 0
        self.short = 0          # 8.1.3: fewer octets than a frame can have
        self.badfcs = 0
        self.aborted = 0        # 8.1.4: seven or more contiguous ones
        self.oversize = 0

    def feed(self, bits):
        out = []
        for b in bits:
            self.buf.append(b)
            if len(self.buf) >= 8 and self.buf[-8:] == FLAG_BITS:
                body = self.buf[:-8]
                self.buf = []
                if not self.insync:
                    self.insync = True          # that was an opening flag
                    continue
                self._take(body, out)
                self.insync = True              # a closing flag opens the next
            elif len(self.buf) > 8 * (self.max_octets + 8):
                self.oversize += 1
                self.buf = []
                self.insync = False
        return out

    def _take(self, body, out):
        if not body:
            return                              # flags back to back
        if body[-7:] == [1] * 7:
            self.aborted += 1
            return
        oct_ = bytes_of(unstuff(body))
        need = self.addr_len + 1 + (4 if self.wide else 2)
        if len(oct_) < need:
            self.short += 1
            return
        if not fcs_ok(oct_, self.wide):
            self.badfcs += 1
            return
        n = 4 if self.wide else 2
        body_ = oct_[:-n]
        self.frames += 1
        out.append((body_[:self.addr_len],
                    body_[self.addr_len:self.addr_len + 1], body_[self.addr_len + 1:]))


# ---------------------------------------------------------------------------
# 7.2.1 Detection phase
# ---------------------------------------------------------------------------
#
# The patterns are transcribed as the eight bits the Recommendation prints
# between the start and stop bits -- seven data bits, low order first, then the
# parity bit -- rather than reconstructed from a character and a parity rule.
# Reconstructing them would need the parity convention, and the printed patterns
# do not use one convention: DC1 appears with even parity and then odd, while
# 'E', 'C' and NUL are all printed with a parity bit of 0, which is odd for 'E'
# and 'C' and even for NUL. The literal bits are what goes on the line.
DC1_EVEN = (1, 0, 0, 0, 1, 0, 0, 0)     # 7.2.1.2  "1000 1000"
DC1_ODD = (1, 0, 0, 0, 1, 0, 0, 1)      # 7.2.1.2  "1000 1001"
CHAR_E = (1, 0, 1, 0, 0, 0, 1, 0)       # Table 3  "1010 0010"
CHAR_C = (1, 1, 0, 0, 0, 0, 1, 0)       # Table 3  "1100 0010"
CHAR_NULL = (0, 0, 0, 0, 0, 0, 0, 0)    # Table 3  "0000 0000"


def char10(field):
    """One asynchronous character: start bit, the eight printed bits, stop bit."""
    return [0] + list(field) + [1]


def pattern(first, second, fill=8):
    """Two characters separated and followed by 8 to 16 ones (7.2.1.2)."""
    if not 8 <= fill <= 16:
        raise ValueError("7.2.1.2 allows 8 to 16 ones between characters")
    return (char10(first) + [1] * fill + char10(second) + [1] * fill)


def odp(fill=8):
    """The Originator Detection Pattern: DC1 even, then DC1 odd."""
    return pattern(DC1_EVEN, DC1_ODD, fill)


def adp(supported=True, fill=8):
    """An Answerer Detection Pattern: E and C for V.42, E and NUL for none."""
    return pattern(CHAR_E, CHAR_C if supported else CHAR_NULL, fill)


class PatternDetector:
    """Find repeated detection-phase characters in a bit stream.

    Deliberately looks for the *characters*, not the whole pattern, because the
    number of ones between them is only bounded (8 to 16) and the far end may
    start mid-pattern. 7.2.1.2 asks the originator for "the characters from at
    least two adjacent ADPs" and 7.2.1.3 asks the answerer for "at least four
    DC1s of alternating parity", so what each end needs is a count of characters,
    which is what this reports.
    """

    def __init__(self, wanted, need):
        self.wanted = [tuple(w) for w in wanted]
        self.need = need
        self.win = collections.deque(maxlen=10)
        self.seen = []          # indices into `wanted`, in order
        self.hits = 0

    def feed(self, bits):
        """Returns True once the requirement is met -- and the requirement is
        *alternation*, not a count.

        Both clauses ask for alternating characters, and the difference matters:
        four repetitions of the "no error-correcting protocol desired" ADP
        contain four (E) characters, so a detector that merely counted the
        characters it recognised would report V.42 support from a pattern that
        says the opposite. Returning the alternation test is the only answer
        this can give.
        """
        for b in bits:
            self.win.append(b)
            if len(self.win) < 10 or self.win[0] != 0 or self.win[9] != 1:
                continue
            field = tuple(self.win)[1:9]
            for i, w in enumerate(self.wanted):
                if field == w:
                    self.seen.append(i)
                    self.hits += 1
                    self.win.clear()
                    break
        return self.alternating(self.need)

    def alternating(self, need):
        """True once `need` characters have arrived alternating between the two
        wanted fields -- 7.2.1.3's "alternating parity"."""
        run = 1
        for a, b in zip(self.seen, self.seen[1:]):
            run = run + 1 if a != b else 1
            if run >= need:
                return True
        return False


class Detection:
    """7.2.1's detection phase, either role.

    Fed the received bit stream a chunk at a time; returns what to transmit and,
    once decided, what the far end is. `elapsed` is passed in rather than read
    from a clock so the phase can be replayed and tested.

      originator  sends ODP until the ADP is seen, or T400 expires
      answerer    sends mark until the ODP is seen, then the ADP ten times
    """

    UNDECIDED, LAPM, NONE = "undecided", "lapm", "none"
    # 7.2.1.3's second exit: "the start of the protocol phase is indicated by
    # receipt of continuous flags, or of an LAPM or alternative procedure
    # protocol frame". A real modem takes this route -- both of the ones here
    # send XID as their first frame with no ODP at all -- and an answerer that
    # only watches for the ODP concludes there is nothing out there while the
    # far end is retransmitting XID at it.
    FLAG_RUN = 4

    def __init__(self, originator, supported=True, fill=8, adp_reps=10):
        self.originator = originator
        self.supported = supported
        self.fill = fill
        self.adp_reps = adp_reps
        self.result = self.UNDECIDED
        self.saw_odp = False
        self.saw_flags = False      # 7.2.1.3's other exit, see FLAG_RUN
        self.flag_run = 0
        self.sent_adp = 0
        self.win = collections.deque(maxlen=8)
        # 7.2.1.2: "the characters from at least two adjacent ADPs", which is
        # four characters. 7.2.1.3: "at least four DC1s of alternating parity".
        if originator:
            self.det = PatternDetector([CHAR_E, CHAR_C], 4)
            self.none = PatternDetector([CHAR_E, CHAR_NULL], 4)
        else:
            self.det = PatternDetector([DC1_EVEN, DC1_ODD], 4)
            self.none = None

    def feed(self, bits, elapsed):
        """Received bits in; bits to transmit out."""
        if self.originator:
            ec = self.det.feed(bits)
            no = self.none.feed(bits) if self.none else False
            if self.result is self.UNDECIDED:
                if ec:
                    self.result = self.LAPM
                elif no:
                    self.result = self.NONE
                elif elapsed >= T400:
                    self.result = self.NONE     # 7.2.1.2: no ADP within T400
            if self.result is self.UNDECIDED:
                return odp(self.fill)
            return []
        # answerer
        if not self.saw_odp:
            if self._flags(bits):
                # The originator has skipped the detection phase and is already
                # in the protocol establishment phase. Nothing is owed to it --
                # no ADP, since it is not listening for one -- so go straight to
                # LAPM and let it drive.
                self.saw_flags = True
                self.result = self.LAPM
                return []
            if self.det.feed(bits):
                self.saw_odp = True
            elif elapsed >= T400:
                self.result = self.NONE         # 7.2.1.3
                return []
            else:
                return [1] * len(bits)          # mark until the ODP arrives
        if self.sent_adp < self.adp_reps:
            self.sent_adp += 1
            return adp(self.supported, self.fill)
        self.result = self.LAPM if self.supported else self.NONE
        return []

    def _flags(self, bits):
        """FLAG_RUN consecutive flags, on any bit alignment.

        Counting whole flags rather than looking for one is deliberate: 0x7E
        occurs by chance in scrambled data about once every 256 bits, and the ODP
        itself is mostly ones, so a single flag decides nothing. Consecutive
        flags cannot happen by accident and are exactly what the clause names.
        """
        for b in bits:
            self.win.append(b)
            if len(self.win) == 8 and list(self.win) == list(FLAG_BITS):
                self.flag_run += 1
                self.win.clear()
                if self.flag_run >= self.FLAG_RUN:
                    return True
            elif len(self.win) == 8:
                # not a flag on this alignment: slide by one and start over
                self.win.popleft()
                self.flag_run = 0
        return False


# ---------------------------------------------------------------------------
# 8.2 LAPM: address, control, frame types
# ---------------------------------------------------------------------------
#
# Sequence numbers are modulo 128 (Table 7), so the control field is two octets
# for the formats that carry them and one for those that do not. All the
# encodings below are Table 8's, and they are the familiar LAPB ones.
#
# Bit 1 is the first bit transmitted, so in an octet it is the least significant.

MOD = 128
DLCI_DATA = 0                   # Table 10: DTE-to-DTE data

# S format: octet 3 is the type, octet 4 is N(R) in bits 8-2 and P/F in bit 1
S_RR, S_RNR, S_REJ, S_SREJ = 0x01, 0x05, 0x09, 0x0D
# U format: one octet, P/F in bit 5
U_SABME, U_DM, U_UI = 0x6F, 0x0F, 0x03
U_DISC, U_UA, U_FRMR = 0x43, 0x63, 0x87
U_XID, U_TEST = 0xAF, 0xE3
PF = 0x10

N401 = 128                      # 9.2.3: default information field octets
WINDOW = 15                     # 9.2.4: default k
T401 = 1.0                      # 9.2.1: no default given; see Appendix IV
N400 = 5                        # 9.2.2: no default given, minimum 1
FLAGS_BEFORE_SABME = 16         # 8.3.2.1 Note 2


def address(originator, command, dlci=DLCI_DATA):
    """Table 6: the C/R bit depends on the role *and* on command vs response.

    A command from the originator and a response from the answerer both carry
    C/R = 1; the other two carry 0. Getting this backwards is invisible in a
    loopback where both ends make the same mistake, so it is tested against the
    table directly.
    """
    cr = 1 if (originator == command) else 0
    return ((dlci & 0x3F) << 2) | (cr << 1) | 1        # EA = 1: last octet


def i_control(ns, nr, p=0):
    return bytes((((ns % MOD) << 1) & 0xFE, (((nr % MOD) << 1) | p) & 0xFF))


def s_control(kind, nr, pf=0):
    return bytes((kind, (((nr % MOD) << 1) | pf) & 0xFF))


def u_control(kind, pf=0):
    return bytes((kind | (PF if pf else 0),))


class Frame:
    """A decoded LAPM frame."""

    def __init__(self, kind, ns=None, nr=None, pf=0, info=b"", cr=None):
        self.kind = kind
        self.ns = ns
        self.nr = nr
        self.pf = pf
        self.info = info
        self.cr = cr

    def __repr__(self):
        bits_ = [self.kind]
        if self.ns is not None:
            bits_.append("N(S)=%d" % self.ns)
        if self.nr is not None:
            bits_.append("N(R)=%d" % self.nr)
        if self.pf:
            bits_.append("P/F")
        if self.info:
            bits_.append("%d octets" % len(self.info))
        return "<%s>" % " ".join(bits_)


_S_NAME = {S_RR: "RR", S_RNR: "RNR", S_REJ: "REJ", S_SREJ: "SREJ"}
_U_NAME = {U_SABME: "SABME", U_DM: "DM", U_UI: "UI", U_DISC: "DISC",
           U_UA: "UA", U_FRMR: "FRMR", U_XID: "XID", U_TEST: "TEST"}


def parse(addr, ctl_and_info):
    """Decode address plus control plus information into a Frame, or None.

    8.2.4.1 classifies anything not in Table 8 as an undefined control field, and
    8.5.5 says what to do about it; returning None is how that is reported here.
    """
    if not ctl_and_info:
        return None
    cr = (addr[0] >> 1) & 1
    c0 = ctl_and_info[0]
    if not c0 & 1:                                     # I format
        if len(ctl_and_info) < 2:
            return None
        return Frame("I", ns=c0 >> 1, nr=ctl_and_info[1] >> 1,
                     pf=ctl_and_info[1] & 1, info=bytes(ctl_and_info[2:]), cr=cr)
    if (c0 & 3) == 1:                                  # S format
        if len(ctl_and_info) < 2 or c0 not in _S_NAME:
            return None
        return Frame(_S_NAME[c0], nr=ctl_and_info[1] >> 1,
                     pf=ctl_and_info[1] & 1, info=bytes(ctl_and_info[2:]), cr=cr)
    base = c0 & ~PF                                    # U format
    if base not in _U_NAME:
        return None
    return Frame(_U_NAME[base], pf=1 if c0 & PF else 0,
                 info=bytes(ctl_and_info[1:]), cr=cr)


class Lapm:
    """8.3 to 8.5: establishment, data transfer, release.

    Works in frames, not bits, so it can be driven and tested without a
    modulator. `poll` returns frames to transmit as (address, control+info)
    octet pairs; `feed` takes one received the same way. `Link` below puts the
    framing round it.

    What is implemented: SABME/UA establishment with T401 and N400 retries,
    I-frame transfer with V(S)/V(A)/V(R) modulo 128, the window k, segmentation
    at N401, acknowledgement by RR, REJ on a sequence error, retransmission from
    V(A), and DISC/UA release.

    What is not: 8.5.3's timer-recovery state proper. On T401 expiry this
    retransmits from V(A) and counts the attempt, which recovers the same losses
    but does not use the P/F exchange to resynchronise first. SREJ, RNR flow
    control, FRMR reporting and XID negotiation are also absent -- the defaults
    of 9.2.3 and 9.2.4 are used, which is what XID's absence means anyway.
    """

    DISCONNECTED, SETUP, CONNECTED, RELEASING, FAILED = (
        "disconnected", "setup", "connected", "releasing", "failed")

    def __init__(self, originator, k=WINDOW, n401=N401, t401=T401, n400=N400):
        self.originator = originator
        self.k = k
        self.n401 = n401
        self.t401 = t401
        self.n400 = n400
        self.state = self.DISCONNECTED
        self.vs = self.vr = self.va = 0
        self.outq = collections.deque()      # user data awaiting an I frame
        self.sent = {}                       # N(S) -> info, awaiting ack
        self.inq = bytearray()               # delivered user data
        self.timer = None                    # T401 deadline, or None
        self.retries = 0
        self.rejected = False                # 8.4.5 reject exception condition
        self.recovery = False                # 8.4.8 timer-recovery condition
        self.ack_due = False
        self.stats = collections.Counter()
        # 12.2: what XID settled on. Defaults until something says otherwise,
        # which is exactly 9.2.3's "absence of a value indicates use of the
        # default".
        self.xid = XidParams(n401, n401, k, k)
        self.opts = ()          # optional procedures we are willing to agree to
        self.xids = 0
        self.xid_due = False
        # Diagnostic only, and not conformant: repeat the XID response. An HDLC
        # frame dies on a single bit error, so "the far end ignores our response"
        # and "our response never arrives" look identical from here. Repetition
        # separates them.
        self.xid_reps = 1
        # Diagnostic: cycle response variants, one per received XID command, so
        # a single call can test several hypotheses about why a far end is
        # rejecting ours. Each entry is (label, C/R override or None, builder).
        self.xid_variants = None
        self.xid_tried = []
        # Whether the response carries PI 3 at all. See xid_response.
        self.xid_opt_pi = True

    # -- helpers --------------------------------------------------------

    def _cmd(self, ctl, info=b""):
        self.stats[ctl[:1].hex()] += 1
        return (bytes((address(self.originator, True),)), bytes(ctl) + info)

    def _rsp(self, ctl, info=b"", cr=None):
        self.stats[ctl[:1].hex()] += 1
        addr = address(self.originator, False)
        if cr is not None:      # diagnostic override; see xid_variants
            addr = (addr & ~0x02) | (cr << 1)
        return (bytes((addr,)), bytes(ctl) + info)

    def _win_open(self):
        return ((self.vs - self.va) % MOD) < self.k

    # -- interface ------------------------------------------------------

    def connect(self, now=0.0):
        """8.3.2.1: SABME, P always 1 so a DM cannot be misread."""
        self.state = self.SETUP
        self.retries = 0
        self.timer = now + self.t401
        return [self._cmd(u_control(U_SABME, 1))]

    def release(self, now=0.0):
        self.state = self.RELEASING
        self.retries = 0
        self.timer = now + self.t401
        return [self._cmd(u_control(U_DISC, 1))]

    def negotiate(self, now=0.0):
        """8.10.2: an XID command, T401 started and N400 reset. Optional for us
        to send -- 7.2.2 permits skipping negotiation when the defaults suit --
        but not optional to answer, and both modems here open with one."""
        self.timer = now + self.t401
        self.retries = 0
        self.xid_due = True
        return [self._cmd(u_control(U_XID, 0), self.xid.command())]

    def _got_xid(self, f, now):
        """8.10.2 both ways round. The P/F bit of an XID frame is 0 (8.2.4.13),
        so command and response are told apart only by the C/R bit and our own
        role, which is the one thing a loopback cannot check."""
        self.xids += 1
        is_cmd = (f.cr == 1) != bool(self.originator)
        if is_cmd:
            info, params = xid_response(f.info, self.n401, self.k, self.opts,
                                        opt_pi=self.xid_opt_pi)
            self._adopt(params)
            cr = None
            if self.xid_variants:
                label, cr, build = self.xid_variants[
                    (self.xids - 1) % len(self.xid_variants)]
                info = build(f.info)
                self.xid_tried.append((now, label))
            return [self._rsp(u_control(U_XID, 0), info, cr)] * self.xid_reps
        self._adopt(xid_confirm(f.info, self.n401, self.k, self.opts))
        self.xid_due = False
        self.timer = None
        return []

    def _adopt(self, params):
        """8.10.1: "the affected parameter values/procedure settings shall be
        recorded". The transmit direction is the one that changes what we do."""
        self.xid = params
        self.n401 = params.n401_tx
        self.k = params.k_tx

    def send(self, data):
        self.outq.extend(data)

    def received(self):
        out = bytes(self.inq)
        del self.inq[:]
        return out

    def poll(self, now=0.0, max_i=None):
        """Frames to transmit now: new I frames, a due acknowledgement, and
        whatever T401 has decided to repeat.

        `max_i` bounds how many I frames one call will produce. Without it a
        single poll builds the entire window, and then the window is spent on
        frames that are still sitting in our own transmit buffer rather than in
        flight -- which is not what k counts, and which puts every reply we owe
        the far end behind up to 15 frames of our own data.
        """
        out = []
        n_i = 0
        if self.state is self.CONNECTED:
            while self.outq and self._win_open():
                if max_i is not None and n_i >= max_i:
                    break
                n_i += 1
                chunk = bytes(self.outq.popleft()
                              for _ in range(min(self.n401, len(self.outq))))
                self.sent[self.vs] = chunk
                out.append(self._cmd(i_control(self.vs, self.vr), chunk))
                self.vs = (self.vs + 1) % MOD
                self.ack_due = False
                if self.timer is None:
                    self.timer = now + self.t401
            if self.ack_due and not out:
                out.append(self._rsp(s_control(S_RR, self.vr)))
                self.ack_due = False
        if self.timer is not None and now >= self.timer:
            out += self._expire(now)
        return out

    def _expire(self, now):
        self.stats["t401"] += 1
        if self.state in (self.SETUP, self.RELEASING):
            # 8.3.2.2 and 8.3.4: repeat the unnumbered command, give up at N400
            self.retries += 1
            if self.retries > self.n400:
                self.state = self.FAILED
                self.timer = None
                return []
            self.timer = now + self.t401
            return [self._cmd(u_control(
                U_SABME if self.state is self.SETUP else U_DISC, 1))]
        # 8.4.8 waiting acknowledgement. The first expiry *enters* the
        # timer-recovery condition and resets the count; only later ones
        # increment it. The recovery action is an enquiry -- "an appropriate
        # supervisory command ... with the P bit set to 1" -- not a blind
        # retransmission: the peer's F=1 answer says what it actually has, so
        # the resend that follows is the right one rather than a guess.
        if not self.recovery:
            self.recovery = True
            self.retries = 0
        else:
            self.retries += 1
        if self.retries >= self.n400:
            self.state = self.FAILED            # 8.4.9 termination
            self.timer = None
            return []
        self.timer = now + self.t401
        return [self._cmd(s_control(S_RR, self.vr, 1))]

    def _resend(self):
        """Retransmit everything still unacknowledged, oldest first.

        Driven by the pending set rather than by walking V(A) to V(S), which is
        what it used to do and was wrong: a REJ's N(R) acknowledges everything
        below it, so V(A) advances to N(R) -- and if V(S) is also set to N(R), as
        8.4.6 describes, the interval between them is empty and nothing gets
        retransmitted. The frames to repeat are exactly the ones still held, and
        V(S) does not need to move for that.
        """
        out = []
        for n in sorted(self.sent, key=lambda v: (v - self.va) % MOD):
            out.append(self._cmd(i_control(n, self.vr), self.sent[n]))
        self.stats["resend"] += len(out)
        return out

    def feed(self, addr, ctl_info, now=0.0):
        """One received frame in; frames to transmit in reply out."""
        f = parse(addr, ctl_info)
        if f is None:
            self.stats["undefined"] += 1
            return []
        self.stats["rx " + f.kind] += 1
        if f.kind == "SABME":
            if self.sent:
                # Note 3 to 8.3.2.1: "any unacknowledged I frames remain
                # unacknowledged ... Responsibility for the contents of the
                # information fields of such I frames reverts to the control
                # function", which then decides whether to hand them back.
                #
                # We are the control function and we are a transparent pipe, so
                # we hand them back. There is no risk of delivering them twice:
                # 8.3.2.1 has the peer discarding "all frames other than
                # unnumbered format frames" while it is establishing, so it threw
                # these away. Clearing them instead loses user data silently --
                # measured, a caller whose UA went missing to a line error made
                # us drop exactly one window, 15 frames of 128 octets, and both
                # ends then agreed the transfer was complete.
                for n in sorted(self.sent,
                                key=lambda v: (v - self.va) % MOD,
                                reverse=True):
                    self.outq.extendleft(reversed(self.sent[n]))
                self.stats["requeued"] += len(self.sent)
            self.vs = self.vr = self.va = 0
            self.sent.clear()
            self.rejected = False
            self.recovery = False
            self.timer = None
            self.state = self.CONNECTED
            return [self._rsp(u_control(U_UA, f.pf))]
        if f.kind == "UA":
            if self.state is self.SETUP:
                self.vs = self.vr = self.va = 0
                self.sent.clear()
                self.state = self.CONNECTED
                self.timer = None
            elif self.state is self.RELEASING:
                self.state = self.DISCONNECTED
                self.timer = None
            return []
        if f.kind == "DISC":
            self.state = self.DISCONNECTED
            self.timer = None
            return [self._rsp(u_control(U_UA, f.pf))]
        if f.kind == "DM":
            self.state = self.DISCONNECTED if self.state is self.RELEASING \
                else self.FAILED
            self.timer = None
            return []
        if f.kind == "XID":
            return self._got_xid(f, now)
        if self.state is not self.CONNECTED:
            return []
        if f.nr is not None:
            self._ack(f.nr, now, rej=(f.kind == "REJ"))
        if f.kind == "I":
            return self._got_i(f, now)
        if f.kind == "REJ":
            return self._resend()
        if f.kind in ("RR", "RNR", "REJ") and f.pf:
            if self._is_command(f):
                # 8.4.6's enquiry: a command with P=1 is answered with a
                # response carrying F=1. Without the command/response test this
                # answered its own answer, which two of our own entities would
                # do to each other for ever.
                return [self._rsp(s_control(S_RR, self.vr, 1))]
            if self.recovery:
                # 8.4.8: "The timer-recovery condition is cleared when the
                # error-correcting entity receives a valid supervisory response
                # frame with the F bit set to 1", and then transmission or
                # retransmission resumes. V(S) is left where it is on purpose --
                # see _resend.
                self.recovery = False
                self.retries = 0
                self.timer = None
                if f.kind in ("RR", "REJ"):
                    return self._resend()
                self.timer = now + self.t401     # RNR: peer still busy
        return []

    def _is_command(self, f):
        """Table 6 read backwards. Our own role decides: a frame from the
        originator with C/R = 1 is a command, and from the answerer it is a
        response."""
        return (f.cr == 1) != bool(self.originator)

    def _ack(self, nr, now, rej=False):
        """Advance V(A). Anything below it is acknowledged and can be dropped.

        8.4.6 stops T401 only "on receipt of a valid I frame or an RR, RNR, or
        REJ supervisory frame with the N(R) higher than V(A) (actually
        acknowledging some I frames), or an REJ frame with an N(R) equal
        to V(A)", and restarts it afterwards only if I frames are still
        outstanding.

        Restarting it on *every* N(R) instead is a deadlock, and not a
        theoretical one: a Conexant sent 580 RR frames all carrying the same
        N(R) while its own buffer was full, and each one pushed the timer out
        another second. Fifteen frames sat unacknowledged for the rest of the
        call with the window shut, zero retransmissions and zero T401 expiries
        -- a stall driven entirely by the far end's acknowledgements. The
        parenthesis in the clause is the whole rule.
        """
        nr %= MOD
        advanced = nr != self.va
        n = self.va
        while n != nr:
            self.sent.pop(n, None)
            n = (n + 1) % MOD
        self.va = nr
        if advanced or rej:
            self.retries = 0
            self.timer = None if self.va == self.vs else now + self.t401

    def _got_i(self, f, now):
        if f.ns == self.vr:
            self.inq.extend(f.info)
            self.vr = (self.vr + 1) % MOD
            self.rejected = False
            self.ack_due = True
            if f.pf:
                return [self._rsp(s_control(S_RR, self.vr, 1))]
            return []
        # 8.4.5: out of sequence. One REJ, then wait for the retransmission.
        self.stats["seqerr"] += 1
        if self.rejected:
            return []
        self.rejected = True
        return [self._rsp(s_control(S_REJ, self.vr, f.pf))]


class Link:
    """LAPM over the bit stream: framing on one side, an entity on the other.

    This is the piece that plugs into a data phase. Bits in, bits out; octets in,
    octets out. 8.3.2.1 Note 2's "at least 16 flag patterns" before the first
    protocol frame is emitted here, since it is a property of the bit stream
    rather than of the protocol.

    Interframe time fill (8.1.5) is flags, not mark: a receiver hunting for a
    flag has nothing to synchronise on otherwise.
    """

    def __init__(self, originator, ahead=WINDOW, **kw):
        self.lapm = Lapm(originator, **kw)
        self.deframer = Deframer()
        self.preamble = FLAGS_BEFORE_SABME
        # The frame being transmitted, and the queue of whole frames behind it.
        # Kept as a list of frames rather than one flat bit queue so a
        # supervisory frame can jump the queue without splicing itself into the
        # middle of whatever is already going out.
        self.cur = collections.deque()
        self.pend = collections.deque()     # (address, control+information)
        self.prebits = collections.deque()  # 8.3.2.1 Note 2's opening flags
        self.ahead = ahead          # how many I frames may wait in the queue
        self.started = False
        self.txlog = []             # (t, address, control hex, info length)
        self.rxlog = []             # (t, frame kind) as received

    def _render(self, addr, ci):
        """Turn a queued frame into bits, binding N(R) now rather than when it
        was generated.

        Frames do not go out in the order they were built: a reply jumps ahead
        of queued data. If each carried the N(R) it was built with, a frame
        generated earlier would then be transmitted later with a *lower* N(R),
        and N(R) going backwards is an invalid N(R). The Cirrus answered exactly
        that with an FRMR -- rejected control field 2C 08, so N(S) 22 and
        N(R) 4 against its own V(S) of 8, Z bit set -- and then a DISC.

        Binding at transmission time fixes it at the root: every frame that
        carries an N(R) carries the current V(R), so the sequence on the wire is
        monotonic whatever order the frames were made in. It also means an
        acknowledgement is never late, because whatever goes out next is already
        carrying it.
        """
        n = 1 if (ci[0] & 3) == 3 else 2     # 8.2.2: U is one octet, I and S two
        ctl = bytearray(ci[:n])
        if n == 2:
            ctl[1] = ((self.lapm.vr << 1) & 0xFE) | (ctl[1] & 1)   # keep P/F
        return frame(addr, bytes(ctl), ci[n:])

    def _queue(self, frames, now=0.0, front=False):
        """Queue frames for transmission.

        `front` is for supervisory and unnumbered frames, so a reply we owe the
        far end does not wait behind a queue of our own data. With N(R) bound at
        transmission time (see _render) this costs nothing and cannot reorder
        anything that matters.

        `ahead` can throttle how many I frames wait in the queue as well. It
        defaults to the window, i.e. no throttle, because throttling it was
        measured to be worse: against the Cirrus, a limit of two frames produced
        15 retransmissions and 6 T401 expiries where no limit produced 1302
        frames with none of either. The idea had been that queued data delays
        acknowledgements, which late binding already fixes.
        """
        out = []
        for addr, ci in frames:
            self.txlog.append((now, addr[0], ci[:1].hex(), len(ci) - 1))
            out.append((addr, ci))
        if front:
            self.pend.extendleft(reversed(out))
        else:
            self.pend.extend(out)

    def connect(self, now=0.0):
        for _ in range(self.preamble):
            self.prebits.extend(FLAG_BITS)
        self._queue(self.lapm.connect(now), now)
        self.started = True

    def send(self, data):
        self.lapm.send(data)

    def received(self):
        return self.lapm.received()

    def step(self, inbits, n, now=0.0):
        """Feed received bits, take exactly n bits to transmit."""
        for addr, ctl, info in self.deframer.feed(inbits):
            f = parse(addr, ctl + info)
            self.rxlog.append((now, f.kind if f else "?"))
            # Replies to received frames are acknowledgements, enquiries and
            # answers to enquiries. All of them are time-critical; none carries
            # user data. They go to the front.
            self._queue(self.lapm.feed(addr, ctl + info, now), now, front=True)
        # Only ask for new I frames when the queue is nearly empty. Frames
        # sitting in our own bit buffer are not in flight, so letting them
        # consume the window measures the wrong thing and delays everything
        # behind them.
        # Always poll, even with a full queue: a due acknowledgement and a
        # T401 expiry both come out of here, and gating the call on having room
        # for data would stop us acknowledging exactly when we are busiest --
        # which is the bug this scheduling exists to fix.
        room = max(self.ahead - len(self.pend), 0)
        for addr, ci in self.lapm.poll(now, max_i=room):
            self.txlog.append((now, addr[0], ci[:1].hex(), len(ci) - 1))
            if ci[0] & 1:
                self.pend.appendleft((addr, ci))    # not an I frame: urgent
            else:
                self.pend.append((addr, ci))
        out = []
        while len(out) < n:
            if not self.cur:
                if self.prebits:
                    self.cur = self.prebits
                    self.prebits = collections.deque()
                elif self.pend:
                    self.cur = collections.deque(self._render(*self.pend.popleft()))
                else:
                    self.cur = collections.deque(FLAG_BITS)  # 8.1.5 time fill
            out.append(self.cur.popleft())
        return out


class Session:
    """The whole of V.42 over a data phase: detection, then LAPM.

    7.2.1.1 says the role comes from "the role assumed during carrier handshake
    as assigned in the particular modulation Recommendations", so a V.32 answerer
    is the V.42 answerer too and nothing extra has to be negotiated.

    All of this rides on the scrambled data-phase bit stream -- 7.2.1.2 says the
    detection patterns are "sent using the scrambling function of the signal
    converter" -- so it plugs in where the V.14 converter would go.

    If the far end turns out not to speak V.42, `fell_back` goes true and the
    caller should use the V.14 path instead. That is the whole point of the
    detection phase and it has to be reported, not swallowed.
    """

    DETECT, LAPM_UP, FALLBACK = "detect", "lapm", "fallback"

    def __init__(self, originator, supported=True, **kw):
        self.originator = originator
        self.det = Detection(originator, supported=supported)
        self.link = Link(originator, **kw)
        self.phase = self.DETECT
        self.t0 = None
        self.pending = collections.deque()
        self.outq = bytearray()

    @property
    def fell_back(self):
        return self.phase is self.FALLBACK

    @property
    def up(self):
        return self.phase is self.LAPM_UP and self.link.lapm.state == Lapm.CONNECTED

    def put(self, data):
        if self.phase is self.LAPM_UP:
            self.link.send(data)
        else:
            self.outq.extend(data)

    def received(self):
        return self.link.received() if self.phase is self.LAPM_UP else b""

    def step(self, inbits, n, now=0.0):
        """Received bits in, exactly n bits to transmit out."""
        # 9.1.1: T400 "governs the amount of time that a control function ...
        # waits for the ADP or the ODP" -- and waiting cannot begin before the
        # receiver delivers anything to wait on. v32fsm gates the descrambled
        # stream on the eye being open, deliberately, so the detection phase is
        # not decided by junk; but the timer used to start at the data phase
        # regardless, so whatever the gate held back came out of the 750 ms.
        #
        # Measured on recorded runs: one whose ODP was confirmable 520 ms into
        # the delivered bit stream reported "no far end" at 750 ms of frame
        # time, having spent at least 230 ms of the window deaf. Replaying the
        # same recording against the same detector on the bit clock decides
        # lapm. Same bits, same code, opposite answers, and the difference was
        # which clock the timeout ran on.
        if self.t0 is None and inbits:
            self.t0 = now
        elapsed = 0.0 if self.t0 is None else now - self.t0
        if self.phase is self.DETECT:
            out = self.det.feed(inbits, elapsed)
            if self.det.result is Detection.LAPM:
                self.phase = self.LAPM_UP
                # 7.2.1.1: the originator drives establishment
                if self.originator:
                    self.link.connect(now)
                if self.outq:
                    self.link.send(bytes(self.outq))
                    del self.outq[:]
            elif self.det.result is Detection.NONE:
                self.phase = self.FALLBACK
                return [1] * n
            else:
                self.pending.extend(out)
                got = []
                while len(got) < n:
                    got.append(self.pending.popleft() if self.pending else 1)
                return got
        if self.phase is self.FALLBACK:
            return [1] * n
        return self.link.step(inbits, n, now)


# ---------------------------------------------------------------------------
# 12.2 XID information fields
#
# Both hardware modems here send XID as their very first protocol frame, with no
# detection phase at all, and retransmit it N400 times if nothing answers. So
# responding to XID is not optional in practice even though 7.2.2 says
# "negotiation/indication may be omitted if default parameter values and
# procedures are satisfactory" -- that clause lets us decline to *initiate* it,
# not to answer.

XID_FI = 0x82               # 12.2.2: the ISO "general purpose" format
GI_PARAM = 0x80             # 12.2.2 a) parameter negotiation
GI_PRIVATE = 0xF0           # 12.2.2 b) private parameter negotiation

PI_HDLC_OPT = 3             # Table 11a
PI_N401_TX = 5              # in *bits*, per Note 3
PI_N401_RX = 6
PI_K_TX = 7
PI_K_RX = 8

# Table 11b, the private subfield
PI_PSET = 0                 # "V.42" -- Note 2
PI_V42BIS_P0 = 1
PI_V42BIS_P1 = 2
PI_V42BIS_P2 = 3
PSET_V42 = bytes((0x2A, 0x34, 0x32))

# Table 11a Note 1: bit positions in the 32-bit optional-functions mask.
OPT_SREJ = 3                # "3A": selective retransmission, single I frame
OPT_TEST = 14               # loop-back test procedure
OPT_FCS32 = 17              # extended FCS
OPT_SREJ_SPAN = 24          # selective retransmission with span list
# "the transmitter of an XID command frame shall set bit positions 2, 4, 8, 9,
# 12 and 16 to 1 ... A receiver of these frames should ignore these bit
# positions." They carry no meaning for us either way; they are set because the
# clause says to, and ignored on receipt for the same reason.
OPT_CONFORM = (2, 4, 8, 9, 12, 16)


def opt_mask(bits=(), response=False):
    """Table 11a Note 1: a 32-bit mask, bit 1 the low-order bit of octet 1."""
    want = set(bits) | set(OPT_CONFORM)
    # "The transmitter of an XID response frame shall also set these bit
    # positions to 1, except bit position 16 shall be set to 0 if bit position
    # 17 is set to 1."
    if response and OPT_FCS32 in want:
        want.discard(16)
    v = 0
    for b in want:
        v |= 1 << (b - 1)
    return bytes((v >> (8 * k)) & 0xFF for k in range(4))


def mask_bits(pv):
    """The bit positions set in an optional-functions mask, conformance bits
    dropped -- they are noise by definition of the clause that demands them."""
    out = set()
    for k, byte in enumerate(pv):
        for j in range(8):
            if byte & (1 << j):
                out.add(8 * k + j + 1)
    return out - set(OPT_CONFORM)


def _be(value, n):
    """Note 4: "the first octet transmitted shall contain the higher-order
    bits" -- so a plain big-endian integer."""
    return bytes((value >> (8 * (n - 1 - k))) & 0xFF for k in range(n))


def xid_info(params, private=None):
    """Build an XID information field from {PI: PV} subfields.

    12.2.1.2: "The data link layer subfields, if present, follow in ascending
    order according to their GI values" -- so parameter negotiation (0x80) comes
    before private parameter negotiation (0xF0).
    """
    out = bytearray((XID_FI,))
    for gi, items in ((GI_PARAM, params), (GI_PRIVATE, private)):
        if not items:
            continue
        body = bytearray()
        for pi in sorted(items):
            pv = items[pi]
            body.append(pi)
            body.append(len(pv))
            body.extend(pv)
        out.append(gi)
        out.extend(_be(len(body), 2))
        out.extend(body)
    return bytes(out)


def parse_xid(info):
    """{GI: {PI: PV}}, or None if this is not a general-purpose XID.

    12.2.2: "Fields that are not recognized are ignored", so an unknown GI is
    skipped by its own GL rather than treated as an error -- which is the whole
    point of carrying a length.
    """
    if not info or info[0] != XID_FI:
        return None
    out = {}
    i = 1
    while i + 3 <= len(info):
        gi = info[i]
        gl = (info[i + 1] << 8) | info[i + 2]
        j, end = i + 3, i + 3 + gl
        if end > len(info):
            break                       # truncated; take what parsed
        sub = out.setdefault(gi, {})
        while j + 2 <= end:
            pi, pl = info[j], info[j + 1]
            if j + 2 + pl > end:
                break
            sub[pi] = info[j + 2:j + 2 + pl]
            j += 2 + pl
        i = end
    return out


def _pv_int(sub, pi, default):
    pv = sub.get(pi)
    if not pv:
        return default              # "absence of a value indicates use of
    v = 0                           #  the default" (9.2.3, 9.2.4)
    for b in pv:
        v = (v << 8) | b
    return v


class XidParams:
    """What an XID exchange settled on, in the units the protocol uses."""

    def __init__(self, n401_tx=N401, n401_rx=N401, k_tx=WINDOW, k_rx=WINDOW,
                 opts=()):
        self.n401_tx = n401_tx
        self.n401_rx = n401_rx
        self.k_tx = k_tx
        self.k_rx = k_rx
        self.opts = set(opts)

    def __repr__(self):
        return ("XidParams(N401 %d/%d octets, k %d/%d, opts %s)"
                % (self.n401_tx, self.n401_rx, self.k_tx, self.k_rx,
                   sorted(self.opts) or "none"))

    def command(self):
        return xid_info({PI_HDLC_OPT: opt_mask(self.opts),
                         PI_N401_TX: _be(8 * self.n401_tx, 2),
                         PI_N401_RX: _be(8 * self.n401_rx, 2),
                         PI_K_TX: _be(self.k_tx, 1),
                         PI_K_RX: _be(self.k_rx, 1)})


def xid_response(info, n401=N401, k=WINDOW, opts=(), opt_pi=True):
    """Answer an XID command per 9.2.3 and 9.2.4, and say what was settled.

    Both clauses read: "The value chosen by the responder shall be between the
    value chosen by the initiator and the default value, inclusive". The minimum
    of the two is always inside that interval whichever side the initiator picked,
    and it is the only choice that never commits us to more than we offered, so
    that is what this takes.

    The direction crossover is Note 2's, and it is the part worth being careful
    about: transmit and receive are relative to *the sender of the frame*, so the
    initiator's PI 5 (its transmit) is answered by the responder's PI 6 (its
    receive). With everything at its default the crossed and uncrossed responses
    are byte-identical, so a loopback cannot tell them apart -- hence the
    asymmetric case in the tests.
    """
    sub = (parse_xid(info) or {}).get(GI_PARAM, {})
    # theirs, in our units
    t_n401_tx = _pv_int(sub, PI_N401_TX, 8 * N401) // 8
    t_n401_rx = _pv_int(sub, PI_N401_RX, 8 * N401) // 8
    t_k_tx = _pv_int(sub, PI_K_TX, WINDOW)
    t_k_rx = _pv_int(sub, PI_K_RX, WINDOW)
    theirs = mask_bits(sub.get(PI_HDLC_OPT, b""))
    agreed = theirs & set(opts)

    # initiator->responder is their PI 5 / PI 7, which is our receive direction
    rx_n401 = min(t_n401_tx, n401)
    rx_k = min(t_k_tx, k)
    # responder->initiator is their PI 6 / PI 8, which is our transmit direction
    tx_n401 = min(t_n401_rx, n401)
    tx_k = min(t_k_rx, k)

    out = {PI_N401_TX: _be(8 * tx_n401, 2),
           PI_N401_RX: _be(8 * rx_n401, 2),
           PI_K_TX: _be(tx_k, 1),
           PI_K_RX: _be(rx_k, 1)}
    if opt_pi:
        out[PI_HDLC_OPT] = opt_mask(agreed, response=True)
    info_out = xid_info(out)
    return info_out, XidParams(tx_n401, rx_n401, tx_k, rx_k, agreed)


def xid_confirm(info, n401=N401, k=WINDOW, opts=()):
    """Read an XID *response* to a command we sent. The crossover applies the
    same way, from the other side: their PI 6 is our transmit direction."""
    sub = (parse_xid(info) or {}).get(GI_PARAM, {})
    tx_n401 = min(_pv_int(sub, PI_N401_RX, 8 * N401) // 8, n401)
    rx_n401 = min(_pv_int(sub, PI_N401_TX, 8 * N401) // 8, n401)
    tx_k = min(_pv_int(sub, PI_K_RX, WINDOW), k)
    rx_k = min(_pv_int(sub, PI_K_TX, WINDOW), k)
    return XidParams(tx_n401, rx_n401, tx_k, rx_k,
                     mask_bits(sub.get(PI_HDLC_OPT, b"")) & set(opts))
