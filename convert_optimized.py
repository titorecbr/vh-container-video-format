#!/usr/bin/env python3
"""
Optimized converter: MP4 -> .vh v2

Fast mode (default):
  - Pipes JPEG frames directly from ffmpeg (no temp files)
  - Deduplicates identical consecutive frames
  - ~60 seconds for 12000 frames
  - ~14x MP4 size (vs 36x in v1)

Delta mode (--delta):
  - All of the above + XOR delta compression
  - Significantly smaller (~3-5x MP4) but slower conversion
"""

import subprocess
import sys
import os
import io
import time
import json
import hashlib
import zlib
import argparse
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vhlib import VHFile


def get_video_info(input_path: str) -> dict:
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_format', '-show_streams', input_path],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)


def convert(input_path: str, output_path: str = None,
            quality: int = 10, fps: float = None, use_delta: bool = False,
            keyframe_interval: int = 24):
    input_path = str(Path(input_path).resolve())
    if output_path is None:
        output_path = str(Path(input_path).with_suffix('.vh'))

    info = get_video_info(input_path)
    video_stream = next(s for s in info['streams'] if s['codec_type'] == 'video')
    audio_stream = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)

    width = int(video_stream['width'])
    height = int(video_stream['height'])
    source_fps = eval(video_stream['r_frame_rate'])
    duration = float(info['format']['duration'])
    target_fps = fps or source_fps
    total_frames = int(duration * target_fps)

    mode = "FAST (pipe + dedup)" if not use_delta else "DELTA (pipe + dedup + xor_zlib)"

    print(f"=== MP4 -> VH v2 [{mode}] ===")
    print(f"Source:     {Path(input_path).name} ({os.path.getsize(input_path)/(1024*1024):.1f} MB)")
    print(f"Resolution: {width}x{height} @ {target_fps}fps")
    print(f"Duration:   {duration:.1f}s | Est frames: {total_frames}")
    print(f"JPEG q:     {quality}")
    print()

    if use_delta:
        _convert_delta(input_path, output_path, info, video_stream, audio_stream,
                        width, height, target_fps, duration, total_frames,
                        quality, keyframe_interval, fps)
    else:
        _convert_fast(input_path, output_path, info, video_stream, audio_stream,
                       width, height, target_fps, duration, total_frames, quality, fps)


def _convert_fast(input_path, output_path, info, video_stream, audio_stream,
                   width, height, target_fps, duration, total_frames, quality, fps):
    """Fast mode: pipe JPEG frames from ffmpeg, dedup by hash, insert into SQLite."""

    # Start ffmpeg JPEG pipe
    cmd = ['ffmpeg', '-v', 'warning', '-i', input_path,
           '-f', 'image2pipe', '-c:v', 'mjpeg', '-q:v', str(quality)]
    if fps:
        cmd.extend(['-vf', f'fps={fps}'])
    cmd.append('pipe:1')

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stats = {'full': 0, 'ref': 0, 'total_bytes': 0}
    t0 = time.time()

    with VHFile(output_path, mode='w') as vh:
        _write_metadata(vh, input_path, width, height, target_fps, duration,
                        video_stream, quality, 'jpeg', False)

        print("[1/2] Extracting + packing (JPEG pipe)...")

        buffer = b''
        frame_id = 0
        prev_hash = None
        last_data_id = None

        while True:
            chunk = proc.stdout.read(262144)  # 256KB chunks
            if not chunk and not buffer:
                break
            if chunk:
                buffer += chunk

            # Parse complete JPEG frames from buffer
            while True:
                start = buffer.find(b'\xff\xd8')
                if start == -1:
                    buffer = b''
                    break
                end = buffer.find(b'\xff\xd9', start + 2)
                if end == -1:
                    buffer = buffer[start:]
                    break

                jpeg_data = buffer[start:end + 2]
                buffer = buffer[end + 2:]

                frame_hash = hashlib.md5(jpeg_data).digest()
                timestamp_ms = frame_id / target_fps * 1000

                if frame_hash == prev_hash and last_data_id is not None:
                    vh.add_frame_ref(frame_id, timestamp_ms, last_data_id, width, height)
                    stats['ref'] += 1
                else:
                    vh.add_frame(frame_id, timestamp_ms, jpeg_data, 'jpeg', width, height)
                    stats['full'] += 1
                    stats['total_bytes'] += len(jpeg_data)
                    last_data_id = frame_id
                    prev_hash = frame_hash

                frame_id += 1

                if frame_id % 200 == 0:
                    vh.commit()
                    _print_progress(frame_id, total_frames, stats, t0)

        vh.commit()
        proc.wait()

        t1 = time.time()
        print(f"\n      {frame_id} frames in {t1-t0:.1f}s ({frame_id/(t1-t0):.0f} frames/s)")

        _extract_audio(vh, input_path, audio_stream, duration)
        vh.set_meta('frame_count', frame_id)
        vh.commit()

    _print_summary(input_path, output_path, stats, frame_id, t0)


