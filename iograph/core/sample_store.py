"""Lossless disk-backed samples with bounded-memory, immutable render views.

Sealed records never change. Only the last (coalesced idle) sample stays mutable
in memory. A snapshot captures that tail and a length, so GUI appends/resets can
run alongside a renderer without copying the recording or changing its input.
The temporary file closes/deletes when the last store/snapshot reference dies.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from struct import Struct
from tempfile import TemporaryFile
from threading import RLock
from weakref import finalize

Sample = tuple[float | None, float | None, int]
_RECORD = Struct("<ddqB")  # doubles preserve source coordinates; int64 elapsed ms
_BATCH = 4096


def _pack(sample: Sample) -> bytes:
    x, y, t = sample
    return _RECORD.pack(0.0 if x is None else x, 0.0 if y is None else y, t, x is None or y is None)


def _unpack(record) -> Sample:
    x, y, t, idle = record
    return (None, None, t) if idle else (x, y, t)


def _close_file(file):
    try:
        file.close()
    except OSError:
        pass  # A failed final flush must still allow the handle to close.


class SampleSnapshot(Sequence):
    def __init__(self, owner: SampleStore, count: int, tail: Sample | None):
        self._owner = owner
        self._count = count
        self._tail = tail

    def __len__(self) -> int:
        return self._count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(index)
        if index == self._count - 1:
            return self._tail
        return self._owner._read(index)

    def packed_chunks(self) -> Iterator[bytes]:
        # At most 100 KiB per reader, regardless of recording duration.
        for start in range(0, max(0, self._count - 1), _BATCH):
            count = min(_BATCH, self._count - 1 - start)
            with self._owner._lock:
                self._owner._file.seek(start * _RECORD.size)
                data = self._owner._file.read(count * _RECORD.size)
            if len(data) != count * _RECORD.size:
                raise OSError("Incomplete sample storage")
            yield data
        if self._count:
            yield _pack(self._tail)

    def __iter__(self) -> Iterator[Sample]:
        for data in self.packed_chunks():
            for record in _RECORD.iter_unpack(data):
                yield _unpack(record)

    def __eq__(self, other):
        if not isinstance(other, Sequence):
            return NotImplemented
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))


class SampleStore(Sequence):
    def __init__(self):
        self._file = None  # Empty/idle-only sessions need no temporary file.
        self._lock = RLock()
        self._count = 0
        self._tail: Sample | None = None

    def __len__(self) -> int:
        return self._count

    def snapshot(self) -> SampleSnapshot:
        with self._lock:
            return SampleSnapshot(self, self._count, self._tail)

    def __getitem__(self, index):
        return self.snapshot()[index]

    def __iter__(self) -> Iterator[Sample]:
        return iter(self.snapshot())

    def __eq__(self, other):
        return self.snapshot() == other

    def _read(self, index: int) -> Sample:
        with self._lock:
            self._file.seek(index * _RECORD.size)
            data = self._file.read(_RECORD.size)
        if len(data) != _RECORD.size:
            raise OSError("Incomplete sample storage")
        return _unpack(_RECORD.unpack(data))

    def _ensure_file(self):
        if self._file is None:
            self._file = TemporaryFile(mode="w+b")
            # Snapshots keep the owner alive. Close explicitly only after its
            # final reference disappears, including during cyclic GC.
            finalize(self, _close_file, self._file)

    def append(self, sample: Sample) -> None:
        # Validate/pack before changing the file or the recording length.
        _pack(sample)
        with self._lock:
            if self._count:
                self._ensure_file()
                offset = (self._count - 1) * _RECORD.size
                if self._file.tell() != offset:
                    self._file.seek(offset)
                self._file.write(_pack(self._tail))
            self._tail = sample
            self._count += 1

    def extend(self, samples: Iterable[Sample]) -> None:
        if isinstance(samples, SampleStore):
            samples = samples.snapshot()
        if isinstance(samples, SampleSnapshot):
            for data in samples.packed_chunks():
                with self._lock:
                    self._ensure_file()
                    offset = max(0, self._count - 1) * _RECORD.size
                    self._file.seek(offset)
                    if self._count:
                        self._file.write(_pack(self._tail))
                    self._file.write(data[:-_RECORD.size])
                    self._tail = _unpack(_RECORD.unpack(data[-_RECORD.size:]))
                    self._count += len(data) // _RECORD.size
            return
        for sample in samples:
            self.append(sample)

    def __setitem__(self, index: int, sample: Sample) -> None:
        if index not in (-1, self._count - 1) or not self._count:
            raise IndexError("Only the current tail may change")
        _pack(sample)
        with self._lock:
            self._tail = sample


class RawRecording(Sequence):
    """Validated import rows as a lazy mapping view over the same packed format."""
    def __init__(self):
        self.samples = SampleStore()
        self.segment_starts: set[int] = set()
        self.has_position = False

    def __len__(self):
        return len(self.samples)

    def append(self, row: dict) -> None:
        if row.get("segment_start"):
            self.segment_starts.add(len(self))
        self.samples.append((row["x"], row["y"], row["t"]))
        self.has_position |= row["x"] is not None

    def __iter__(self):
        for i, (x, y, t) in enumerate(self.samples):
            yield {"x": x, "y": y, "t": t, "segment_start": i in self.segment_starts}

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        x, y, t = self.samples[index]
        return {"x": x, "y": y, "t": t, "segment_start": index in self.segment_starts}
