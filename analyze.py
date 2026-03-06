#!/usr/bin/env python3
"""Analyze .vh file to find optimization opportunities."""

import sys
import hashlib
import io
import time
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))
from vhlib import VHFile


def analyze(vh_path: str):
    vh = VHFile(vh_path, mode='r')
    meta = vh.get_all_meta()
    count = vh.frame_count

    print("=" * 60)
    print("  ANALISE DE OTIMIZACAO - VH Format")
    print("=" * 60)
    print(f"  Arquivo: {Path(vh_path).name}")
    print(f"  Frames:  {count}")
    print(f"  Size:    {Path(vh_path).stat().st_size / (1024*1024):.1f} MB")
    print()

    # 1. Frame size distribution
    print("[1] Distribuicao de tamanho dos frames")
    sizes = []
    rows = vh._conn.execute(
        "SELECT frame_id, size_bytes FROM frames ORDER BY frame_id"
    ).fetchall()
    sizes = [r[1] for r in rows]

    avg = sum(sizes) / len(sizes)
    min_s = min(sizes)
    max_s = max(sizes)
    total = sum(sizes)

    print(f"  Total frame data: {total / (1024*1024):.1f} MB")
    print(f"  Media:  {avg/1024:.1f} KB/frame")
    print(f"  Min:    {min_s/1024:.1f} KB")
    print(f"  Max:    {max_s/1024:.1f} KB")

    # Size histogram
    buckets = Counter()
    for s in sizes:
        kb = s // 1024
        if kb < 50:
            buckets["< 50 KB"] += 1
        elif kb < 100:
            buckets["50-100 KB"] += 1
        elif kb < 200:
            buckets["100-200 KB"] += 1
        elif kb < 300:
            buckets["200-300 KB"] += 1
        else:
            buckets["> 300 KB"] += 1

    print(f"  Histograma:")
    for bucket in ["< 50 KB", "50-100 KB", "100-200 KB", "200-300 KB", "> 300 KB"]:
        c = buckets.get(bucket, 0)
        bar = "#" * (c * 40 // count)
        print(f"    {bucket:>12}: {c:5d} ({c*100//count:2d}%) {bar}")

    # 2. Duplicate frame detection
    print(f"\n[2] Deteccao de frames duplicados (hash MD5)")
    t0 = time.time()
    hashes = {}
    duplicates = 0
    dup_bytes_saved = 0
    sample_step = 1  # check all frames

    for i in range(0, count, sample_step):
        data = vh.get_frame_image(i)
        h = hashlib.md5(data).hexdigest()
        if h in hashes:
            duplicates += 1
            dup_bytes_saved += len(data)
        else:
            hashes[h] = i

    t1 = time.time()
    print(f"  Frames unicos:    {len(hashes)}")
    print(f"  Frames duplicados: {duplicates} ({duplicates*100//count}%)")
    print(f"  Economia se deduplicar: {dup_bytes_saved/(1024*1024):.1f} MB")
    print(f"  (analise em {t1-t0:.1f}s)")

    # 3. Near-duplicate detection (consecutive frames similarity)
    print(f"\n[3] Frames quase identicos (diff entre consecutivos)")
    t0 = time.time()
    try:
        from PIL import Image
        import numpy as np

        near_dupes = 0
        near_dupe_bytes = 0
        diffs = []
        sample_points = min(500, count - 1)
        step = max(1, (count - 1) // sample_points)

        for i in range(0, count - 1, step):
            data_a = vh.get_frame_image(i)
            data_b = vh.get_frame_image(i + 1)

            img_a = np.array(Image.open(io.BytesIO(data_a)))
            img_b = np.array(Image.open(io.BytesIO(data_b)))

            # Mean absolute difference per pixel
            diff = np.mean(np.abs(img_a.astype(int) - img_b.astype(int)))
            diffs.append((i, diff, sizes[i+1]))

            if diff < 1.0:  # nearly identical
                near_dupes += 1
                near_dupe_bytes += sizes[i+1]

        # Extrapolate to full video
        ratio = near_dupes / len(diffs)
        est_near_dupes = int(ratio * count)
        est_near_dupe_bytes = near_dupe_bytes * (count / len(diffs))

        print(f"  Amostras analisadas: {len(diffs)}")
        print(f"  Frames quase identicos (diff < 1.0): {near_dupes}/{len(diffs)} ({ratio*100:.0f}%)")
        print(f"  Estimativa total: ~{est_near_dupes} frames poupados")
        print(f"  Economia estimada: ~{est_near_dupe_bytes/(1024*1024):.0f} MB")

        # Diff distribution
        very_low = sum(1 for _, d, _ in diffs if d < 0.5)
        low = sum(1 for _, d, _ in diffs if 0.5 <= d < 2.0)
        medium = sum(1 for _, d, _ in diffs if 2.0 <= d < 10.0)
        high = sum(1 for _, d, _ in diffs if d >= 10.0)

        print(f"  Distribuicao de diff entre frames consecutivos:")
        print(f"    diff < 0.5 (identico):  {very_low:4d} ({very_low*100//len(diffs)}%)")
        print(f"    diff 0.5-2 (minimo):    {low:4d} ({low*100//len(diffs)}%)")
        print(f"    diff 2-10 (moderado):   {medium:4d} ({medium*100//len(diffs)}%)")
        print(f"    diff > 10 (grande):     {high:4d} ({high*100//len(diffs)}%)")

    except ImportError:
        print("  (PIL/numpy nao disponivel, pulando)")

    t1 = time.time()
    print(f"  (analise em {t1-t0:.1f}s)")

    # 4. WebP comparison (sample)
    print(f"\n[4] Comparacao JPEG vs WebP (amostra)")
    try:
        from PIL import Image
        sample_ids = [0, count//4, count//2, count*3//4, count-1]
        jpeg_total = 0
        webp_total = 0

        for fid in sample_ids:
            data = vh.get_frame_image(fid)
            jpeg_total += len(data)

            img = Image.open(io.BytesIO(data))
            buf = io.BytesIO()
            img.save(buf, format='WEBP', quality=80)
            webp_total += buf.tell()

        print(f"  {len(sample_ids)} frames amostrados:")
        print(f"  JPEG total: {jpeg_total/1024:.0f} KB")
        print(f"  WebP total: {webp_total/1024:.0f} KB")
        print(f"  Reducao:    {(1 - webp_total/jpeg_total)*100:.0f}%")
        est_webp_size = total * (webp_total / jpeg_total)
        print(f"  Estimativa VH inteiro com WebP: {est_webp_size/(1024*1024):.0f} MB")
    except ImportError:
        print("  (PIL nao disponivel)")

    # 5. SQLite overhead
    print(f"\n[5] Overhead do SQLite")
    file_size = Path(vh_path).stat().st_size
    audio_size = vh._conn.execute(
        "SELECT COALESCE(SUM(LENGTH(data)), 0) FROM audio"
    ).fetchone()[0]
    overhead = file_size - total - audio_size
    print(f"  Dados de frames:  {total/(1024*1024):.1f} MB")
    print(f"  Dados de audio:   {audio_size/(1024*1024):.1f} MB")
    print(f"  Overhead SQLite:  {overhead/(1024*1024):.1f} MB ({overhead*100//file_size}%)")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  RESUMO DE OTIMIZACOES POSSIVEIS")
    print(f"{'=' * 60}")
    print(f"  Tamanho atual:           {file_size/(1024*1024):.0f} MB")
    print(f"  Deduplicacao exata:     -{dup_bytes_saved/(1024*1024):.0f} MB")
    if 'est_near_dupe_bytes' in dir():
        print(f"  Skip near-duplicates:   ~-{est_near_dupe_bytes/(1024*1024):.0f} MB")
    if 'est_webp_size' in dir():
        print(f"  JPEG -> WebP:           ~-{(total - est_webp_size)/(1024*1024):.0f} MB")
    print(f"  Overhead SQLite:        -{overhead/(1024*1024):.0f} MB")

    vh.close()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 analyze.py <file.vh>")
        sys.exit(1)
    analyze(sys.argv[1])