def _convert_delta(input_path, output_path, info, video_stream, audio_stream,
                    width, height, target_fps, duration, total_frames,
                    quality, keyframe_interval, fps):
    """Delta mode: extract JPEG to temp dir, then dedup + delta compress."""
    import numpy as np
    from PIL import Image

    frame_size = width * height * 3

    with tempfile.TemporaryDirectory(prefix='vh_') as tmpdir:
        # Step 1: Extract JPEG frames (fast, using ffmpeg)
        print("[1/3] Extracting JPEG frames (ffmpeg)...")
        t0 = time.time()

        ffmpeg_cmd = ['ffmpeg', '-v', 'warning', '-i', input_path, '-q:v', str(quality)]
        if fps:
            ffmpeg_cmd.extend(['-vf', f'fps={fps}'])
        ffmpeg_cmd.append(os.path.join(tmpdir, 'frame_%07d.jpg'))

        subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
        frame_files = sorted(f for f in os.listdir(tmpdir) if f.endswith('.jpg'))
        actual_frames = len(frame_files)

        t1 = time.time()
        print(f"      {actual_frames} frames in {t1-t0:.1f}s ({actual_frames/(t1-t0):.0f} f/s)")

        # Step 2: Dedup + Delta + Insert
        print("[2/3] Compressing (dedup + delta)...")
        stats = {'full': 0, 'ref': 0, 'delta': 0,
                 'full_bytes': 0, 'delta_bytes': 0}

        with VHFile(output_path, mode='w') as vh:
            _write_metadata(vh, input_path, width, height, target_fps, duration,
                            video_stream, quality, 'jpeg', True, keyframe_interval)

            prev_hash = None
            last_data_id = None
            prev_keyframe_id = None
            prev_keyframe_pixels = None

            t2 = time.time()

            for i, fname in enumerate(frame_files):
                filepath = os.path.join(tmpdir, fname)
                with open(filepath, 'rb') as f:
                    jpeg_data = f.read()

                frame_hash = hashlib.md5(jpeg_data).digest()
                timestamp_ms = i / target_fps * 1000

                # Dedup
                if frame_hash == prev_hash and last_data_id is not None:
                    vh.add_frame_ref(i, timestamp_ms, last_data_id, width, height)
                    stats['ref'] += 1
                    if (i + 1) % 200 == 0:
                        vh.commit()
                        _print_progress_delta(i + 1, actual_frames, stats, t2)
                    continue

                # Keyframe decision
                frames_since_kf = (i - prev_keyframe_id) if prev_keyframe_id is not None else keyframe_interval
                need_keyframe = (prev_keyframe_pixels is None or
                                 frames_since_kf >= keyframe_interval)

                if not need_keyframe:
                    # Delta: decode JPEG, XOR with reference, compress
                    pixels = np.array(Image.open(io.BytesIO(jpeg_data)))
                    delta = np.bitwise_xor(pixels, prev_keyframe_pixels)
                    delta_compressed = zlib.compress(delta.tobytes(), level=1)

                    delta_threshold = frame_size * 0.03
                    if len(delta_compressed) < delta_threshold:
                        vh.add_frame_delta(i, timestamp_ms, prev_keyframe_id,
                                           delta_compressed, width, height)
                        stats['delta'] += 1
                        stats['delta_bytes'] += len(delta_compressed)
                        last_data_id = i
                        prev_hash = frame_hash
                        if (i + 1) % 200 == 0:
                            vh.commit()
                            _print_progress_delta(i + 1, actual_frames, stats, t2)
                        continue
                    else:
                        need_keyframe = True

                # Store as keyframe
                vh.add_frame(i, timestamp_ms, jpeg_data, 'jpeg', width, height)
                stats['full'] += 1
                stats['full_bytes'] += len(jpeg_data)

                # Decode for delta reference
                prev_keyframe_pixels = np.array(Image.open(io.BytesIO(jpeg_data)))
                prev_keyframe_id = i
                last_data_id = i
                prev_hash = frame_hash

                if (i + 1) % 200 == 0:
                    vh.commit()
                    _print_progress_delta(i + 1, actual_frames, stats, t2)

            vh.commit()
            t3 = time.time()
            print(f"\n      Compressed in {t3-t2:.1f}s")

            _extract_audio(vh, input_path, audio_stream, duration)
            vh.set_meta('frame_count', actual_frames)
            vh.commit()

    stats['total_bytes'] = stats['full_bytes'] + stats['delta_bytes']
    _print_summary(input_path, output_path, stats, actual_frames, t0, delta=True)


