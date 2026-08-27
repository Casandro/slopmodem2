"""Bridge a V.32bis data phase to a TCP stream, as a raw byte pipe.

No modem control signals in either direction: no AT commands, no DTR/DSR or
RTS/CTS, no CONNECT or NO CARRIER text. Bytes off the line go to the socket and
bytes off the socket go to the line, and the only thing either side learns about
the other is that it stopped.

Kept clear of SIP and RTP so the offline tests can import it: sip_glue calls
sipcfg.load() at import time and raises SystemExit without testrig/ata.md.

Two conventions run through the whole file and are worth stating once.

**Actions live in frame(), decisions live in stop_reason(), and stop_reason takes
`now` as an argument** rather than calling time.time() itself. Every termination
branch is then reachable in a test by advancing a float, which matters because
the interesting ones are timeouts of five to nine hundred seconds.

**Backpressure is the absence of a read, not a buffer.** While the handshake is
running, while a retrain is in progress, and whenever the modem's queue is
already full, this simply does not call recv(). The kernel's receive window
closes and the host is throttled to the line rate by TCP itself, with nothing of
ours in the path to go stale or to size wrongly. A terminal server's host
typically writes a banner the instant it accepts; that banner sits in the socket
buffer and is delivered the moment the data phase opens.
"""
import socket

import v32flow
import v32fsm
import v42

FEED_MAX = 64           # octets offered per 20 ms frame; see feed_budget below
LAPM_OUTQ_HI = 1024     # queue depth, not window; measured free -- see v32flow
SEND_CHUNK = 8192
COMPACT_AT = 65536

TCP_OUT_HI = 262144     # 256 KiB, ~145 s of buffering at the fastest line rate
TCP_STALL = 30.0        # ... sustained beyond that, the peer is not slow, it is stopped
PEEK_EVERY = 1.0

HANDSHAKE_MAX = 60.0    # 5.4 with retrains can legitimately take forty seconds
CARRIER_LOST = 5.0      # 5x the FSM's own RETRAIN_LOSS verdict of one second
RETRAIN_STUCK = 20.0    # a whole 5.4 start-up is about five
RTP_DEAD = 10.0         # 500 missed frames
IDLE_MAX = 900.0        # a shell prompt is allowed to be quiet; 0 disables
MAX_CALL = 3600.0

DETECT_MAX = 3.0        # 7.2.1's T400 is 750 ms; four times that is generous
LAPM_UP_MAX = 8.0       # see _v42_watchdogs -- this one is load-bearing
LAPM_SETTLE = 0.25
V14_LINGER = 0.5
POST_FIN_MAX = 15.0


