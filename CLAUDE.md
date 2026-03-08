# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

VH Format is a SQLite-based video container optimized for AI workloads. It stores video frames as individual images (JPEG/WebP) in a SQLite database, enabling O(1) random frame access without video decoding. The project is a Python library + CLI + GUI viewer + VLC native plugin.

## Commands

```bash
# CLI (unified entry point)
./vh info <file.vh>
./vh convert <input.mp4> [output.vh] [--delta] [--quality N] [--fps N]
./vh play <file.vh>
./vh viewer <file.vh>
./vh export <file.vh> -o output.mp4

# Run tests (custom framework, not pytest - requires .vh test files present)
python3 test_vh.py

# Build VLC plugin (requires libvlccore-dev, libvlc-dev, libsqlite3-dev)
cd vlc-plugin && make
make install-user   # installs to ~/.local/lib/vlc/plugins/demux/
```

## Architecture

### Storage Layer

`.vh` files are SQLite databases with WAL journaling. Schema tables:
- **metadata** - key/value pairs (JSON-encoded values): width, height, fps, duration_s, etc.
- **frames** - frame_id (PK), timestamp_ms, frame_type, ref_frame_id, image_format, image_data (BLOB)
- **audio** - Opus-encoded audio tracks as BLOBs
- **annotations** - per-frame key/value annotations (JSON values), searchable by key
- **thumbnails** / **embeddings** - created lazily with `_ensure_table()`

### Format Versions

- **v1**: Each frame stored independently as JPEG. Detected by absence of `frame_type` column.
- **v2**: Three frame types in `frame_type` column:
  - `full` - complete keyframe image (JPEG/WebP)
  - `ref` - pointer to identical frame (0 bytes, deduplication)
  - `delta` - XOR diff vs keyframe, compressed with zlib (`xor_zlib` format)

Version detection: `PRAGMA table_info(frames)` checks for `frame_type` column.

### Core Components

- **`vhlib.py`** - Core library. `VHFile` class handles all read/write operations. Delta decoding uses `numpy` XOR + `zlib`. Keyframe cache (max 4 entries) for delta decoding performance. The `analyze()` method runs arbitrary functions on frames and stores results as annotations.
- **`vh`** - CLI entry point (executable Python script). Dispatches to subcommands via argparse. Imports from `convert_optimized`, `vh_play`, `vh_viewer` as needed.
- **`convert.py`** - Original v1 converter (temp files approach).
- **`convert_optimized.py`** - v2 converter. Two modes: fast (JPEG pipe from ffmpeg + dedup) and delta (temp files + XOR compression). The CLI `vh convert` uses this one.
- **`vh_stream.py`** - `VHStream` class: lazy-loading reader that loads only the frame index on open. Supports prefetch with background thread (separate SQLite connection), async iteration, and `__getitem__` slice syntax.
- **`vh_viewer.py`** - Tkinter GUI player with PIL-rendered anti-aliased widgets (2x supersampling). `FramePipeline` prefetches frames in background thread. `AudioPlayer` uses ffplay subprocess. Custom `Timeline` and `IconBtn` widgets.
- **`vlc-plugin/vh_demux.c`** - Native VLC demuxer plugin in C. Opens SQLite directly, feeds JPEG frames as MJPEG to VLC. Supports seek. Delta frames fall back to keyframe (no numpy in C).

### Dependencies

- Python 3 with sqlite3 (stdlib)
- ffmpeg/ffprobe (frame extraction, conversion, audio, playback)
- Pillow + numpy (delta frame decoding, viewer, thumbnails)
- tkinter (viewer GUI)
- VLC plugin build: gcc, libvlccore-dev, libvlc-dev, libsqlite3-dev

### Key Patterns

- All Python modules use `sys.path.insert(0, ...)` to resolve `vhlib` from the script's directory.
- `VHFile` is a context manager (`with VHFile(...) as vh:`). Always call `commit()` after writes, `close()` when done.
- Metadata values are JSON-encoded in the database (`json.dumps`/`json.loads`).
- Frame images are raw JPEG/WebP bytes stored as SQLite BLOBs.
- Audio is stored as a single Opus blob per track, extracted/muxed via ffmpeg pipes.
