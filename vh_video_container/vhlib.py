"""
VH Format - Video container optimized for AI workloads.

v1: Each frame stored independently (JPEG/WebP)
v2: Deduplication + delta compression + WebP keyframes
    - 'full'  : complete image (keyframe)
    - 'ref'   : pointer to identical frame (0 bytes)
    - 'delta' : XOR diff vs keyframe, compressed with zlib
"""

import sqlite3
import json
import zlib
import io
from pathlib import Path
from typing import Optional, Any

try:
    import numpy as np
    from PIL import Image
    HAS_IMAGING = True
except ImportError:
    HAS_IMAGING = False


class VHFile:
    """Read and write .vh video files."""

    def __init__(self, path: str, mode: str = 'r'):
        self.path = Path(path)
        self.mode = mode
        self._version = None
        self._keyframe_cache = {}
        self._width = None
        self._height = None

        if mode == 'w':
            if self.path.exists():
                self.path.unlink()
            self._conn = sqlite3.connect(str(self.path))
            self._setup_pragmas()
            self._init_schema()
        elif mode in ('r', 'a'):
            if not self.path.exists():
                raise FileNotFoundError(f"File not found: {self.path}")
            self._conn = sqlite3.connect(str(self.path))
            self._setup_pragmas()
        else:
            raise ValueError(f"Invalid mode: {mode}")

    def _setup_pragmas(self):
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-64000")

    @property
    def version(self) -> int:
        if self._version is None:
            cols = [c[1] for c in self._conn.execute("PRAGMA table_info(frames)").fetchall()]
            self._version = 2 if 'frame_type' in cols else 1
        return self._version

    def _get_dimensions(self):
        if self._width is None:
            self._width = self.get_meta('width', 0)
            self._height = self.get_meta('height', 0)
        return self._width, self._height

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE frames (
                frame_id INTEGER PRIMARY KEY,
                timestamp_ms REAL NOT NULL,
                frame_type TEXT NOT NULL DEFAULT 'full',
                ref_frame_id INTEGER DEFAULT NULL,
                image_format TEXT NOT NULL DEFAULT 'webp',
                image_data BLOB,
                width INTEGER,
                height INTEGER,
                size_bytes INTEGER
            );

            CREATE TABLE audio (
                track_id INTEGER PRIMARY KEY AUTOINCREMENT,
                codec TEXT NOT NULL,
                sample_rate INTEGER,
                channels INTEGER,
                duration_ms REAL,
                data BLOB NOT NULL
            );

            CREATE TABLE annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                frame_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                FOREIGN KEY (frame_id) REFERENCES frames(frame_id)
            );

            CREATE TABLE thumbnails (
                frame_id INTEGER PRIMARY KEY,
                image_data BLOB NOT NULL,
                width INTEGER,
                height INTEGER,
                size_bytes INTEGER,
                FOREIGN KEY (frame_id) REFERENCES frames(frame_id)
            );

            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                frame_id INTEGER NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector BLOB NOT NULL,
                FOREIGN KEY (frame_id) REFERENCES frames(frame_id)
            );

            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                frame_id INTEGER,
                filename TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                description TEXT,
                data BLOB NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (frame_id) REFERENCES frames(frame_id)
            );

            CREATE INDEX idx_annotations_frame ON annotations(frame_id);
            CREATE INDEX idx_annotations_key ON annotations(key);
            CREATE INDEX idx_frames_timestamp ON frames(timestamp_ms);
            CREATE INDEX idx_frames_type ON frames(frame_type);
            CREATE UNIQUE INDEX idx_embeddings_frame_model ON embeddings(frame_id, model);
            CREATE INDEX idx_documents_frame ON documents(frame_id);
        """)
        self._version = 2
        self._conn.commit()

    # --- Metadata ---

    def set_meta(self, key: str, value: Any):
        self._conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
            (key, json.dumps(value))
        )

    def get_meta(self, key: str, default=None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def get_all_meta(self) -> dict:
        rows = self._conn.execute("SELECT key, value FROM metadata").fetchall()
        return {k: json.loads(v) for k, v in rows}

    # --- Frames: Write ---

    def add_frame(self, frame_id: int, timestamp_ms: float, image_data: bytes,
                  image_format: str = 'webp', width: int = 0, height: int = 0):
        if self.version == 2:
            self._conn.execute(
                "INSERT INTO frames (frame_id, timestamp_ms, frame_type, ref_frame_id, "
                "image_format, image_data, width, height, size_bytes) "
                "VALUES (?, ?, 'full', NULL, ?, ?, ?, ?, ?)",
                (frame_id, timestamp_ms, image_format, image_data, width, height, len(image_data))
            )
        else:
            self._conn.execute(
                "INSERT INTO frames (frame_id, timestamp_ms, image_format, image_data, "
                "width, height, size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (frame_id, timestamp_ms, image_format, image_data, width, height, len(image_data))
            )

    def add_frame_ref(self, frame_id: int, timestamp_ms: float, ref_frame_id: int,
                      width: int = 0, height: int = 0):
        self._conn.execute(
            "INSERT INTO frames (frame_id, timestamp_ms, frame_type, ref_frame_id, "
            "image_format, image_data, width, height, size_bytes) "
            "VALUES (?, ?, 'ref', ?, 'ref', NULL, ?, ?, 0)",
            (frame_id, timestamp_ms, ref_frame_id, width, height)
        )

    def add_frame_delta(self, frame_id: int, timestamp_ms: float, ref_frame_id: int,
                        delta_data: bytes, width: int = 0, height: int = 0):
        self._conn.execute(
            "INSERT INTO frames (frame_id, timestamp_ms, frame_type, ref_frame_id, "
            "image_format, image_data, width, height, size_bytes) "
            "VALUES (?, ?, 'delta', ?, 'xor_zlib', ?, ?, ?, ?)",
            (frame_id, timestamp_ms, ref_frame_id, delta_data, width, height, len(delta_data))
        )

    # --- Frames: Read ---

    def get_frame(self, frame_id: int) -> Optional[dict]:
        data = self.get_frame_image(frame_id)
        if data is None:
            return None
        if self.version == 2:
            row = self._conn.execute(
                "SELECT frame_id, timestamp_ms, width, height FROM frames WHERE frame_id = ?",
                (frame_id,)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT frame_id, timestamp_ms, width, height FROM frames WHERE frame_id = ?",
                (frame_id,)
            ).fetchone()
        if not row:
            return None
        fmt = self.get_meta('image_format', 'webp') if self.version == 2 else 'jpeg'
        return {
            'frame_id': row[0], 'timestamp_ms': row[1], 'format': fmt,
            'data': data, 'width': row[2], 'height': row[3], 'size_bytes': len(data)
        }

    def get_frame_image(self, frame_id: int) -> Optional[bytes]:
        if self.version == 1:
            row = self._conn.execute(
                "SELECT image_data FROM frames WHERE frame_id = ?", (frame_id,)
            ).fetchone()
            return row[0] if row else None

        # v2: handle frame types
        row = self._conn.execute(
            "SELECT frame_type, ref_frame_id, image_data FROM frames WHERE frame_id = ?",
            (frame_id,)
        ).fetchone()
        if not row:
            return None

        frame_type, ref_id, data = row

        if frame_type == 'full':
            return data
        elif frame_type == 'ref':
            return self.get_frame_image(ref_id)
        elif frame_type == 'delta':
            pixels = self._decode_delta(ref_id, data)
            img = Image.fromarray(pixels)
            buf = io.BytesIO()
            img.save(buf, format='WEBP', quality=85)
            return buf.getvalue()
        return None

    def get_frame_pixels(self, frame_id: int) -> Optional[Any]:
        """Get frame as numpy array (fast, avoids re-encoding for delta frames)."""
        if not HAS_IMAGING:
            raise RuntimeError("numpy and Pillow required for get_frame_pixels()")

        if self.version == 1:
            data = self.get_frame_image(frame_id)
            if data is None:
                return None
            return np.array(Image.open(io.BytesIO(data)))

        row = self._conn.execute(
            "SELECT frame_type, ref_frame_id, image_data FROM frames WHERE frame_id = ?",
            (frame_id,)
        ).fetchone()
        if not row:
            return None

        frame_type, ref_id, data = row

        if frame_type == 'full':
            return np.array(Image.open(io.BytesIO(data)))
        elif frame_type == 'ref':
            return self.get_frame_pixels(ref_id)
        elif frame_type == 'delta':
            return self._decode_delta(ref_id, data)
        return None

    def _get_keyframe_pixels(self, frame_id: int):
        """Get decoded keyframe pixels with caching."""
        if frame_id in self._keyframe_cache:
            return self._keyframe_cache[frame_id]

        data = self._conn.execute(
            "SELECT image_data FROM frames WHERE frame_id = ?", (frame_id,)
        ).fetchone()[0]

        pixels = np.array(Image.open(io.BytesIO(data)))
        self._keyframe_cache[frame_id] = pixels

        # Keep cache small
        if len(self._keyframe_cache) > 4:
            oldest = min(self._keyframe_cache.keys())
            del self._keyframe_cache[oldest]

        return pixels

    def _decode_delta(self, ref_frame_id: int, delta_data: bytes):
        """Decode a delta frame: XOR(keyframe_pixels, delta) = original pixels."""
        ref_pixels = self._get_keyframe_pixels(ref_frame_id)
        w, h = self._get_dimensions()
        delta = np.frombuffer(zlib.decompress(delta_data), dtype=np.uint8).reshape(h, w, 3)
        return np.bitwise_xor(ref_pixels, delta)

    def get_frames_range(self, start: int, end: int) -> list:
        rows = self._conn.execute(
            "SELECT frame_id, timestamp_ms, image_format, width, height, size_bytes "
            "FROM frames WHERE frame_id BETWEEN ? AND ?", (start, end)
        ).fetchall()
        return [{'frame_id': r[0], 'timestamp_ms': r[1], 'format': r[2],
                 'width': r[3], 'height': r[4], 'size_bytes': r[5]} for r in rows]

    def get_frames_by_time(self, start_ms: float, end_ms: float) -> list:
        rows = self._conn.execute(
            "SELECT frame_id, timestamp_ms, image_format, width, height, size_bytes "
            "FROM frames WHERE timestamp_ms BETWEEN ? AND ?", (start_ms, end_ms)
        ).fetchall()
        return [{'frame_id': r[0], 'timestamp_ms': r[1], 'format': r[2],
                 'width': r[3], 'height': r[4], 'size_bytes': r[5]} for r in rows]

    @property
    def frame_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]

    def get_frame_stats(self) -> dict:
        if self.version == 1:
            return {'full': self.frame_count, 'ref': 0, 'delta': 0}
        rows = self._conn.execute(
            "SELECT frame_type, COUNT(*), COALESCE(SUM(size_bytes), 0) "
            "FROM frames GROUP BY frame_type"
        ).fetchall()
        return {r[0]: {'count': r[1], 'bytes': r[2]} for r in rows}

    # --- Audio ---

    def add_audio(self, data: bytes, codec: str = 'opus',
                  sample_rate: int = 48000, channels: int = 2, duration_ms: float = 0):
        self._conn.execute(
            "INSERT INTO audio (codec, sample_rate, channels, duration_ms, data) VALUES (?, ?, ?, ?, ?)",
            (codec, sample_rate, channels, duration_ms, data)
        )

    def get_audio(self, track_id: int = 1) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT track_id, codec, sample_rate, channels, duration_ms, data "
            "FROM audio WHERE track_id = ?", (track_id,)
        ).fetchone()
        if not row:
            return None
        return {
            'track_id': row[0], 'codec': row[1], 'sample_rate': row[2],
            'channels': row[3], 'duration_ms': row[4], 'data': row[5]
        }

    def export_audio(self, output_path: str, track_id: int = 1) -> bool:
        audio = self.get_audio(track_id)
        if not audio:
            return False
        with open(output_path, 'wb') as f:
            f.write(audio['data'])
        return True

    # --- Annotations ---

    def annotate(self, frame_id: int, key: str, value: Any):
        # Upsert: if same frame+key exists, update it; otherwise insert
        existing = self._conn.execute(
            "SELECT id FROM annotations WHERE frame_id = ? AND key = ?",
            (frame_id, key)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE annotations SET value = ? WHERE id = ?",
                (json.dumps(value), existing[0])
            )
        else:
            self._conn.execute(
                "INSERT INTO annotations (frame_id, key, value) VALUES (?, ?, ?)",
                (frame_id, key, json.dumps(value))
            )

    def update_annotation(self, frame_id: int, key: str, value: Any) -> bool:
        """Update an existing annotation. Returns True if found and updated."""
        cur = self._conn.execute(
            "UPDATE annotations SET value = ? WHERE frame_id = ? AND key = ?",
            (json.dumps(value), frame_id, key)
        )
        return cur.rowcount > 0

    def delete_annotation(self, frame_id: int, key: str) -> bool:
        """Delete a specific annotation by frame and key. Returns True if deleted."""
        cur = self._conn.execute(
            "DELETE FROM annotations WHERE frame_id = ? AND key = ?",
            (frame_id, key)
        )
        return cur.rowcount > 0

    def delete_annotations(self, frame_id: int) -> int:
        """Delete all annotations for a frame. Returns count deleted."""
        cur = self._conn.execute(
            "DELETE FROM annotations WHERE frame_id = ?", (frame_id,)
        )
        return cur.rowcount

    def get_annotations(self, frame_id: int) -> dict:
        rows = self._conn.execute(
            "SELECT key, value FROM annotations WHERE frame_id = ?", (frame_id,)
        ).fetchall()
        return {k: json.loads(v) for k, v in rows}

    def search_annotations(self, key: str, value_contains: str = None) -> list:
        if value_contains:
            rows = self._conn.execute(
                "SELECT frame_id, key, value FROM annotations WHERE key = ? AND value LIKE ?",
                (key, f'%{value_contains}%')
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT frame_id, key, value FROM annotations WHERE key = ?", (key,)
            ).fetchall()
        return [{'frame_id': r[0], 'key': r[1], 'value': json.loads(r[2])} for r in rows]

    def search_frames_with_annotation(self, key: str) -> list:
        rows = self._conn.execute(
            "SELECT DISTINCT frame_id FROM annotations WHERE key = ? ORDER BY frame_id",
            (key,)
        ).fetchall()
        return [r[0] for r in rows]

    # --- Documents ---

    _DOCUMENTS_SCHEMA = """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            frame_id INTEGER,
            filename TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            description TEXT,
            data BLOB NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (frame_id) REFERENCES frames(frame_id)
        );
        CREATE INDEX IF NOT EXISTS idx_documents_frame ON documents(frame_id);
    """

    def _ensure_documents_table(self):
        self._ensure_table('documents', self._DOCUMENTS_SCHEMA)

    def add_document(self, filepath: str, frame_id: int = None,
                     description: str = None) -> int:
        """Attach a document to a frame (or globally if frame_id=None).
        Returns the document id."""
        self._ensure_documents_table()
        p = Path(filepath)
        data = p.read_bytes()
        mime = self._guess_mime(p.name)
        cur = self._conn.execute(
            "INSERT INTO documents (frame_id, filename, mime_type, description, data, size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (frame_id, p.name, mime, description, data, len(data))
        )
        return cur.lastrowid

    def add_document_bytes(self, filename: str, data: bytes,
                           frame_id: int = None, description: str = None,
                           mime_type: str = None) -> int:
        """Attach a document from raw bytes. Returns the document id."""
        self._ensure_documents_table()
        if mime_type is None:
            mime_type = self._guess_mime(filename)
        cur = self._conn.execute(
            "INSERT INTO documents (frame_id, filename, mime_type, description, data, size_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (frame_id, filename, mime_type, description, data, len(data))
        )
        return cur.lastrowid

    def get_document(self, doc_id: int) -> Optional[dict]:
        """Get a document by id. Returns dict with keys: id, frame_id, filename, mime_type, description, data, size_bytes, created_at."""
        self._ensure_documents_table()
        row = self._conn.execute(
            "SELECT id, frame_id, filename, mime_type, description, data, size_bytes, created_at "
            "FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if not row:
            return None
        return dict(zip(('id', 'frame_id', 'filename', 'mime_type',
                         'description', 'data', 'size_bytes', 'created_at'), row))

    def list_documents(self, frame_id: int = None) -> list:
        """List documents. If frame_id given, only for that frame. Returns list of dicts (without data blob)."""
        self._ensure_documents_table()
        if frame_id is not None:
            rows = self._conn.execute(
                "SELECT id, frame_id, filename, mime_type, description, size_bytes, created_at "
                "FROM documents WHERE frame_id = ? ORDER BY created_at", (frame_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, frame_id, filename, mime_type, description, size_bytes, created_at "
                "FROM documents ORDER BY frame_id, created_at"
            ).fetchall()
        return [dict(zip(('id', 'frame_id', 'filename', 'mime_type',
                          'description', 'size_bytes', 'created_at'), r)) for r in rows]

    def export_document(self, doc_id: int, output_path: str) -> bool:
        """Extract a document to a file. Returns True on success."""
        doc = self.get_document(doc_id)
        if not doc:
            return False
        Path(output_path).write_bytes(doc['data'])
        return True

    def delete_document(self, doc_id: int) -> bool:
        """Delete a document by id."""
        self._ensure_documents_table()
        cur = self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        return cur.rowcount > 0

    @staticmethod
    def _guess_mime(filename: str) -> str:
        ext = Path(filename).suffix.lower()
        mime_map = {
            '.pdf': 'application/pdf',
            '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
            '.svg': 'image/svg+xml',
            '.txt': 'text/plain', '.csv': 'text/csv', '.md': 'text/markdown',
            '.json': 'application/json', '.xml': 'application/xml',
            '.html': 'text/html', '.htm': 'text/html',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.xls': 'application/vnd.ms-excel',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.ppt': 'application/vnd.ms-powerpoint',
            '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            '.zip': 'application/zip', '.tar': 'application/x-tar',
            '.gz': 'application/gzip',
            '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
        }
        return mime_map.get(ext, 'application/octet-stream')

    # --- Thumbnails ---

    def _ensure_table(self, table_name: str, create_sql: str):
        """Create table if it doesn't exist (for backward compat with v1/v2 files)."""
        exists = self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ).fetchone()[0]
        if not exists:
            self._conn.executescript(create_sql)

    def add_thumbnail(self, frame_id: int, image_data: bytes,
                      width: int = 0, height: int = 0):
        self._ensure_table('thumbnails', """
            CREATE TABLE IF NOT EXISTS thumbnails (
                frame_id INTEGER PRIMARY KEY,
                image_data BLOB NOT NULL,
                width INTEGER,
                height INTEGER,
                size_bytes INTEGER
            );
        """)
        self._conn.execute(
            "INSERT OR REPLACE INTO thumbnails "
            "(frame_id, image_data, width, height, size_bytes) VALUES (?, ?, ?, ?, ?)",
            (frame_id, image_data, width, height, len(image_data))
        )

    def get_thumbnail(self, frame_id: int) -> Optional[bytes]:
        exists = self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='thumbnails'"
        ).fetchone()[0]
        if not exists:
            return None
        row = self._conn.execute(
            "SELECT image_data FROM thumbnails WHERE frame_id = ?", (frame_id,)
        ).fetchone()
        return row[0] if row else None

    def generate_thumbnail(self, frame_id: int, max_size: int = 320,
                           quality: int = 75) -> Optional[bytes]:
        """Generate and store a thumbnail from a full frame."""
        if not HAS_IMAGING:
            raise RuntimeError("Pillow required for thumbnail generation")
        data = self.get_frame_image(frame_id)
        if not data:
            return None
        img = Image.open(io.BytesIO(data))
        img.thumbnail((max_size, max_size))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality)
        thumb_data = buf.getvalue()
        self.add_thumbnail(frame_id, thumb_data, img.width, img.height)
        return thumb_data

    # --- Embeddings ---

    def add_embedding(self, frame_id: int, model: str, vector):
        """Store an embedding vector for a frame.
        vector: list/array of floats or numpy array.
        """
        self._ensure_table('embeddings', """
            CREATE TABLE IF NOT EXISTS embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                frame_id INTEGER NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector BLOB NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_frame_model
                ON embeddings(frame_id, model);
        """)
        import struct
        if hasattr(vector, 'tolist'):
            vector = vector.tolist()
        dims = len(vector)
        blob = struct.pack(f'{dims}f', *vector)
        self._conn.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(frame_id, model, dimensions, vector) VALUES (?, ?, ?, ?)",
            (frame_id, model, dims, blob)
        )

    def get_embedding(self, frame_id: int, model: str = 'clip') -> Optional[dict]:
        exists = self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='embeddings'"
        ).fetchone()[0]
        if not exists:
            return None
        row = self._conn.execute(
            "SELECT dimensions, vector FROM embeddings WHERE frame_id = ? AND model = ?",
            (frame_id, model)
        ).fetchone()
        if not row:
            return None
        import struct
        dims = row[0]
        vector = list(struct.unpack(f'{dims}f', row[1]))
        return {'frame_id': frame_id, 'model': model, 'dimensions': dims, 'vector': vector}

    def search_similar(self, vector, model: str = 'clip', top_k: int = 10) -> list:
        """Find frames with most similar embeddings (cosine similarity)."""
        import struct
        import math

        exists = self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='embeddings'"
        ).fetchone()[0]
        if not exists:
            return []

        if hasattr(vector, 'tolist'):
            vector = vector.tolist()

        rows = self._conn.execute(
            "SELECT frame_id, dimensions, vector FROM embeddings WHERE model = ?",
            (model,)
        ).fetchall()

        def cosine_sim(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            return dot / (na * nb) if na > 0 and nb > 0 else 0

        results = []
        for fid, dims, blob in rows:
            v = list(struct.unpack(f'{dims}f', blob))
            sim = cosine_sim(vector, v)
            results.append({'frame_id': fid, 'similarity': sim})

        results.sort(key=lambda x: x['similarity'], reverse=True)
        return results[:top_k]

    # --- Slicing ---

    def slice_to_file(self, output_path: str, start_frame: int, end_frame: int):
        """Extract a range of frames into a new .vh file."""
        with VHFile(output_path, mode='w') as out:
            for key, value in self.get_all_meta().items():
                out.set_meta(key, value)
            out.set_meta('slice_start', start_frame)
            out.set_meta('slice_end', end_frame)

            for new_id, fid in enumerate(range(start_frame, end_frame + 1)):
                data = self.get_frame_image(fid)
                if data is None:
                    continue
                ts = self._conn.execute(
                    "SELECT timestamp_ms FROM frames WHERE frame_id = ?", (fid,)
                ).fetchone()[0]
                w, h = self._get_dimensions()
                out.add_frame(new_id, ts, data, 'webp', w, h)

            out.set_meta('frame_count', new_id + 1)
            out.commit()

    # --- Export ---

    def export_frame(self, frame_id: int, output_path: str) -> bool:
        data = self.get_frame_image(frame_id)
        if not data:
            return False
        with open(output_path, 'wb') as f:
            f.write(data)
        return True

    def export_to_mp4(self, output_path: str, fps: float = None,
                      start_frame: int = None, end_frame: int = None):
        """Re-encode .vh back to MP4 using ffmpeg, with audio if available."""
        import subprocess
        import tempfile
        import os

        if fps is None:
            fps = self.get_meta('fps', 24)

        count = self.frame_count
        start = start_frame or 0
        end = end_frame or (count - 1)
        end = min(end, count - 1)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Detect frame format
            sample = self.get_frame_image(start)
            if sample and sample[:2] == b'\xff\xd8':
                ext = 'jpg'
            else:
                ext = 'webp'

            for idx, fid in enumerate(range(start, end + 1)):
                data = self.get_frame_image(fid)
                if data:
                    with open(os.path.join(tmpdir, f'frame_{idx:07d}.{ext}'), 'wb') as f:
                        f.write(data)

            # Export audio if present
            audio_path = os.path.join(tmpdir, 'audio.opus')
            has_audio = self.export_audio(audio_path)

            cmd = [
                'ffmpeg', '-y', '-v', 'warning',
                '-framerate', str(fps),
                '-i', os.path.join(tmpdir, f'frame_%07d.{ext}'),
            ]

            if has_audio:
                start_sec = start / fps
                duration_sec = (end - start + 1) / fps
                cmd.extend([
                    '-ss', f'{start_sec:.3f}',
                    '-i', audio_path,
                    '-t', f'{duration_sec:.3f}',
                ])

            cmd.extend(['-c:v', 'libx264', '-pix_fmt', 'yuv420p'])

            if has_audio:
                cmd.extend(['-c:a', 'aac', '-b:a', '128k'])

            cmd.append(output_path)
            subprocess.run(cmd, capture_output=True)

    # --- AI Analysis ---

    def analyze(self, fn, frames=None, batch_size=1, key='ai_result',
                commit_every=100, progress=True):
        """Run a function on frames and store results as annotations.

        Args:
            fn: callable(image_bytes) -> Any  OR  callable(list[image_bytes]) -> list[Any]
                If batch_size > 1, fn receives a list and must return a list.
            frames: list of frame_ids to process (default: all)
            batch_size: number of frames per call (1 = one-by-one)
            key: annotation key to store results under
            commit_every: commit to DB every N frames
            progress: print progress to stdout

        Returns:
            dict with stats: {'processed': N, 'errors': N, 'elapsed': float}
        """
        import time as _time

        count = self.frame_count
        if frames is None:
            frames = list(range(count))

        total = len(frames)
        processed = 0
        errors = 0
        t0 = _time.time()

        if batch_size <= 1:
            for i, fid in enumerate(frames):
                try:
                    data = self.get_frame_image(fid)
                    if data is None:
                        continue
                    result = fn(data)
                    if result is not None:
                        self.annotate(fid, key, result)
                    processed += 1
                except Exception as e:
                    errors += 1
                    if progress:
                        print(f"  Frame {fid}: error - {e}")

                if progress and (i + 1) % 50 == 0:
                    elapsed = _time.time() - t0
                    rate = (i + 1) / elapsed if elapsed > 0 else 0
                    eta = (total - i - 1) / rate if rate > 0 else 0
                    print(f"  {i+1}/{total} ({(i+1)*100//total}%) "
                          f"- {rate:.1f} f/s - ETA: {eta:.0f}s", end='\r')

                if (i + 1) % commit_every == 0:
                    self.commit()
        else:
            for i in range(0, total, batch_size):
                batch_ids = frames[i:i + batch_size]
                batch_data = []
                valid_ids = []
                for fid in batch_ids:
                    data = self.get_frame_image(fid)
                    if data is not None:
                        batch_data.append(data)
                        valid_ids.append(fid)

                if not batch_data:
                    continue

                try:
                    results = fn(batch_data)
                    for fid, result in zip(valid_ids, results):
                        if result is not None:
                            self.annotate(fid, key, result)
                        processed += 1
                except Exception as e:
                    errors += len(valid_ids)
                    if progress:
                        print(f"  Batch {i//batch_size}: error - {e}")

                done = min(i + batch_size, total)
                if progress and done % 50 < batch_size:
                    elapsed = _time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    print(f"  {done}/{total} ({done*100//total}%) "
                          f"- {rate:.1f} f/s - ETA: {eta:.0f}s", end='\r')

                if done % commit_every < batch_size:
                    self.commit()

        self.commit()
        elapsed = _time.time() - t0

        if progress:
            print(f"\n  Done: {processed} processed, {errors} errors, {elapsed:.1f}s")

        return {'processed': processed, 'errors': errors, 'elapsed': elapsed}

    def iter_frames(self, start: int = 0, end: int = None, step: int = 1):
        """Iterator that yields (frame_id, image_bytes) lazily."""
        if end is None:
            end = self.frame_count - 1
        for fid in range(start, end + 1, step):
            data = self.get_frame_image(fid)
            if data is not None:
                yield fid, data

    def iter_pixels(self, start: int = 0, end: int = None, step: int = 1):
        """Iterator that yields (frame_id, numpy_array) lazily."""
        if not HAS_IMAGING:
            raise RuntimeError("numpy and Pillow required")
        if end is None:
            end = self.frame_count - 1
        for fid in range(start, end + 1, step):
            pixels = self.get_frame_pixels(fid)
            if pixels is not None:
                yield fid, pixels

    # --- Lifecycle ---

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.commit()
        # Clean up WAL/SHM temp files so they don't clutter the directory
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

    def summary(self) -> dict:
        meta = self.get_all_meta()
        frame_count = self.frame_count
        audio_count = self._conn.execute("SELECT COUNT(*) FROM audio").fetchone()[0]
        annotation_count = self._conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
        total_frame_bytes = self._conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM frames"
        ).fetchone()[0]
        file_size = self.path.stat().st_size

        result = {
            'file': str(self.path),
            'file_size_mb': round(file_size / (1024 * 1024), 2),
            'metadata': meta,
            'frame_count': frame_count,
            'total_frame_data_mb': round(total_frame_bytes / (1024 * 1024), 2),
            'audio_tracks': audio_count,
            'annotations': annotation_count,
            'version': self.version,
        }

        if self.version == 2:
            result['frame_stats'] = self.get_frame_stats()

        return result
