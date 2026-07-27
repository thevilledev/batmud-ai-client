"""Telnet transport for BatMUD.

BatMUD negotiates nothing: probing the live server shows it never initiates an
option and answers ``WONT``/``DONT`` to every option offered, including NAWS,
TTYPE, CHARSET, EOR, GMCP, MSDP and MCCP. The only telnet feature it actually
uses is ``IAC GA`` to mark prompts. A full option-negotiating stack would
therefore be dead weight, so this module implements just enough of RFC 854 to
strip commands, refuse anything offered, and surface Go-Ahead.

Text is ISO-8859-1 in both directions; since CHARSET is refused there is no way
to ask for UTF-8.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import ssl
import unicodedata
from collections.abc import AsyncIterator

from .batclient import BC_ENABLE, BatClientParser
from .events import Disconnected, Event

log = logging.getLogger(__name__)

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
GA = 249
SE = 240
EOR = 239

ENCODING = "iso-8859-1"

DEFAULT_HOST = "batmud.bat.org"
TLS_PORT = 2022
PLAIN_PORT = 2023


class Marker(enum.Enum):
    """Non-text items in the inbound stream."""

    GO_AHEAD = enum.auto()


Item = str | Marker


class _State(enum.Enum):
    DATA = enum.auto()
    COMMAND = enum.auto()
    OPTION = enum.auto()
    SUBNEGOTIATION = enum.auto()
    SUBNEGOTIATION_IAC = enum.auto()


class TelnetFilter:
    """Splits an inbound byte stream into text runs and Go-Ahead markers.

    Any option the server offers is refused. Replies accumulate in
    :attr:`pending_replies` for the caller to write back.
    """

    def __init__(self) -> None:
        self._state = _State.DATA
        self._command = 0
        self._buffer = bytearray()
        self.pending_replies = bytearray()

    def feed(self, data: bytes) -> list[Item]:
        items: list[Item] = []
        for byte in data:
            match self._state:
                case _State.DATA:
                    if byte == IAC:
                        self._state = _State.COMMAND
                    else:
                        self._buffer.append(byte)
                case _State.COMMAND:
                    self._handle_command(byte, items)
                case _State.OPTION:
                    self._refuse(self._command, byte)
                    self._state = _State.DATA
                case _State.SUBNEGOTIATION:
                    if byte == IAC:
                        self._state = _State.SUBNEGOTIATION_IAC
                case _State.SUBNEGOTIATION_IAC:
                    self._state = _State.DATA if byte == SE else _State.SUBNEGOTIATION
        self._emit_text(items)
        return items

    def _handle_command(self, byte: int, items: list[Item]) -> None:
        if byte == IAC:
            self._buffer.append(IAC)
            self._state = _State.DATA
        elif byte in (WILL, WONT, DO, DONT):
            self._command = byte
            self._state = _State.OPTION
        elif byte == SB:
            self._state = _State.SUBNEGOTIATION
        elif byte in (GA, EOR):
            self._emit_text(items)
            items.append(Marker.GO_AHEAD)
            self._state = _State.DATA
        else:
            # NOP, DM, BRK, IP, AO, AYT, EC, EL: nothing useful to do.
            self._state = _State.DATA

    def _refuse(self, command: int, option: int) -> None:
        if command == WILL:
            self.pending_replies += bytes((IAC, DONT, option))
        elif command == DO:
            self.pending_replies += bytes((IAC, WONT, option))
        # WONT and DONT need no reply.

    def _emit_text(self, items: list[Item]) -> None:
        if not self._buffer:
            return
        items.append(self._buffer.decode(ENCODING))
        self._buffer.clear()


_TRANSLITERATIONS = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": ",",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",
    "\u2014": "-",
    "\u2026": "...",
    "\u2022": "*",
    "\u20ac": "EUR",
}


def _transliterate(char: str) -> str:
    """Best-effort Latin-1 stand-in for a character that has none."""
    if char in _TRANSLITERATIONS:
        return _TRANSLITERATIONS[char]
    decomposed = unicodedata.normalize("NFKD", char)
    return "".join(part for part in decomposed if not unicodedata.combining(part))


def encode_command(text: str) -> bytes:
    """Encode an outbound line as ISO-8859-1 with IAC escaped.

    Characters outside Latin-1 are transliterated rather than dropped so a
    pasted typographic quote still reaches the game as something sensible.
    Latin-1 characters are passed through untouched, which matters because
    normalising them would decompose accents the game understands.
    """
    try:
        payload = text.encode(ENCODING)
    except UnicodeEncodeError:
        chunks = bytearray()
        for char in text:
            try:
                chunks += char.encode(ENCODING)
            except UnicodeEncodeError:
                chunks += _transliterate(char).encode(ENCODING, errors="ignore")
        payload = bytes(chunks)
    return payload.replace(bytes((IAC,)), bytes((IAC, IAC))) + b"\n"


class Connection:
    """A live BatMUD session yielding parsed events."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = TLS_PORT,
        *,
        use_tls: bool = True,
        verify_tls: bool = True,
        enable_batclient: bool = True,
        read_size: int = 4096,
    ) -> None:
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.verify_tls = verify_tls
        self.enable_batclient = enable_batclient
        self.read_size = read_size
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._telnet = TelnetFilter()
        self._parser = BatClientParser()
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        context: ssl.SSLContext | None = None
        if self.use_tls:
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

        log.info("connecting to %s:%s (tls=%s)", self.host, self.port, self.use_tls)
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port, ssl=context
        )
        self._telnet = TelnetFilter()
        self._parser = BatClientParser()

        if self.enable_batclient:
            # Consumed by the server's out-of-band handler, so it is safe to
            # send before the login menu has even arrived.
            await self.send_raw(BC_ENABLE)

    async def send_raw(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("not connected")
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def send(self, text: str) -> None:
        """Send a single command line."""
        await self.send_raw(encode_command(text))

    async def events(self) -> AsyncIterator[Event]:
        """Yield events until the connection closes."""
        if self._reader is None:
            raise ConnectionError("not connected")
        reason = ""
        try:
            while True:
                data = await self._reader.read(self.read_size)
                if not data:
                    break
                for event in self.feed(data):
                    yield event
                if self._telnet.pending_replies:
                    replies = bytes(self._telnet.pending_replies)
                    self._telnet.pending_replies.clear()
                    await self.send_raw(replies)
        except (OSError, ssl.SSLError, asyncio.IncompleteReadError) as error:
            reason = str(error) or type(error).__name__
            log.warning("connection error: %s", reason)
        for event in self._parser.flush():
            yield event
        yield Disconnected(reason)

    def feed(self, data: bytes) -> list[Event]:
        """Turn raw bytes into events. Safe to call with any chunking."""
        events: list[Event] = []
        for item in self._telnet.feed(data):
            if item is Marker.GO_AHEAD:
                events.extend(self._parser.go_ahead())
            else:
                events.extend(self._parser.feed(item))
        return events

    async def close(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is None or writer.is_closing():
            return
        writer.close()
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()
