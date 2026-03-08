#!/usr/bin/env python3
"""Convert MP4 (or any ffmpeg-supported format) to .vh format."""

import subprocess
import sys
import os
import time
import json
import tempfile
import argparse
from pathlib import Path

from .vhlib import VHFile


def get_video_info(input_path: str) -> dict:
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_format', '-show_streams', input_path],
        capture_output=True, text=True
    )
    return json.loads(result.stdout)


def convert(input_path: str, output_path: str = None,
            image_format: str = 'jpeg', quality: int = 5,
            fps: float = None):
    """
    Convert a video file to .vh format.

    Args:
        input_path: Path to source video
        output_path: Path for .vh output (default: same name with .vh extension)
        image_format: Frame image format ('jpeg', 'png', 'webp')
        quality: ffmpeg quality scale (2=best, 31=worst for jpeg)
        fps: Target FPS (default: source fps)
    """
    input_path = str(Path(input_path).resolve())
    if output_path is None:
        output_path = str(Path(input_path).with_suffix('.vh'))

    # Analyze source video
    info = get_video_info(input_path)
    video_stream = next(s for s in info['streams'] if s['codec_type'] == 'video')
    audio_stream = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)

    width = int(video_stream['width'])
    height = int(video_stream['height'])
    source_fps = eval(video_stream['r_frame_rate'])
    duration = float(info['format']['duration'])
    target_fps = fps or source_fps
    estimated_frames = int(duration * target_fps)

    print(f"=== MP4 -> VH Converter ===")
    print(f"Source:     {Path(input_path).name}")
    print(f"Resolution: {width}x{height}")
    print(f"FPS:        {source_fps}" + (f" -> {target_fps}" if fps else ""))
    print(f"Duration:   {duration:.1f}s")
    print(f"Est frames: {estimated_frames}")
    print(f"Format:     {image_format} (q={quality})")
    print(f"Output:     {output_path}")
    print()

    with VHFile(output_path, mode='w') as vh:
        # Store metadata
        vh.set_meta('source_file', Path(input_path).name)
        vh.set_meta('width', width)
        vh.set_meta('height', height)
        vh.set_meta('fps', target_fps)
        vh.set_meta('source_fps', source_fps)
        vh.set_meta('duration_s', duration)
        vh.set_meta('source_codec', video_stream['codec_name'])
        vh.set_meta('image_format', image_format)
        vh.set_meta('quality', quality)

        # Step 1: Extract frames with ffmpeg to temp directory
        with tempfile.TemporaryDirectory(prefix='vh_') as tmpdir:
            print("[1/3] Extracting frames with ffmpeg...")
            ext = 'jpg' if image_format == 'jpeg' else image_format

            ffmpeg_cmd = ['ffmpeg', '-i', input_path, '-v', 'warning']
            if fps:
                ffmpeg_cmd.extend(['-vf', f'fps={fps}'])

            if image_format in ('jpeg', 'jpg'):
                ffmpeg_cmd.extend(['-q:v', str(quality)])
            elif image_format == 'webp':
                ffmpeg_cmd.extend(['-quality', str(quality)])

            ffmpeg_cmd.append(os.path.join(tmpdir, f'frame_%07d.{ext}'))

            t0 = time.time()
            proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"ffmpeg error: {proc.stderr}")
                sys.exit(1)

            frame_files = sorted(f for f in os.listdir(tmpdir) if f.startswith('frame_'))
            total_frames = len(frame_files)
            t1 = time.time()
            print(f"      {total_frames} frames extracted in {t1-t0:.1f}s "
                  f"({total_frames/(t1-t0):.0f} frames/s)")

            # Step 2: Insert frames into .vh
            print("[2/3] Packing frames into .vh...")
            t2 = time.time()
            batch = 200

            for i, fname in enumerate(frame_files):
                filepath = os.path.join(tmpdir, fname)
                with open(filepath, 'rb') as f:
                    data = f.read()

                timestamp_ms = (i / target_fps) * 1000
                vh.add_frame(i, timestamp_ms, data, image_format, width, height)

                if (i + 1) % batch == 0:
                    vh.commit()
                    elapsed = time.time() - t2
                    rate = (i + 1) / elapsed
                    eta = (total_frames - i - 1) / rate
                    print(f"      {i+1}/{total_frames} ({(i+1)*100//total_frames}%) "
                          f"- {rate:.0f} frames/s - ETA: {eta:.0f}s", end='\r')

            vh.commit()
            t3 = time.time()
            print(f"\n      {total_frames} frames packed in {t3-t2:.1f}s "
                  f"({total_frames/(t3-t2):.0f} frames/s)")

        # Step 3: Extract audio
        print("[3/3] Extracting audio...")
        audio_cmd = [
            'ffmpeg', '-i', input_path, '-v', 'warning',
            '-vn', '-ac', '2', '-acodec', 'libopus', '-b:a', '128k',
            '-f', 'opus', 'pipe:1'
        ]
        audio_proc = subprocess.run(audio_cmd, capture_output=True)

        if audio_proc.returncode == 0 and len(audio_proc.stdout) > 0:
            vh.add_audio(
                data=audio_proc.stdout,
                codec='opus',
                sample_rate=int(audio_stream['sample_rate']) if audio_stream else 48000,
                channels=int(audio_stream['channels']) if audio_stream else 2,
                duration_ms=duration * 1000
            )
            print(f"      Audio: {len(audio_proc.stdout) / 1024:.1f} KB (opus)")
        else:
            print("      No audio or extraction failed")

        vh.set_meta('frame_count', total_frames)
        vh.commit()

    # Final summary
    vh_size = Path(output_path).stat().st_size
    mp4_size = Path(input_path).stat().st_size
    total_time = time.time() - t0

    print(f"\n=== Done in {total_time:.1f}s ===")
    print(f"MP4 size: {mp4_size / (1024*1024):.1f} MB")
    print(f"VH size:  {vh_size / (1024*1024):.1f} MB")
    print(f"Ratio:    {vh_size/mp4_size:.1f}x")
    print(f"Frames:   {total_frames}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Convert video to .vh format')
    parser.add_argument('input', help='Input video file')
    parser.add_argument('output', nargs='?', help='Output .vh file')
    parser.add_argument('--fps', type=float, help='Target FPS (default: source)')
    parser.add_argument('--format', default='jpeg', choices=['jpeg', 'png', 'webp'],
                        help='Frame image format (default: jpeg)')
    parser.add_argument('--quality', type=int, default=5,
                        help='Quality 2-31 for jpeg (lower=better, default: 5)')

    args = parser.parse_args()
    convert(args.input, args.output, args.format, args.quality, args.fps)
