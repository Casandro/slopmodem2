"""DTE-side interface: a pseudo-terminal plus a minimal ITU-T V.250 command set.

Everything up to here has been a modem with no user. The line side works in both
directions -- `tracking.LiveRx` decodes the caller live in the RTP callback, and
`fsm.AnswerV22bis` puts our data on the line -- but the data was a fixed pattern
compiled into the modulator, and the characters it recovered ended up in a
buffer. This module gives the modem the other half of its job: something a DTE
can actually open, configure and talk through.

Three pieces:

  AsyncEncoder  the V.14 transmit converter. V.22bis 4.1.2 says start-stop data
                "shall be converted in conformity with Recommendation V.14 to a
                synchronous data stream". That means framing each byte 8N1 and,
                crucially, emitting mark bits when the DTE has nothing to send --
                the line runs at a fixed 2400 bit/s whether or not there is data
                for it. This is exactly the behaviour that was *observed* coming
                back from the hardware modems and written up as a "slip".

  Dte           a pseudo-terminal. The slave path (/dev/pts/N) is a real
                character device with real termios, so any serial terminal
                program can open it -- minicom, kermit, pyserial. The modem holds
                the master end.

  V.250 command handling, inside Dte: command mode versus data mode, the
                result codes, the S-registers that matter, and the +++ escape
                with its guard times.

Known limitation, stated rather than hidden: a pseudo-terminal has no hardware
modem-control leads. There is no way to assert DCD (circuit 109), CTS (106) or
DSR (107) on a pty, so those are conveyed the way V.250 conveys them to software
that cannot see the leads either -- as result codes and as `AT&V` state. Circuit
106 is stood in for by back-pressure: the modem stops reading from the pty once
its transmit queue is full, so the DTE's own write() blocks, which is what CTS
off accomplishes on a real interface.
"""
import collections, os, pty, termios, time, tty

FRAME_S = 0.02                      # one RTP frame, and our command-mode clock


class AsyncEncoder:
    """V.14 transmit converter: bytes in, a continuous 8N1 bit stream out.

    `take(n)` always returns exactly n bits. When the queue is empty it returns
    mark bits, because the line does not stop for want of data. `idled` counts
    those, which makes DTE starvation measurable rather than mysterious: the
    slips seen coming back from the Conexant were this, on the far side, when the
    Pi fed it at exactly the line rate and left no margin.
    """

    # Deletion is triggered by queue depth, so the threshold has to sit above
    # the natural burstiness of the caller. The modem polls once per 20 ms RTP
    # frame, which at 9600 bit/s is 48 characters, so a depth of 2 would delete
    # every stop bit in the stream -- and a stream with no mark bits left in it
    # is one the far framer can never acquire on. 128 is comfortably clear of one
    # frame's worth and still bounds the added latency to about 130 ms.
    def __init__(self, idle=2, hiwater=128, delete_stops=False):
        self.q = collections.deque()
        self.bits = collections.deque()
        self.idle = idle
        self.hiwater = hiwater
        # Off by default. Deletion is what V.14 is *for*, but the V.22bis path
        # was built and verified against two real modems without it, and a
        # caller that queues a block up front rather than pacing it looks exactly
        # like a DTE running fast -- so switching it on globally started deleting
        # stop bits in streams whose deframers had no reason to expect any.
        # V.32's data phase asks for it explicitly; nothing else has to change.
        self.delete_stops = delete_stops
        self.chars = 0                  # characters framed onto the line
        self.idled = 0                  # mark bits sent for want of data
        self.deleted = 0                # stop bits deleted per V.14
        self.last_del = False

    def put(self, data):
        self.q.extend(data)

    def pending(self):
        return len(self.q)

    def take(self, n):
        out = []
        bits, q = self.bits, self.q
        for _ in range(n):
            if not bits:
                if q:
                    ch = q.popleft()
                    bits.append(0)                      # start bit
                    for k in range(8):
                        bits.append((ch >> k) & 1)      # data, LSB first
                    if (self.delete_stops and len(q) > self.hiwater
                            and not self.last_del):
                        # V.14 stop-bit deletion, which is the whole point of the
                        # conversion: a start-stop DTE may run slightly faster
                        # than the synchronous line, and Table 8/V.32 says how
                        # much -- 9600 to 9696 bit/s basic at 9600, 9821
                        # extended. Deleting a stop bit takes a character from 10
                        # bits to 9, so a fraction p of characters deleted
                        # lets the DTE run at 10/(10-p) times the line rate:
                        # p = 0.1 reaches Table 8's basic 9696 and p = 0.226 its
                        # extended 9821. Data bits are never deleted.
                        #
                        # Never twice in a row. The far framer treats a zero in
                        # the stop position as a deletion, but a *run* of them as
                        # lost framing -- which is how it still catches a real
                        # slip. Bang-bang on queue depth alone produced runs of
                        # hundreds, the framer resynchronised in the middle of
                        # them, and characters were lost. One-in-two is still
                        # p = 0.5, twice what the extended range needs.
                        self.deleted += 1
                        self.last_del = True
                    else:
                        self.last_del = False
                        bits.append(1)                  # stop bit
                        for _k in range(self.idle):
                            bits.append(1)              # inter-character idle
                    self.chars += 1
                else:
                    out.append(1)
                    self.idled += 1
                    continue
            out.append(bits.popleft())
        return out


