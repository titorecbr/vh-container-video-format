#!/usr/bin/env python3
"""Demo: compare .vh vs MP4 for AI workloads."""

import time
import subprocess
import sys
import json
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vhlib import VHFile


def demo(vh_path: str, mp4_path: str):
    print("=" * 60)
    print("  VH FORMAT - AI-Optimized Video Container Demo")
    print("=" * 60)

    vh = VHFile(vh_path, mode='r')
    summary = vh.summary()
    meta = summary['metadata']
    fps = meta.get('fps', 24)
    frame_count = summary['frame_count']

    print(f"\n[INFO]")
    print(f"  File:       {Path(vh_path).name}")
    print(f"  Size:       {summary['file_size_mb']} MB")
    print(f"  Frames:     {frame_count}")
    print(f"  Resolution: {meta.get('width')}x{meta.get('height')}")
    print(f"  FPS:        {fps}")
    print(f"  Duration:   {meta.get('duration_s', 0):.1f}s")
    print(f"  Format:     {meta.get('image_format')} (q={meta.get('quality')})")

    # --- Benchmark 1: Random frame access ---
    print(f"\n[BENCHMARK] Random frame access")
    n_vh = 200
    n_mp4 = 10
    test_frames = [random.randint(0, frame_count - 1) for _ in range(n_vh)]

    # VH
    t0 = time.time()
    for fid in test_frames:
        data = vh.get_frame_image(fid)
    vh_time = time.time() - t0
    vh_per_frame = vh_time / n_vh * 1000

    # MP4 (ffmpeg seek - real-world scenario)
    t0 = time.time()
    for fid in test_frames[:n_mp4]:
        timestamp = fid / fps
        subprocess.run([
            'ffmpeg', '-v', 'quiet',
            '-ss', f'{timestamp:.3f}', '-i', mp4_path,
            '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'
        ], capture_output=True)
    mp4_time = time.time() - t0
    mp4_per_frame = mp4_time / n_mp4 * 1000

    speedup = mp4_per_frame / vh_per_frame

    print(f"  VH:  {n_vh} frames in {vh_time*1000:.0f}ms  ({vh_per_frame:.1f}ms/frame)")
    print(f"  MP4: {n_mp4} frames in {mp4_time*1000:.0f}ms  ({mp4_per_frame:.1f}ms/frame)")
    print(f"  >>> VH is {speedup:.0f}x faster <<<")

    # --- Benchmark 2: Sequential range ---
    print(f"\n[BENCHMARK] Sequential range read (100 frames)")
    start = frame_count // 2

    t0 = time.time()
    for fid in range(start, start + 100):
        data = vh.get_frame_image(fid)
    vh_seq_time = time.time() - t0

    print(f"  VH:  100 frames in {vh_seq_time*1000:.0f}ms ({vh_seq_time*10:.1f}ms/frame)")

    # --- Demo 3: Annotations ---
    print(f"\n[DEMO] Annotations")
    vh_rw = VHFile(vh_path, mode='a')

    vh_rw.annotate(0, 'scene', 'intro')
    vh_rw.annotate(0, 'objects', ['window', 'teams', 'taskbar'])
    vh_rw.annotate(frame_count // 4, 'scene', 'content')
    vh_rw.annotate(frame_count // 2, 'scene', 'midpoint')
    vh_rw.annotate(frame_count // 2, 'ai_analysis', {
        'description': 'Screen recording of Teams meeting',
        'confidence': 0.95
    })
    vh_rw.annotate(frame_count - 1, 'scene', 'end')
    vh_rw.commit()

    # Query annotations
    ann = vh_rw.get_annotations(0)
    print(f"  Frame 0 annotations: {json.dumps(ann, indent=4)}")

    scenes = vh_rw.search_annotations('scene')
    print(f"  Frames with 'scene': {[s['frame_id'] for s in scenes]}")

    vh_rw.close()

    # --- Demo 4: Extract frame ---
    print(f"\n[DEMO] Frame extraction")
    mid = frame_count // 2
    frame = vh.get_frame(mid)
    out_path = str(Path(vh_path).parent / 'frame_extraido.jpg')
    with open(out_path, 'wb') as f:
        f.write(frame['data'])
    print(f"  Frame {mid} -> {out_path}")
    print(f"  Timestamp: {frame['timestamp_ms']/1000:.2f}s")
    print(f"  Size: {frame['size_bytes'] / 1024:.1f} KB")

    # --- Demo 5: Slice ---
    print(f"\n[DEMO] Slice (cortar trecho)")
    slice_path = str(Path(vh_path).parent / 'slice_test.vh')
    t0 = time.time()
    vh.slice_to_file(slice_path, 0, 240)  # ~10s at 24fps
    slice_time = time.time() - t0
    slice_size = Path(slice_path).stat().st_size / (1024 * 1024)
    print(f"  Frames 0-240 -> {Path(slice_path).name}")
    print(f"  Slice size: {slice_size:.1f} MB")
    print(f"  Time: {slice_time*1000:.0f}ms (sem recodificacao!)")

    # --- Demo 6: Audio ---
    print(f"\n[DEMO] Audio")
    audio_path = str(Path(vh_path).parent / 'audio_extraido.opus')
    if vh.export_audio(audio_path):
        audio_size = Path(audio_path).stat().st_size / 1024
        print(f"  Audio exportado: {audio_path} ({audio_size:.0f} KB)")
    else:
        print("  Sem trilha de audio")

    vh.close()

    # --- Summary ---
    mp4_size = Path(mp4_path).stat().st_size / (1024 * 1024)
    vh_size = Path(vh_path).stat().st_size / (1024 * 1024)

    print(f"\n{'=' * 60}")
    print(f"  RESUMO")
    print(f"{'=' * 60}")
    print(f"  MP4: {mp4_size:.1f} MB | VH: {vh_size:.1f} MB ({vh_size/mp4_size:.1f}x)")
    print(f"  Acesso aleatorio: {speedup:.0f}x mais rapido no VH")
    print(f"  Cada frame acessivel em ~{vh_per_frame:.1f}ms (vs ~{mp4_per_frame:.0f}ms no MP4)")
    print(f"  Annotations: nativo (SQL queries)")
    print(f"  Corte/slice: instantaneo, sem recodificacao")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python demo.py <file.vh> <original.mp4>")
        sys.exit(1)
    demo(sys.argv[1], sys.argv[2])
