"""
VH Stream - Lazy loading and async API for .vh files.

VHStream loads only the index (metadata + frame list) on open.
Frame data is fetched on demand, never loaded into memory in bulk.

Usage:
    # Lazy iteration (low memory)
    stream = VHStream('video.vh')
    for fid, data in stream.iter_frames(start=100, end=200):
        process(data)

    # Async pipeline for AI
    async for fid, data in stream.async_iter_frames():
        result = await model(data)

    # Prefetch (read-ahead buffer)
    stream = VHStream('video.vh', prefetch=8)
    for fid, data in stream.iter_frames():
        process(data)  # next frames already loading in background
"""

import sqlite3
import json
import threading
import queue
from pathlib import Path
from typing import Optional, Iterator, Tuple


def _read_frame(conn, frame_id, version):
    """Read a single frame from a SQLite connection (thread-safe helper)."""
    if version == 1:
        row = conn.execute(
            "SELECT image_data FROM frames WHERE frame_id = ?", (frame_id,)
        ).fetchone()
        return row[0] if row else None

    row = conn.execute(
        "SELECT frame_type, ref_frame_id, image_data FROM frames WHERE frame_id = ?",
        (frame_id,)
    ).fetchone()
    if not row:
        return None
    ftype, ref_id, data = row
    if ftype == 'full':
        return data
    elif ftype == 'ref':
        return _read_frame(conn, ref_id, version)
    # delta frames: return the keyframe as fallback (no PIL in prefetch)
    elif ftype == 'delta':
        ref_row = conn.execute(
            "SELECT image_data FROM frames WHERE frame_id = ?", (ref_id,)
        ).fetchone()
        return ref_row[0] if ref_row else None
    return None


