from __future__ import annotations

import os
import tempfile
from io import IOBase
from typing import Optional, Protocol, runtime_checkable

from ..streams import EndianBinaryReader, EndianBinaryWriter


@runtime_checkable
class Replacer(Protocol):
    size: int

    def __len__(self) -> int: ...

    def iter_chunks(self, chunk_size: int = 1024 * 1024): ...

    def write_to(self, writer: EndianBinaryWriter, chunk_size: int = 1024 * 1024) -> None: ...

    def read_bytes(self) -> bytes: ...

    def cleanup(self) -> None: ...


class BytesReplacer:
    __slots__ = ("data", "size")

    def __init__(self, data: bytes | bytearray | memoryview):
        self.data = data if isinstance(data, bytes) else bytes(data)
        self.size = len(self.data)

    def __len__(self) -> int:
        return self.size

    def iter_chunks(self, chunk_size: int = 1024 * 1024):
        view = memoryview(self.data)
        position = 0
        while position < len(view):
            next_position = min(position + chunk_size, len(view))
            yield view[position:next_position]
            position = next_position

    def write_to(self, writer: EndianBinaryWriter, chunk_size: int = 1024 * 1024) -> None:
        for chunk in self.iter_chunks(chunk_size):
            writer.write(chunk)

    def read_bytes(self) -> bytes:
        return self.data

    def cleanup(self) -> None:
        return None


class SourceSliceReplacer:
    __slots__ = ("_memory", "_stream", "_offset", "size", "_keepalive")

    def __init__(
        self,
        *,
        size: int,
        memory: Optional[memoryview] = None,
        stream: Optional[IOBase] = None,
        offset: int = 0,
        keepalive: object | None = None,
    ):
        self._memory = memory
        self._stream = stream
        self._offset = int(offset)
        self.size = int(size)
        self._keepalive = keepalive

    @classmethod
    def from_reader(
        cls,
        reader: EndianBinaryReader,
        offset: int,
        size: int,
    ) -> SourceSliceReplacer:
        if hasattr(reader, "view"):
            return cls(
                size=size,
                memory=reader.view,
                offset=offset,
                keepalive=reader,
            )
        return cls(
            size=size,
            stream=reader.stream,
            offset=reader.BaseOffset + offset,
            keepalive=reader,
        )

    @classmethod
    def from_writer(
        cls,
        writer: EndianBinaryWriter,
        offset: int = 0,
        size: Optional[int] = None,
    ) -> SourceSliceReplacer:
        length = writer.Length if size is None else int(size)
        return cls(
            size=length,
            stream=writer.stream,
            offset=offset,
            keepalive=writer,
        )

    def __len__(self) -> int:
        return self.size

    def iter_chunks(self, chunk_size: int = 1024 * 1024):
        if self._memory is not None:
            end = self._offset + self.size
            position = self._offset
            while position < end:
                next_position = min(position + chunk_size, end)
                yield self._memory[position:next_position]
                position = next_position
            return

        assert self._stream is not None
        stream = self._stream
        last_position = stream.tell()
        try:
            stream.seek(self._offset)
            remaining = self.size
            while remaining > 0:
                chunk = stream.read(min(chunk_size, remaining))
                if not chunk:
                    raise EOFError("Unexpected EOF while streaming source slice")
                yield chunk
                remaining -= len(chunk)
        finally:
            stream.seek(last_position)

    def write_to(self, writer: EndianBinaryWriter, chunk_size: int = 1024 * 1024) -> None:
        for chunk in self.iter_chunks(chunk_size):
            writer.write(chunk)

    def read_bytes(self) -> bytes:
        if self._memory is not None:
            return self._memory[self._offset : self._offset + self.size].tobytes()

        assert self._stream is not None
        stream = self._stream
        last_position = stream.tell()
        try:
            stream.seek(self._offset)
            return stream.read(self.size)
        finally:
            stream.seek(last_position)

    def cleanup(self) -> None:
        return None


class TempFileReplacer:
    __slots__ = ("path", "_offset", "size", "_delete_on_cleanup")

    def __init__(
        self,
        path: str,
        size: int,
        *,
        offset: int = 0,
        delete_on_cleanup: bool = False,
    ):
        self.path = path
        self._offset = int(offset)
        self.size = int(size)
        self._delete_on_cleanup = bool(delete_on_cleanup)

    def __len__(self) -> int:
        return self.size

    def iter_chunks(self, chunk_size: int = 1024 * 1024):
        with open(self.path, "rb") as source:
            source.seek(self._offset)
            remaining = self.size
            while remaining > 0:
                chunk = source.read(min(chunk_size, remaining))
                if not chunk:
                    raise EOFError("Unexpected EOF while streaming temp-file replacer")
                yield chunk
                remaining -= len(chunk)

    def write_to(self, writer: EndianBinaryWriter, chunk_size: int = 1024 * 1024) -> None:
        for chunk in self.iter_chunks(chunk_size):
            writer.write(chunk)

    def read_bytes(self) -> bytes:
        with open(self.path, "rb") as source:
            source.seek(self._offset)
            return source.read(self.size)

    def cleanup(self) -> None:
        if self._delete_on_cleanup:
            try:
                if os.path.isfile(self.path):
                    os.remove(self.path)
            except OSError:
                pass

    def __del__(self) -> None:
        self.cleanup()


