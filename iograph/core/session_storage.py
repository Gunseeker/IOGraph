from __future__ import annotations

from pathlib import Path
import json
import os
from collections.abc import Iterable
from tempfile import NamedTemporaryFile
from uuid import uuid4

from PyQt6.QtCore import QStandardPaths


class SessionStorage:
    def __init__(self, session_state_file: str, session_chunk_ms: int) -> None:
        self._session_state_file = session_state_file
        self._session_chunk_ms = int(session_chunk_ms)

    def session_state_path(self) -> Path:
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        if not base:
            return Path.home() / ".iograph" / self._session_state_file
        return Path(base) / self._session_state_file

    def preview_cache_path(self) -> Path:
        return self.session_state_path().with_name("preview_cache.png")

    def desktop_cache_path(self) -> Path:
        return self.session_state_path().with_name("desktop_cache.png")

    def raw_chunks_dir(self) -> Path:
        return self.session_state_path().with_name("raw_chunks")

    def save_state(self, state: dict) -> None:
        path = self.session_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                    prefix=path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(state, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        # Only remove obsolete chunks after the new manifest is committed.
        storage = state.get("raw_storage", {})
        if storage.get("format") == "ndjson-chunks":
            keep = {chunk["file"] for chunk in storage.get("chunks", [])}
            for old in self.raw_chunks_dir().glob("*.ndjson"):
                if old.name not in keep:
                    try:
                        old.unlink()
                    except OSError:
                        pass

    def load_state(self) -> dict | None:
        path = self.session_state_path()
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Invalid session manifest")
        return payload

    def clear_state_files(self) -> None:
        path = self.session_state_path()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        preview_cache = self.preview_cache_path()
        if preview_cache.exists():
            try:
                preview_cache.unlink()
            except OSError:
                pass
        desktop_cache = self.desktop_cache_path()
        if desktop_cache.exists():
            try:
                desktop_cache.unlink()
            except OSError:
                pass
        chunks_dir = self.raw_chunks_dir()
        if chunks_dir.exists() and chunks_dir.is_dir():
            for f in chunks_dir.glob("*.ndjson"):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                chunks_dir.rmdir()
            except OSError:
                pass

    def write_raw_chunks(self, rows: Iterable[dict]) -> dict:
        chunks_dir = self.raw_chunks_dir()
        chunks_dir.mkdir(parents=True, exist_ok=True)
        generation = uuid4().hex
        index: list[dict] = []
        output = None
        last_key = None

        def finish_chunk():
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
                output.close()

        try:
            for row in rows:
                t = int(row.get("t", 0))
                key = max(0, t // self._session_chunk_ms)
                if last_key is None or key != last_key:
                    finish_chunk()
                    name = f"chunk_{generation}_{len(index):06d}_{key:06d}.ndjson"
                    output = (chunks_dir / name).open("w", encoding="utf-8")
                    index.append({"file": name, "first_t": t, "last_t": t, "count": 0})
                    last_key = key
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                index[-1]["last_t"] = t
                index[-1]["count"] += 1
            finish_chunk()
        except BaseException:
            if output is not None:
                output.close()
            for chunk in index:
                (chunks_dir / chunk["file"]).unlink(missing_ok=True)
            raise
        return {"format": "ndjson-chunks", "chunk_ms": self._session_chunk_ms, "chunks": index}

    def read_raw_samples(self, payload: dict) -> list[dict]:
        return list(self.iter_raw_samples(payload))

    def iter_raw_samples(self, payload: dict):
        storage = payload.get("raw_storage")
        if isinstance(storage, dict) and storage.get("format") == "ndjson-chunks":
            chunks = storage.get("chunks", [])
            if not isinstance(chunks, list):
                raise ValueError("Invalid chunk index")
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    raise ValueError("Invalid chunk entry")
                name = chunk.get("file")
                if not isinstance(name, str) or Path(name).name != name:
                    raise ValueError("Invalid raw chunk path")
                path = self.raw_chunks_dir() / name
                count = 0
                with path.open("r", encoding="utf-8") as source:
                    for line in source:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("Invalid raw sample")
                        count += 1
                        yield row
                if "count" in chunk and count != chunk["count"]:
                    raise ValueError("Incomplete raw chunk")
            return

        if storage is not None:
            raise ValueError("Unsupported raw storage")

        inline = payload.get("raw_samples", [])
        if isinstance(inline, list):
            yield from (r for r in inline if isinstance(r, dict))