class VHStream:
    """Lazy/streaming reader for .vh files. Loads index only, frames on demand."""

    def __init__(self, path: str, prefetch: int = 0):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"File not found: {self.path}")

        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-32000")
        self._prefetch = prefetch

        # Load index only (lightweight)
        self._meta = {}
        for k, v in self._conn.execute("SELECT key, value FROM metadata").fetchall():
            self._meta[k] = json.loads(v)

        self._frame_count = self._conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]

        # Check version
        cols = [c[1] for c in self._conn.execute("PRAGMA table_info(frames)").fetchall()]
        self._version = 2 if 'frame_type' in cols else 1

        # Load frame index (id + type + ref only — no blob data)
        if self._version == 2:
            self._index = self._conn.execute(
                "SELECT frame_id, frame_type, ref_frame_id, timestamp_ms "
                "FROM frames ORDER BY frame_id"
            ).fetchall()
        else:
            self._index = self._conn.execute(
                "SELECT frame_id, 'full', NULL, timestamp_ms "
                "FROM frames ORDER BY frame_id"
            ).fetchall()

    @property
    def meta(self) -> dict:
        return dict(self._meta)

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def fps(self) -> float:
        return self._meta.get('fps', 24.0)

    @property
    def duration(self) -> float:
        return self._meta.get('duration_s', self._frame_count / self.fps)

    @property
    def resolution(self) -> Tuple[int, int]:
        return self._meta.get('width', 0), self._meta.get('height', 0)

    def get_frame_info(self, frame_id: int) -> Optional[dict]:
        """Get frame metadata without loading image data."""
        if frame_id < 0 or frame_id >= len(self._index):
            return None
        fid, ftype, ref_id, ts = self._index[frame_id]
        return {
            'frame_id': fid,
            'frame_type': ftype,
            'ref_frame_id': ref_id,
            'timestamp_ms': ts,
        }

    def get_frame_image(self, frame_id: int) -> Optional[bytes]:
        """Load a single frame's image data on demand."""
        if self._version == 1:
            row = self._conn.execute(
                "SELECT image_data FROM frames WHERE frame_id = ?", (frame_id,)
            ).fetchone()
            return row[0] if row else None

        row = self._conn.execute(
            "SELECT frame_type, ref_frame_id, image_data FROM frames WHERE frame_id = ?",
            (frame_id,)
        ).fetchone()
        if not row:
            return None

        ftype, ref_id, data = row
        if ftype == 'full':
            return data
        elif ftype == 'ref':
            return self.get_frame_image(ref_id)
        elif ftype == 'delta':
            # Delta requires imaging libs — import lazily
            import zlib
            import io
            import numpy as np
            from PIL import Image

            ref_data = self._conn.execute(
                "SELECT image_data FROM frames WHERE frame_id = ?", (ref_id,)
            ).fetchone()
            if not ref_data:
                return None
            ref_pixels = np.array(Image.open(io.BytesIO(ref_data[0])))
            w, h = self.resolution
            delta = np.frombuffer(
                zlib.decompress(data), dtype=np.uint8
            ).reshape(h, w, 3)
            result = np.bitwise_xor(ref_pixels, delta)
            img = Image.fromarray(result)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=85)
            return buf.getvalue()
        return None

    def iter_frames(self, start: int = 0, end: int = None,
                    step: int = 1) -> Iterator[Tuple[int, bytes]]:
        """Iterate frames lazily. With prefetch > 0, uses a background thread."""
        if end is None:
            end = self._frame_count - 1
        end = min(end, self._frame_count - 1)

        frame_ids = range(start, end + 1, step)

        if self._prefetch > 0:
            yield from self._iter_prefetch(frame_ids)
        else:
            for fid in frame_ids:
                data = self.get_frame_image(fid)
                if data is not None:
                    yield fid, data

    def _iter_prefetch(self, frame_ids) -> Iterator[Tuple[int, bytes]]:
        """Read-ahead iterator using a background thread with its own connection."""
        buf = queue.Queue(maxsize=self._prefetch)
        sentinel = object()
        db_path = str(self.path)
        version = self._version

        def producer():
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                for fid in frame_ids:
                    data = _read_frame(conn, fid, version)
                    if data is not None:
                        buf.put((fid, data))
            finally:
                conn.close()
            buf.put(sentinel)

        t = threading.Thread(target=producer, daemon=True)
        t.start()

        while True:
            item = buf.get()
            if item is sentinel:
                break
            yield item

        t.join(timeout=1)

    def frames_at_time(self, time_sec: float, window_sec: float = 0) -> list:
        """Get frame(s) at a specific time (seconds)."""
        target_ms = time_sec * 1000
        if window_sec <= 0:
            # Find closest frame
            row = self._conn.execute(
                "SELECT frame_id FROM frames ORDER BY ABS(timestamp_ms - ?) LIMIT 1",
                (target_ms,)
            ).fetchone()
            return [row[0]] if row else []
        else:
            start_ms = (time_sec - window_sec / 2) * 1000
            end_ms = (time_sec + window_sec / 2) * 1000
            rows = self._conn.execute(
                "SELECT frame_id FROM frames WHERE timestamp_ms BETWEEN ? AND ?",
                (start_ms, end_ms)
            ).fetchall()
            return [r[0] for r in rows]

    def sample_frames(self, n: int = 10) -> list:
        """Get N evenly-spaced frame IDs for sampling."""
        if n >= self._frame_count:
            return list(range(self._frame_count))
        step = max(1, self._frame_count // n)
        return list(range(0, self._frame_count, step))[:n]

    # --- Async API ---

    async def async_iter_frames(self, start: int = 0, end: int = None,
                                step: int = 1):
        """Async iterator for AI pipelines. Runs I/O in thread pool."""
        import asyncio

        if end is None:
            end = self._frame_count - 1
        end = min(end, self._frame_count - 1)
        loop = asyncio.get_event_loop()

        for fid in range(start, end + 1, step):
            data = await loop.run_in_executor(None, self.get_frame_image, fid)
            if data is not None:
                yield fid, data

    async def async_get_frame(self, frame_id: int) -> Optional[bytes]:
        """Async single frame fetch."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_frame_image, frame_id)

    def close(self):
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            pass
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __len__(self):
        return self._frame_count

    def __getitem__(self, key):
        """Support vh[frame_id] and vh[start:end] syntax."""
        if isinstance(key, slice):
            start = key.start or 0
            stop = key.stop or self._frame_count
            step = key.step or 1
            return list(self.iter_frames(start, stop - 1, step))
        else:
            data = self.get_frame_image(key)
            if data is None:
                raise IndexError(f"Frame {key} not found")
            return data