def _write_metadata(vh, input_path, width, height, fps, duration,
                     video_stream, quality, image_format, has_delta,
                     keyframe_interval=None):
    vh.set_meta('source_file', Path(input_path).name)
    vh.set_meta('width', width)
    vh.set_meta('height', height)
    vh.set_meta('fps', fps)
    vh.set_meta('duration_s', duration)
    vh.set_meta('source_codec', video_stream['codec_name'])
    vh.set_meta('image_format', image_format)
    vh.set_meta('quality', quality)
    vh.set_meta('format_version', 2)
    vh.set_meta('has_delta', has_delta)
    if keyframe_interval:
        vh.set_meta('keyframe_interval', keyframe_interval)


def _extract_audio(vh, input_path, audio_stream, duration):
    print("[audio] Extracting...")
    audio_cmd = [
        'ffmpeg', '-v', 'warning', '-i', input_path,
        '-vn', '-ac', '2', '-acodec', 'libopus', '-b:a', '128k',
        '-f', 'opus', 'pipe:1'
    ]
    result = subprocess.run(audio_cmd, capture_output=True)
    if result.returncode == 0 and len(result.stdout) > 0:
        vh.add_audio(
            data=result.stdout, codec='opus',
            sample_rate=int(audio_stream['sample_rate']) if audio_stream else 48000,
            channels=int(audio_stream['channels']) if audio_stream else 2,
            duration_ms=duration * 1000
        )
        print(f"      Audio: {len(result.stdout) / 1024:.1f} KB")
    else:
        print("      No audio")


def _print_progress(frame_id, total, stats, t0):
    if frame_id % 200 != 0:
        return
    elapsed = time.time() - t0
    rate = frame_id / elapsed if elapsed > 0 else 0
    eta = (total - frame_id) / rate if rate > 0 else 0
    mb = stats['total_bytes'] / (1024 * 1024)
    print(f"      {frame_id}/{total} ({frame_id*100//total}%) "
          f"- {rate:.0f} f/s - ETA: {eta:.0f}s "
          f"[uniq:{stats['full']} dup:{stats['ref']} {mb:.0f}MB]", end='\r')


def _print_progress_delta(frame_id, total, stats, t0):
    elapsed = time.time() - t0
    rate = frame_id / elapsed if elapsed > 0 else 0
    eta = (total - frame_id) / rate if rate > 0 else 0
    print(f"      {frame_id}/{total} ({frame_id*100//total}%) "
          f"- {rate:.0f} f/s - ETA: {eta:.0f}s "
          f"[K:{stats['full']} D:{stats['delta']} R:{stats['ref']}]", end='\r')


def _print_summary(input_path, output_path, stats, frame_count, t0, delta=False):
    vh_size = os.path.getsize(output_path)
    mp4_size = os.path.getsize(input_path)
    total_time = time.time() - t0

    print(f"\n{'=' * 50}")
    print(f"  RESULTADO")
    print(f"{'=' * 50}")
    print(f"  Tempo:       {total_time:.1f}s")
    print(f"  MP4:         {mp4_size / (1024*1024):.1f} MB")
    print(f"  VH:          {vh_size / (1024*1024):.1f} MB")
    print(f"  Ratio:       {vh_size/mp4_size:.1f}x")
    print(f"  Frames:      {frame_count}")
    print(f"    Unicos:    {stats['full']}")
    print(f"    Duplicados:{stats['ref']} (0 bytes)")
    if delta:
        print(f"    Deltas:    {stats.get('delta', 0)} ({stats.get('delta_bytes', 0)/(1024*1024):.1f} MB)")
    print(f"  Data:        {stats['total_bytes']/(1024*1024):.1f} MB")
    print(f"{'=' * 50}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Optimized MP4 to .vh v2 converter')
    parser.add_argument('input', help='Input video file')
    parser.add_argument('output', nargs='?', help='Output .vh file')
    parser.add_argument('--quality', type=int, default=10,
                        help='JPEG quality 2-31 (lower=better, default: 10)')
    parser.add_argument('--fps', type=float, help='Target FPS')
    parser.add_argument('--delta', action='store_true',
                        help='Enable delta compression (slower, much smaller)')
    parser.add_argument('--keyframe-interval', type=int, default=24,
                        help='Keyframe interval for delta mode (default: 24)')

    args = parser.parse_args()
    convert(args.input, args.output, args.quality, args.fps,
            args.delta, args.keyframe_interval)
