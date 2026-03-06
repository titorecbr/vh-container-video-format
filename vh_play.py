#!/usr/bin/env python3
"""
Play .vh files using VLC (or ffplay as fallback).

How it works:
  1. Reads frames directly from the .vh (SQLite)
  2. Mounts an AVI container (MJPEG copy - no re-encoding)
  3. Muxes audio if present
  4. Opens in VLC
  5. Cleans up temp files when player closes

Usage:
  python3 vh_play.py <file.vh>
  python3 vh_play.py <file.vh> --player ffplay
  python3 vh_play.py <file.vh> --start 1000 --end 5000   # frames 1000-5000
"""

import sys
import os
import time
import shutil
import tempfile
import subprocess
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vhlib import VHFile


def find_player():
    for player in ['vlc', 'ffplay', 'mpv']:
        if shutil.which(player):
            return player
    return None


def play(vh_path: str, player: str = None, start_frame: int = None, end_frame: int = None):
    if player is None:
        player = find_player()
        if player is None:
            print("Nenhum player encontrado (vlc, ffplay, mpv).")
            sys.exit(1)

    vh = VHFile(vh_path, mode='r')
    meta = vh.get_all_meta()
    fps = meta.get('fps', 24)
    total = vh.frame_count
    fmt = meta.get('image_format', 'jpeg')
    ext = 'jpg' if fmt == 'jpeg' else fmt

    start = start_frame or 0
    end = end_frame or (total - 1)
    end = min(end, total - 1)
    count = end - start + 1

    print(f"VH Player")
    print(f"  Arquivo:    {Path(vh_path).name}")
    print(f"  Resolucao:  {meta.get('width')}x{meta.get('height')}")
    print(f"  FPS:        {fps}")
    print(f"  Frames:     {start}-{end} ({count} de {total})")
    print(f"  Player:     {player}")
    print()

    tmpdir = tempfile.mkdtemp(prefix='vh_play_')
    try:
        # Step 1: Extract frames
        print(f"[1/3] Extraindo {count} frames...")
        t0 = time.time()

        for i, frame_id in enumerate(range(start, end + 1)):
            data = vh.get_frame_image(frame_id)
            if data is None:
                continue
            out_file = os.path.join(tmpdir, f'frame_{i:07d}.{ext}')
            with open(out_file, 'wb') as f:
                f.write(data)

            if (i + 1) % 500 == 0 or i == count - 1:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                eta = (count - i - 1) / rate if rate > 0 else 0
                print(f"      {i+1}/{count} ({(i+1)*100//count}%) "
                      f"- {rate:.0f} frames/s - ETA: {eta:.0f}s", end='\r')

        t1 = time.time()
        print(f"\n      Pronto em {t1-t0:.1f}s")

        # Step 2: Extract audio
        audio_path = os.path.join(tmpdir, 'audio.opus')
        has_audio = vh.export_audio(audio_path)

        # Step 3: Mux AVI (MJPEG copy = instant, no re-encode)
        print("[2/3] Montando AVI...")
        output_avi = os.path.join(tmpdir, 'playback.avi')

        ffmpeg_cmd = [
            'ffmpeg', '-y', '-v', 'warning',
            '-framerate', str(fps),
            '-i', os.path.join(tmpdir, f'frame_%07d.{ext}'),
        ]
        if has_audio:
            # Calculate audio offset and duration for sliced playback
            start_sec = start / fps
            duration_sec = count / fps
            ffmpeg_cmd.extend([
                '-ss', f'{start_sec:.3f}',
                '-i', audio_path,
                '-t', f'{duration_sec:.3f}',
            ])

        ffmpeg_cmd.extend(['-c:v', 'copy'])

        if has_audio:
            ffmpeg_cmd.extend(['-c:a', 'aac', '-b:a', '128k'])

        ffmpeg_cmd.append(output_avi)

        t2 = time.time()
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)

        if result.returncode != 0:
            # Fallback: re-encode with mjpeg if copy fails
            print("      Copy falhou, re-encoding...")
            ffmpeg_cmd = [
                'ffmpeg', '-y', '-v', 'warning',
                '-framerate', str(fps),
                '-i', os.path.join(tmpdir, f'frame_%07d.{ext}'),
            ]
            if has_audio:
                ffmpeg_cmd.extend([
                    '-ss', f'{start_sec:.3f}',
                    '-i', audio_path,
                    '-t', f'{duration_sec:.3f}',
                ])
            ffmpeg_cmd.extend(['-c:v', 'mjpeg', '-q:v', '5'])
            if has_audio:
                ffmpeg_cmd.extend(['-c:a', 'aac', '-b:a', '128k'])
            ffmpeg_cmd.append(output_avi)
            subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=True)

        t3 = time.time()
        avi_size = os.path.getsize(output_avi) / (1024 * 1024)
        print(f"      AVI: {avi_size:.1f} MB ({t3-t2:.1f}s)")

        # Step 4: Open player
        print(f"[3/3] Abrindo {player}...")
        print()

        if player == 'vlc':
            subprocess.run(['vlc', '--play-and-exit', output_avi])
        elif player == 'ffplay':
            subprocess.run(['ffplay', '-autoexit', output_avi])
        elif player == 'mpv':
            subprocess.run(['mpv', output_avi])
        else:
            subprocess.run([player, output_avi])

    finally:
        vh.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("Temp files cleaned up.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Play .vh video files')
    parser.add_argument('input', help='Input .vh file')
    parser.add_argument('--player', default=None, help='Player: vlc, ffplay, mpv (default: auto)')
    parser.add_argument('--start', type=int, default=None, help='Start frame')
    parser.add_argument('--end', type=int, default=None, help='End frame')

    args = parser.parse_args()
    play(args.input, args.player, args.start, args.end)
