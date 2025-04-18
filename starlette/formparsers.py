from __future__ import annotations

import typing
from dataclasses import dataclass, field
from enum import Enum
from tempfile import SpooledTemporaryFile
from urllib.parse import unquote_plus

from multipart import MultipartSegment, PushMultipartParser, parse_options_header
from urllib.parse import parse_qsl

from starlette.datastructures import FormData, Headers, UploadFile

if typing.TYPE_CHECKING:
    import python_multipart as multipart
    from python_multipart.multipart import QuerystringCallbacks
else:
    try:
        try:
            import python_multipart as multipart
            from python_multipart.multipart import parse_options_header
        except ModuleNotFoundError:  # pragma: no cover
            import multipart
            from multipart.multipart import parse_options_header
    except ModuleNotFoundError:  # pragma: no cover
        multipart = None
        parse_options_header = None


class FormMessage(Enum):
    FIELD_START = 1
    FIELD_NAME = 2
    FIELD_DATA = 3
    FIELD_END = 4
    END = 5


@dataclass
class MultipartPart:
    content_disposition: bytes | None = None
    field_name: str = ""
    data: bytearray = field(default_factory=bytearray)
    file: UploadFile | None = None
    item_headers: list[tuple[bytes, bytes]] = field(default_factory=list)


def _user_safe_decode(src: bytes | bytearray, codec: str) -> str:
    try:
        return src.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return src.decode("latin-1")


class MultiPartException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class FormParser:
    def __init__(self, headers: Headers, stream: typing.AsyncGenerator[bytes, None]) -> None:
        assert multipart is not None, "The `python-multipart` library must be installed to use form parsing."
        self.headers = headers
        self.stream = stream
        self.messages: list[tuple[FormMessage, bytes]] = []

    def on_field_start(self) -> None:
        message = (FormMessage.FIELD_START, b"")
        self.messages.append(message)

    def on_field_name(self, data: bytes, start: int, end: int) -> None:
        message = (FormMessage.FIELD_NAME, data[start:end])
        self.messages.append(message)

    def on_field_data(self, data: bytes, start: int, end: int) -> None:
        message = (FormMessage.FIELD_DATA, data[start:end])
        self.messages.append(message)

    def on_field_end(self) -> None:
        message = (FormMessage.FIELD_END, b"")
        self.messages.append(message)

    def on_end(self) -> None:
        message = (FormMessage.END, b"")
        self.messages.append(message)

    async def parse(self) -> FormData:
        # Callbacks dictionary.
        callbacks: QuerystringCallbacks = {
            "on_field_start": self.on_field_start,
            "on_field_name": self.on_field_name,
            "on_field_data": self.on_field_data,
            "on_field_end": self.on_field_end,
            "on_end": self.on_end,
        }

        # Create the parser.
        parser = multipart.QuerystringParser(callbacks)
        field_name = b""
        field_value = b""

        items: list[tuple[str, str | UploadFile]] = []

        # Feed the parser with data from the request.
        async for chunk in self.stream:
            if chunk:
                parser.write(chunk)
            else:
                parser.finalize()
            messages = list(self.messages)
            self.messages.clear()
            for message_type, message_bytes in messages:
                if message_type == FormMessage.FIELD_START:
                    field_name = b""
                    field_value = b""
                elif message_type == FormMessage.FIELD_NAME:
                    field_name += message_bytes
                elif message_type == FormMessage.FIELD_DATA:
                    field_value += message_bytes
                elif message_type == FormMessage.FIELD_END:
                    name = unquote_plus(field_name.decode("latin-1"))
                    value = unquote_plus(field_value.decode("latin-1"))
                    items.append((name, value))

        return FormData(items)


class MultiPartParser:
    spool_max_size = 1024 * 1024  # 1MB
    """The maximum size of the spooled temporary file used to store file data."""
    max_part_size = 1024 * 1024  # 1MB
    """The maximum size of a part in the multipart request."""

    def __init__(
        self,
        headers: Headers,
        stream: typing.AsyncGenerator[bytes, None],
        *,
        max_files: int | float = 1000,
        max_fields: int | float = 1000,
        max_part_size: int = 1024 * 1024,  # 1MB
    ) -> None:
        assert multipart is not None, "The `python-multipart` library must be installed to use form parsing."
        self.headers = headers
        self.stream = stream
        self.max_files = max_files
        self.max_fields = max_fields
        self.items: list[tuple[str, str | UploadFile]] = []
        self._current_files = 0
        self._current_fields = 0
        self._charset = ""
        self._files_to_close_on_error: list[SpooledTemporaryFile[bytes]] = []
        self.max_part_size = max_part_size

    async def on_part_begin(self, result: MultipartSegment)->MultipartPart:
        result_charset = result.charset or self._charset

        current_part = MultipartPart()
        for header, value in result.headerlist:
            parsed_header = header.lower().encode(result_charset)
            parsed_value = value.encode(result_charset)
            if parsed_header == b"content-disposition":
                current_part.content_disposition = parsed_value
            current_part.item_headers.append((parsed_header, parsed_value))
        await self.on_headers_finished(current_part)
        
        return current_part

    async def on_part_data(self, part: MultipartPart, data: bytes) -> None:
        if part.file is None:
            if len(part.data) + len(data) > self.max_part_size:
                raise MultiPartException(
                    f"Part exceeded maximum size of {int(self.max_part_size / 1024)}KB."
                )
            part.data.extend(data)
        else:
            await part.file.write(data)

    async def on_part_end(self, part: MultipartPart)->MultipartPart:
        if part.file is None:
            self.items.append(
                (
                    part.field_name,
                    _user_safe_decode(part.data, self._charset),
                )
            )
        else:
            await part.file.seek(0)
            self.items.append((part.field_name, part.file))
 
    async def on_headers_finished(self, current_part: MultipartPart) -> None:
        disposition, options = parse_options_header(current_part.content_disposition)
        try:
            current_part.field_name = _user_safe_decode(options[b"name"], self._charset)
        except KeyError:
            raise MultiPartException('The Content-Disposition header field "name" must be provided.')
        if b"filename" in options:
            self._current_files += 1
            if self._current_files > self.max_files:
                raise MultiPartException(f"Too many files. Maximum number of files is {self.max_files}.")
            filename = _user_safe_decode(options[b"filename"], self._charset)
            tempfile = SpooledTemporaryFile(max_size=self.spool_max_size)
            self._files_to_close_on_error.append(tempfile)
            current_part.file = UploadFile(
                file=tempfile,  # type: ignore[arg-type]
                size=0,
                filename=filename,
                headers=Headers(raw=current_part.item_headers),
            )
        else:
            self._current_fields += 1
            if self._current_fields > self.max_fields:
                raise MultiPartException(f"Too many fields. Maximum number of fields is {self.max_fields}.")
            current_part.file = None

    async def parse(self) -> FormData:
        # Parse the Content-Type header to get the multipart boundary.
        _, params = parse_options_header(self.headers["Content-Type"])
        charset = params.get(b"charset", "utf-8")
        if isinstance(charset, bytes):
            charset = charset.decode("latin-1")
        self._charset = charset
        try:
            boundary = params[b"boundary"]
        except KeyError:
            raise MultiPartException("Missing boundary in multipart.")

        try:
            return await self.parse_internal(boundary)
        except MultiPartException as exc:
            for file in self._files_to_close_on_error:
                file.close()
            raise exc

    async def parse_internal(self, boundary: str) -> FormData:
        current_part = None
        with PushMultipartParser(
            boundary,
            header_charset=self._charset,
        ) as parser:
            while not parser.closed:
                chunk = await self.stream.__anext__()
                for result in parser.parse(chunk):
                    if isinstance(result, MultipartSegment):
                        current_part = await self.on_part_begin(result)
                    elif result:  # Non-empty bytearray
                        await self.on_part_data(current_part, result)
                    else:  # Segment End
                        await self.on_part_end(current_part)

        return FormData(self.items)