class Bridge:
    """One call's worth of byte pumping. Construct per call, then frame() it."""

    def __init__(self, m, sock, feed=FEED_MAX, lapm_hi=LAPM_OUTQ_HI,
                 idle_max=IDLE_MAX, t0=0.0, log=None):
        self.m = m
        self.sock = sock
        self.feed = feed
        self.lapm_hi = lapm_hi
        self.idle_max = idle_max
        self.log = log
        self.t0 = self.last_rtp = self.last_move = self.last_peek = t0
        self.outbuf = bytearray()
        self.out_off = 0
        self.tcp_eof = False
        self.wr_shut = False
        self.fatal = None
        self.t_data = None
        self.t_lapm = None
        self.t_fin = None
        self.t_nodd = None
        self.t_nondata = None
        self.t_drained = None
        self.over_since = None
        self.ec_was_up = False
        self.rx_bytes = self.tx_bytes = self.sent_bytes = 0
        self.discarded = 0
        self.notes = []

    def note(self, msg):
        self.notes.append(msg)
        if self.log is not None:
            self.log(msg)

    # -- per frame -------------------------------------------------------

    def frame(self, now):
        """One 20 ms turn. Call after m.step(), once per RTP frame."""
        m = self.m
        self._v42_watchdogs(now)

        # ---- modem -> us. Every frame, in every state. -------------------
        # rx.chars and lapm.inq are both unbounded, and the existing programs
        # only drain them while state == DATA, so anything recovered around the
        # edges of a retrain arrives later as one burst. Draining always costs
        # nothing and removes the burst.
        new = m.received()
        if new:
            if m.ec is not None or m.state == v32fsm.DATA:
                self.outbuf.extend(new)
                self.rx_bytes += len(new)
                self.last_move = now
            else:
                # LAPM output is FCS-protected, so whatever survives is real and
                # is forwarded in any state. V.14 output during a retrain is
                # whatever a closing eye happened to frame, and 5.5 loses
                # in-flight V.14 characters by definition -- so drop it rather
                # than inject noise into what the host is told is a byte stream.
                self.discarded += len(new)

        self._flush(now)

        # ---- us -> modem, budgeted --------------------------------------
        if not self.tcp_eof and not self.fatal and m.state == v32fsm.DATA:
            want = v32flow.feed_budget(m, self.feed, lapm_hi=self.lapm_hi)
            if want > 0:
                data = self._recv(want)
                if data is None:
                    pass
                elif data == b"":
                    self._saw_fin(now)
                else:
                    m.put(data)
                    self.tx_bytes += len(data)
                    self.last_move = now
            elif now - self.last_peek > PEEK_EVERY:
                # want == 0 means we are not reading, so a FIN would go
                # unnoticed for as long as the queue stays full. MSG_PEEK sees
                # it without consuming anything.
                self.last_peek = now
                d = self._recv(1, peek=True)
                if d == b"":
                    self._saw_fin(now)

    def _recv(self, n, peek=False):
        """None on would-block, b"" on EOF, bytes otherwise.

        Never called with n == 0: recv(0) returns b"" and is indistinguishable
        from end of stream, which would hang every call up moments after it
        connected.
        """
        if n <= 0:
            return None
        try:
            return self.sock.recv(n, socket.MSG_PEEK) if peek else self.sock.recv(n)
        except BlockingIOError:
            return None
        except OSError as e:
            self.fatal = "tcp-error-%s" % (e.errno or "?")
            return None

    def _flush(self, now):
        """Non-blocking, partial writes retained for the next frame.

        Shaped after dte.Dte._write, minus its drop-on-overflow tail: discarding
        bytes is defensible for a pty console and never for a TCP stream, where
        the hole is undetectable to whatever rides on top.
        """
        for _ in range(16):
            pend = len(self.outbuf) - self.out_off
            if pend <= 0:
                break
            view = memoryview(self.outbuf)[self.out_off:self.out_off + SEND_CHUNK]
            try:
                n = self.sock.send(view)
            except BlockingIOError:
                break
            except OSError as e:
                self.fatal = "tcp-error-%s" % (e.errno or "?")
                return
            if n <= 0:
                break
            self.out_off += n
            self.sent_bytes += n
        # compact rather than del[:n] every frame: a quarter-megabyte buffer
        # would otherwise be memmoved fifty times a second
        if self.out_off >= len(self.outbuf):
            self.outbuf = bytearray()
            self.out_off = 0
        elif self.out_off > COMPACT_AT:
            del self.outbuf[:self.out_off]
            self.out_off = 0
        pend = len(self.outbuf) - self.out_off
        if pend > TCP_OUT_HI:
            self.over_since = self.over_since or now
        else:
            self.over_since = None

    def _saw_fin(self, now):
        if not self.tcp_eof:
            self.tcp_eof = True
            self.t_fin = now
            self.note("TCP peer closed its end")

    # -- V.42 watchdogs --------------------------------------------------

    def _v42_watchdogs(self, now):
        """Force V.14 when V.42 has concluded but will never come up.

        The second of these is not defensive coding, it is a measured failure.
        The answerer's detection phase matches DC1 with alternating parity
        against the *scrambled* data-phase bit stream, and a V.14-only far end
        whose DTE sends characters during the 750 ms detection window produces
        that pattern by chance. Detection then declares LAPM, the answerer sends
        its ADPs -- and 7.2.1.1 makes the *originator* send SABME, which a V.14
        modem never will. The session sits in phase 'lapm' with lapm.state
        'disconnected' and ec.up False for the rest of the call: put() accepts
        nothing, received() returns b"" forever, ec_fell_back is never set, and
        not one octet moves in either direction with no error reported anywhere.

        Reproduced soft to soft at 9600: 0 octets recovered across 1500 frames.
        With this watchdog the same run falls back and then carries characters
        at 100.00% both ways.
        """
        m = self.m
        if m.state != v32fsm.DATA:
            return
        if self.t_data is None:
            self.t_data = now
        ec = m.ec
        if ec is None:
            return
        if ec.phase is v42.Session.DETECT:
            if now - self.t_data > DETECT_MAX:
                self._force_v14("V.42 detection never concluded")
        elif not ec.up:
            if self.t_lapm is None:
                self.t_lapm = now
            elif now - self.t_lapm > LAPM_UP_MAX:
                self._force_v14("detection said LAPM but the link never came up")
        else:
            self.t_lapm = None
            self.ec_was_up = True

    def _force_v14(self, why):
        self.note("7.2.1.3 forced: %s - running V.14" % why)
        if hasattr(self.m, "force_v14"):
            self.m.force_v14(why)
        else:
            self.m._ec_fallback()

    # -- termination -----------------------------------------------------

    def _tx_drained(self, now):
        """Everything we accepted has actually left, and been acknowledged."""
        if len(self.outbuf) - self.out_off:
            self.t_drained = None
            return False
        m = self.m
        if m.ec is not None and m.ec.up:
            lapm = m.ec.link.lapm
            # not just outq: `sent` empty means every I frame has been
            # acknowledged by the far modem, which is a real delivery receipt
            # and the one thing V.14 cannot give us.
            ok, settle = (not lapm.outq and not lapm.sent), LAPM_SETTLE
        else:
            # pending() is only len(q); enc.bits holds the character currently
            # being shifted out, so both have to be empty.
            ok = m.enc.pending() == 0 and not getattr(m.enc, "bits", None)
            settle = V14_LINGER
        if not ok:
            self.t_drained = None
            return False
        self.t_drained = self.t_drained or now
        if now - self.t_drained < settle:
            return False
        if not self.wr_shut:
            try:
                self.sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            self.wr_shut = True
        return True

    def stop_reason(self, now):
        """Why this call should end, or None. Pure: never reads the clock."""
        m = self.m
        if self.fatal:
            return self.fatal
        if now - self.last_rtp > RTP_DEAD:
            return "rtp-dead"
        if self.over_since and now - self.over_since > TCP_STALL:
            return "tcp-stalled"
        if self.t_data is None:
            return "no-handshake" if now - self.t0 > HANDSHAKE_MAX else None
        if m.state == v32fsm.FAILED:
            return "fsm-failed"

        if m.state == v32fsm.DATA:
            self.t_nondata = None
            # v32fsm never assigns FAILED, and once retrains reaches RETRAIN_MAX
            # _retrain_trigger returns None on its first line and nothing will
            # ever ask again -- the FSM then sits in DATA with a dead receiver
            # for as long as anyone lets it. This is that backstop.
            if m.rx is not None and getattr(m.rx.rx, "dd", False):
                self.t_nodd = None
            else:
                self.t_nodd = self.t_nodd or now
                if now - self.t_nodd > CARRIER_LOST:
                    return "carrier-lost"
        else:
            self.t_nodd = None
            self.t_nondata = self.t_nondata or now
            if now - self.t_nondata > RETRAIN_STUCK:
                return "retrain-stuck"

        if self.ec_was_up and m.ec is not None and \
                m.ec.link.lapm.state in (v42.Lapm.FAILED, v42.Lapm.DISCONNECTED):
            return "lapm-down"

        if self.tcp_eof:
            if self._tx_drained(now):
                return "tcp-eof"
            if now - self.t_fin > POST_FIN_MAX:
                return "post-fin-timeout"

        if self.idle_max and now - self.last_move > self.idle_max:
            return "idle"
        return None

    def close(self):
        """Let the host's read() return 0 so its session process exits.

        Not SO_LINGER(0): a reset leaves the far side's getty or telnetd waiting
        on a socket that will never report end of file, and one leaked session
        per call is the kind of thing that only shows up on the tenth.
        """
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def stats(self):
        return {"tcp_in": self.tx_bytes, "tcp_out": self.sent_bytes,
                "from_line": self.rx_bytes, "discarded": self.discarded,
                "pending": len(self.outbuf) - self.out_off,
                "notes": list(self.notes)}