class _AppendSegmentIO(IOBase):
    def __init__(self, store: AppendOnlySpillStore):
        super().__init__()
        self._store = store
        self._start = store._reserve_segment()
        self._position = 0
        self._length = 0
        self._closed = False

    @property
    def start(self) -> int:
        return self._start

    @property
    def length(self) -> int:
        return self._length

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            new_position = offset
        elif whence == os.SEEK_CUR:
            new_position = self._position + offset
        elif whence == os.SEEK_END:
            new_position = self._length + offset
        else:
            raise ValueError("Invalid whence value")
        if new_position < 0:
            raise ValueError("Negative seek position")
        self._position = new_position
        return self._position

    def write(self, b: bytes | bytearray | memoryview) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed spill segment")
        if not b:
            return 0
        stream = self._store._stream
        stream.seek(self._start + self._position)
        written = stream.write(b)
        self._position += written
        if self._position > self._length:
            self._length = self._position
        return written

    def flush(self) -> None:
        self._store._stream.flush()

    def close(self) -> None:
        if not self._closed:
            self.flush()
            self._closed = True
        super().close()


class SpillStoreSliceReplacer:
    __slots__ = ("_store", "_offset", "size")

    def __init__(self, store: AppendOnlySpillStore, offset: int, size: int):
        self._store = store
        self._offset = int(offset)
        self.size = int(size)

    def __len__(self) -> int:
        return self.size

    def iter_chunks(self, chunk_size: int = 1024 * 1024):
        last_position = self._store._stream.tell()
        try:
            self._store._stream.seek(self._offset)
            remaining = self.size
            while remaining > 0:
                chunk = self._store._stream.read(min(chunk_size, remaining))
                if not chunk:
                    raise EOFError("Unexpected EOF while streaming spill-store slice")
                yield chunk
                remaining -= len(chunk)
        finally:
            self._store._stream.seek(last_position)

    def write_to(self, writer: EndianBinaryWriter, chunk_size: int = 1024 * 1024) -> None:
        for chunk in self.iter_chunks(chunk_size):
            writer.write(chunk)

    def read_bytes(self) -> bytes:
        return self._store.read_range(self._offset, self.size)

    def cleanup(self) -> None:
        return None


class AppendOnlySpillStore:
    __slots__ = ("path", "_stream", "_closed")

    def __init__(
        self,
        *,
        prefix: str = "unitypy_spill_",
        suffix: str = ".bin",
        dir: Optional[str] = None,
    ):
        fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=dir)
        self.path = path
        self._stream = os.fdopen(fd, "w+b")
        self._closed = False

    def _reserve_segment(self) -> int:
        self._stream.seek(0, os.SEEK_END)
        return self._stream.tell()

    def create_writer(self, endian: str = ">") -> tuple[EndianBinaryWriter, _AppendSegmentIO]:
        segment = _AppendSegmentIO(self)
        return EndianBinaryWriter(segment, endian=endian), segment

    def slice(self, offset: int, size: int) -> SpillStoreSliceReplacer:
        return SpillStoreSliceReplacer(self, offset, size)

    def append_bytes(self, data: bytes | bytearray | memoryview) -> SpillStoreSliceReplacer:
        writer, segment = self.create_writer()
        try:
            writer.write(data)
        finally:
            writer.dispose()
        return self.slice(segment.start, segment.length)

    def read_range(self, offset: int, size: int) -> bytes:
        last_position = self._stream.tell()
        try:
            self._stream.seek(offset)
            return self._stream.read(size)
        finally:
            self._stream.seek(last_position)

    def copy_range_to(
        self,
        writer: EndianBinaryWriter,
        offset: int,
        size: int,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        last_position = self._stream.tell()
        try:
            self._stream.seek(offset)
            remaining = int(size)
            while remaining > 0:
                chunk = self._stream.read(min(chunk_size, remaining))
                if not chunk:
                    raise EOFError("Unexpected EOF while streaming spill-store slice")
                writer.write(chunk)
                remaining -= len(chunk)
        finally:
            self._stream.seek(last_position)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close()
        finally:
            try:
                if os.path.isfile(self.path):
                    os.remove(self.path)
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()


__all__ = [
    "AppendOnlySpillStore",
    "BytesReplacer",
    "Replacer",
    "SourceSliceReplacer",
    "SpillStoreSliceReplacer",
    "TempFileReplacer",
]