# ---------------------------------------------------------------------------
# V.250
# ---------------------------------------------------------------------------

OK, CONNECT, RING, NO_CARRIER, ERROR, NO_ANSWER = 0, 1, 2, 3, 4, 8

_VERBOSE = {OK: "OK", CONNECT: "CONNECT", RING: "RING",
            NO_CARRIER: "NO CARRIER", ERROR: "ERROR", NO_ANSWER: "NO ANSWER"}


class Dte:
    """The DTE side of the modem: a pty, a command interpreter, and two modes.

    `poll()` is called once per RTP frame. It never blocks: the master end is
    non-blocking, and if the DTE is not reading, outbound characters queue up to
    a bound and the overflow is counted.
    """

    def __init__(self, idle=2, s0=1, log=None, can_dial=False):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)          # no echo, no CR/LF translation
        os.set_blocking(self.master, False)
        self.name = os.ttyname(self.slave)
        self.tx = AsyncEncoder(idle=idle)
        self.log = log if log is not None else []

        # V.250 options and S-registers
        self.echo = True                # ATE1
        self.quiet = False              # ATQ0
        self.verbose = True             # ATV1
        self.s = {0: s0, 2: ord("+"), 3: 13, 4: 10, 5: 8, 12: 50}
        self.dcd_follows = True         # AT&C1
        self.dtr_hangup = True          # AT&D2

        self.online = False             # data mode
        self.connected = False          # a call is up
        self.rate = 2400
        self.can_dial = can_dial        # ATD accepted at all
        self.want_dial = None           # the number from ATD
        self.want_answer = False        # ATA seen
        self.want_hangup = False        # ATH seen
        self.want_online = False        # ATO seen

        self.cmd = bytearray()          # command-line accumulator
        self.outq = bytearray()         # to the DTE, when it is not reading
        self.dropped = 0
        self.rx_chars = 0
        self.tx_chars = 0
        self.hiwater = 4096

        # +++ escape state, V.250 5.2.3
        self.plus_run = 0
        self.idle_frames = 0
        self.plus_at = None

    # -- plumbing --------------------------------------------------------

    def _ev(self, msg):
        self.log.append(msg)

    def _write(self, data):
        if not data:
            return
        if self.outq:
            self.outq.extend(data)
            data = b""
        buf = bytes(self.outq) if self.outq else data
        try:
            n = os.write(self.master, buf)
        except (BlockingIOError, OSError):
            n = 0
        rest = buf[n:]
        if len(rest) > 65536:
            self.dropped += len(rest) - 65536
            rest = rest[-65536:]
        self.outq = bytearray(rest)

    def result(self, code, suffix=""):
        """Emit a result code in the format ATV and ATQ ask for."""
        if self.quiet:
            return
        cr, lf = chr(self.s[3]), chr(self.s[4])
        if self.verbose:
            txt = _VERBOSE.get(code, str(code)) + suffix
            self._write(("%s%s%s%s%s" % (cr, lf, txt, cr, lf)).encode())
        else:
            self._write(("%d%s" % (code, cr)).encode())
        self._ev("-> DTE %s%s" % (_VERBOSE.get(code, code), suffix))

    # -- events from the line side ---------------------------------------

    def ring(self):
        self.result(RING)

    def connect(self, rate=None):
        if rate:
            self.rate = rate
        self.connected = True
        self.online = True
        self.result(CONNECT, " %d" % self.rate)

    def no_carrier(self):
        self.connected = False
        self.online = False
        self.result(NO_CARRIER)

    def deliver(self, data):
        """Characters recovered from the line, headed for the DTE."""
        if not data:
            return
        self.rx_chars += len(data)
        if self.online:
            self._write(data)
        else:
            # escaped to command mode with the call still up: V.250 keeps the
            # connection, so buffer rather than discard, bounded.
            self.outq.extend(data)

    # -- the frame tick --------------------------------------------------

    def poll(self):
        """Read whatever the DTE has written and act on it. Never blocks."""
        self._write(b"")                        # drain any backlog first
        if self.tx.pending() >= self.hiwater:
            # Stand in for circuit 106 going off: stop reading, and the DTE's
            # own write() blocks once the pty buffer fills.
            return
        try:
            data = os.read(self.master, 4096)
        except (BlockingIOError, OSError):
            data = b""
        if not data:
            self.idle_frames += 1
            self._check_escape(None)
            return
        self.idle_frames = 0
        if self.online:
            self._data_mode(data)
        else:
            self._command_mode(data)

    def _check_escape(self, data):
        """V.250 5.2.3 guard timing: idle, S2 three times, idle."""
        guard = max(1, int(self.s[12] * 0.02 / FRAME_S))
        if self.plus_at is not None and self.idle_frames >= guard:
            self.plus_at = None
            self.plus_run = 0
            self.online = False
            self._ev("+++ escape: command mode, call still up")
            self.result(OK)

    def _data_mode(self, data):
        s2 = self.s[2]
        guard = max(1, int(self.s[12] * 0.02 / FRAME_S))
        # A run of exactly three S2 characters, preceded by an idle period, is
        # the escape. Anything else -- including a fourth -- is just data.
        if data == bytes([s2]) * len(data) and len(data) <= 3:
            if self.plus_run == 0 and self.idle_frames < guard:
                pass                              # no preceding idle: data
            self.plus_run += len(data)
            if self.plus_run == 3:
                self.plus_at = time.time()
                return
            if self.plus_run < 3:
                return
        if self.plus_run:
            # not an escape after all: the buffered plusses were data
            self.tx.put(bytes([s2]) * self.plus_run)
            self.tx_chars += self.plus_run
            self.plus_run = 0
            self.plus_at = None
        self.tx.put(data)
        self.tx_chars += len(data)

    def _command_mode(self, data):
        for b in data:
            if self.echo:
                self._write(bytes([b]))
            if b == self.s[3]:                    # CR ends the line
                line = bytes(self.cmd)
                self.cmd = bytearray()
                self._run_line(line)
            elif b == self.s[5]:                  # backspace
                if self.cmd:
                    self.cmd.pop()
            elif b in (self.s[4],):               # bare LF: ignore
                pass
            else:
                if len(self.cmd) < 256:
                    self.cmd.append(b)

    # -- the command interpreter -----------------------------------------

    def _run_line(self, line):
        txt = line.decode("latin-1", "replace").strip()
        if not txt:
            return
        if txt.upper() in ("A/", "A/\r"):
            txt = self.last if hasattr(self, "last") else ""
        if not txt.upper().startswith("AT"):
            self.result(ERROR)
            return
        self.last = txt
        body = txt[2:]
        self._ev("<- DTE %s" % txt)
        try:
            out = self._commands(body)
        except ValueError:
            self.result(ERROR)
            return
        for chunk in out:
            cr, lf = chr(self.s[3]), chr(self.s[4])
            self._write(("%s%s%s%s%s" % (cr, lf, chunk, cr, lf)).encode())
        if not (self.want_answer or self.want_dial):
            self.result(OK)

    def _commands(self, body):
        """Parse a concatenated V.250 command line. Raises ValueError on any
        command we do not implement, which V.250 answers with ERROR."""
        out = []
        i = 0
        up = body.upper()
        n = len(body)
        while i < n:
            c = up[i]
            if c in " \t":
                i += 1
                continue
            if c == "+":                          # extended command
                j = i + 1
                while j < n and (up[j].isalnum() or up[j] in "&"):
                    j += 1
                name = up[i:j]
                arg = ""
                if j < n and up[j] in "=?":
                    if up[j] == "?":
                        arg = "?"
                        j += 1
                    else:
                        k = j + 1
                        while k < n and up[k] not in ";":
                            k += 1
                        arg = body[j + 1:k]
                        j = k
                out.extend(self._extended(name, arg))
                i = j
                continue
            if c == "&":
                if i + 1 >= n:
                    raise ValueError(body)
                letter = up[i + 1]
                i += 2
                num, i = self._num(up, i)
                out.extend(self._amp(letter, num))
                continue
            i += 1
            if c == "S":
                reg, i = self._num(up, i, required=True)
                if i < n and up[i] == "?":
                    i += 1
                    out.append("%03d" % self.s.get(reg, 0))
                elif i < n and up[i] == "=":
                    i += 1
                    val, i = self._num(up, i, required=True)
                    self.s[reg] = val
                else:
                    raise ValueError(body)
                continue
            num, i = self._num(up, i)
            if c == "E":
                self.echo = bool(num)
            elif c == "Q":
                self.quiet = bool(num)
            elif c == "V":
                self.verbose = bool(num)
            elif c == "Z":
                self._reset()
            elif c == "A":
                self.want_answer = True
            elif c == "H":
                self.want_hangup = True
            elif c == "O":
                if not self.connected:
                    raise ValueError(body)
                self.want_online = True
            elif c == "I":
                out.append(self._info(num))
            elif c in "XWLMN":                    # accepted and ignored
                pass
            elif c == "D":
                # V.250 6.3.1: D takes the rest of the line. Strip the dial
                # modifiers we do not need and keep the digits, * and #, which is
                # what this PBX's numbering plan uses.
                if not self.can_dial:
                    raise ValueError(body)
                rest = body[i:].split(";")[0]
                num = "".join(ch for ch in rest if ch.isdigit() or ch in "*#")
                if not num:
                    raise ValueError(body)
                self.want_dial = num
                self._ev("ATD %s" % num)
                i = n
                continue
            else:
                raise ValueError(body)
        return out

    @staticmethod
    def _num(s, i, required=False):
        j = i
        while j < len(s) and s[j].isdigit():
            j += 1
        if j == i:
            if required:
                raise ValueError(s)
            return 0, i
        return int(s[i:j]), j

    def _extended(self, name, arg):
        if name == "+GMI":
            return ["slopmodem"]
        if name == "+GMM":
            return ["V.22bis soft-modem"]
        if name == "+GMR":
            return ["1.0"]
        if name == "+MS" and arg == "?":
            return ["+MS: V22B,1,1200,2400"]
        if name == "+IFC" and arg == "?":
            return ["+IFC: 0,0"]
        raise ValueError(name)

    def _amp(self, letter, num):
        if letter == "C":
            self.dcd_follows = bool(num)
        elif letter == "D":
            self.dtr_hangup = num >= 2
        elif letter == "F":
            self._reset()
        elif letter == "V":
            return [self._profile()]
        else:
            raise ValueError("&" + letter)
        return []

    def _reset(self):
        self.echo = True
        self.quiet = False
        self.verbose = True
        self.s.update({0: self.s.get(0, 1), 2: ord("+"), 3: 13, 4: 10, 5: 8,
                       12: 50})

    def _info(self, num):
        if num == 0:
            return "slopmodem V.22bis"
        if num == 1:
            return "line rate 2400 bit/s, 600 baud, 16-QAM"
        return "OK"

    def _profile(self):
        return ("E%d Q%d V%d &C%d &D%d S0:%03d S2:%03d S12:%03d"
                % (self.echo, self.quiet, self.verbose, self.dcd_follows,
                   2 if self.dtr_hangup else 0, self.s[0], self.s[2],
                   self.s[12]))

    # -- reporting -------------------------------------------------------

    def summary(self):
        return {"device": self.name, "dialled": self.want_dial,
                "from_dte": self.tx_chars,
                "to_dte": self.rx_chars, "framed": self.tx.chars,
                "idle_bits": self.tx.idled, "dropped": self.dropped,
                "queued": self.tx.pending()}

    def close(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass
