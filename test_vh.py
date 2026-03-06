#!/usr/bin/env python3
"""Testes do formato .vh - valida v1 e v2."""

import sys
import os
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from vhlib import VHFile

BASE = Path(__file__).parent
passed = 0
failed = 0


def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def run_common_tests(vh_path, label):
    """Tests that apply to both v1 and v2."""
    global passed, failed

    print(f"\n{'=' * 60}")
    print(f"  TESTES - {label}")
    print(f"{'=' * 60}")

    # [1] Open
    print(f"\n[1] Abrir arquivo .vh")
    vh = VHFile(vh_path, mode='r')
    test("Abrir em modo leitura", vh is not None)
    test(f"Versao detectada = {vh.version}", vh.version in (1, 2))

    # [2] Metadata
    print(f"\n[2] Metadados")
    meta = vh.get_all_meta()
    test("Metadados existem", len(meta) > 0)
    test("Tem width", meta.get('width') == 2560, f"got {meta.get('width')}")
    test("Tem height", meta.get('height') == 1340, f"got {meta.get('height')}")
    test("Tem fps", meta.get('fps') == 24.0, f"got {meta.get('fps')}")
    test("Tem duration_s", meta.get('duration_s', 0) > 0)
    test("Tem frame_count", meta.get('frame_count', 0) > 0)

    # [3] Frame count
    print(f"\n[3] Contagem de frames")
    count = vh.frame_count
    test("frame_count > 0", count > 0, f"got {count}")
    test("frame_count coerente", count == meta.get('frame_count', -1),
         f"{count} vs {meta.get('frame_count')}")

    # [4] Frame access
    print(f"\n[4] Acesso a frames individuais")
    frame0 = vh.get_frame(0)
    test("Frame 0 existe", frame0 is not None)
    test("Frame 0 tem dados", len(frame0['data']) > 0)
    test("Frame 0 timestamp = 0", frame0['timestamp_ms'] == 0.0)
    test("Frame 0 width", frame0['width'] == 2560)
    test("Frame 0 height", frame0['height'] == 1340)

    # Check image is valid (JPEG or WebP)
    is_jpeg = frame0['data'][:2] == b'\xff\xd8'
    is_webp = frame0['data'][:4] == b'RIFF' and frame0['data'][8:12] == b'WEBP'
    test("Frame 0 eh imagem valida (JPEG ou WebP)", is_jpeg or is_webp,
         f"magic={frame0['data'][:4].hex()}")

    last = vh.get_frame(count - 1)
    test("Ultimo frame existe", last is not None)
    test("Ultimo frame tem dados", len(last['data']) > 0)

    mid_id = count // 2
    mid = vh.get_frame(mid_id)
    test(f"Frame meio ({mid_id}) existe", mid is not None)
    test("Timestamp coerente", 0 < mid['timestamp_ms'] < meta.get('duration_s', 0) * 1000)

    ghost = vh.get_frame(999999)
    test("Frame inexistente = None", ghost is None)

    # [5] get_frame_image
    print(f"\n[5] get_frame_image")
    img = vh.get_frame_image(0)
    test("Retorna bytes", isinstance(img, bytes))
    test("Consistente com get_frame", img == frame0['data'])
    test("Inexistente = None", vh.get_frame_image(999999) is None)

    # [6] Range
    print(f"\n[6] Range de frames")
    rng = vh.get_frames_range(100, 110)
    test("Range 100-110 retorna 11", len(rng) == 11, f"got {len(rng)}")
    test("Ordenados", all(rng[i]['frame_id'] <= rng[i+1]['frame_id'] for i in range(len(rng)-1)))

    # [7] Timestamp search
    print(f"\n[7] Busca por timestamp")
    fps = meta.get('fps', 24)
    tf = vh.get_frames_by_time(10000, 11000)
    test(f"~{int(fps)} frames em 1s", abs(len(tf) - int(fps)) <= 2, f"got {len(tf)}")

    # [8] Performance
    print(f"\n[8] Performance - acesso aleatorio")
    import random
    random.seed(42)
    test_ids = [random.randint(0, count - 1) for _ in range(100)]

    t0 = time.time()
    for fid in test_ids:
        vh.get_frame_image(fid)
    elapsed = (time.time() - t0) * 1000
    per_frame = elapsed / 100

    test("100 frames < 2000ms", elapsed < 2000, f"got {elapsed:.0f}ms")
    print(f"         ({elapsed:.0f}ms total, {per_frame:.2f}ms/frame)")

    vh.close()

    # [9] Audio
    print(f"\n[9] Audio")
    vh = VHFile(vh_path, mode='r')
    audio = vh.get_audio()
    test("Audio existe", audio is not None)
    if audio:
        test("Tem dados", len(audio['data']) > 0)
        test("Codec opus", audio['codec'] == 'opus')
        test("Channels > 0", audio['channels'] > 0)
        test("Duration > 0", audio['duration_ms'] > 0)
        aout = str(BASE / "test_audio.opus")
        ok = vh.export_audio(aout)
        test("Export OK", ok and os.path.exists(aout))
        if os.path.exists(aout):
            os.unlink(aout)
    vh.close()

    # [10] Annotations
    print(f"\n[10] Annotations")
    vh = VHFile(vh_path, mode='a')
    vh._conn.execute("DELETE FROM annotations")
    vh.commit()

    vh.annotate(0, 'label', 'inicio')
    vh.annotate(0, 'objects', ['janela', 'taskbar'])
    vh.annotate(100, 'label', 'cena_2')
    vh.annotate(100, 'score', 0.95)
    vh.annotate(200, 'label', 'cena_3')
    vh.commit()

    ann0 = vh.get_annotations(0)
    test("Frame 0 tem 2 chaves", len(ann0) == 2, f"got {len(ann0)}")
    test("Label = 'inicio'", ann0.get('label') == 'inicio')
    test("Objects eh lista", isinstance(ann0.get('objects'), list))
    test("Score 100 = 0.95", vh.get_annotations(100).get('score') == 0.95)

    labels = vh.search_annotations('label')
    test("Busca 'label' = 3", len(labels) == 3, f"got {len(labels)}")
    test("Busca 'cena' = 2", len(vh.search_annotations('label', 'cena')) == 2)
    test("Frames com label", vh.search_frames_with_annotation('label') == [0, 100, 200])
    test("Sem annotation = {}", vh.get_annotations(5000) == {})
    vh.close()

    # [11] Slice
    print(f"\n[11] Slice")
    vh = VHFile(vh_path, mode='r')
    sp = str(BASE / "test_slice.vh")

    t0 = time.time()
    vh.slice_to_file(sp, 0, 99)
    st = (time.time() - t0) * 1000

    test("Slice criado", os.path.exists(sp))
    svh = VHFile(sp, mode='r')
    test("100 frames", svh.frame_count == 100, f"got {svh.frame_count}")
    sf0 = svh.get_frame(0)
    test("Frame valido", sf0 is not None and len(sf0['data']) > 0)
    test("Frame 0 == original", sf0['data'] == vh.get_frame_image(0))
    svh.close()
    os.unlink(sp)
    vh.close()
    print(f"         (slice em {st:.0f}ms)")

    # [12] Export frame
    print(f"\n[12] Export frame")
    vh = VHFile(vh_path, mode='r')
    ep = str(BASE / "test_export.img")
    ok = vh.export_frame(500, ep)
    test("Export OK", ok and os.path.exists(ep))
    if os.path.exists(ep):
        with open(ep, 'rb') as f:
            data = f.read()
        test("Dados validos", len(data) > 0)
        os.unlink(ep)
    test("Inexistente = False", vh.export_frame(999999, ep) is False)
    vh.close()

    # [13] Modos
    print(f"\n[13] Modos de abertura")
    try:
        VHFile("/tmp/nao_existe.vh", mode='r')
        test("Inexistente levanta erro", False)
    except FileNotFoundError:
        test("Inexistente levanta erro", True)
    try:
        VHFile(vh_path, mode='x')
        test("Modo invalido levanta erro", False)
    except ValueError:
        test("Modo invalido levanta erro", True)

    # [14] Summary
    print(f"\n[14] Summary")
    vh = VHFile(vh_path, mode='r')
    s = vh.summary()
    test("Summary tem file", 'file' in s)
    test("Summary tem frame_count", s['frame_count'] > 0)
    test("Summary tem version", 'version' in s)
    vh.close()


def run_v2_specific_tests(vh_path, label):
    """Tests specific to v2 features."""
    global passed, failed

    print(f"\n{'=' * 60}")
    print(f"  TESTES v2 - {label}")
    print(f"{'=' * 60}")

    vh = VHFile(vh_path, mode='r')

    # Frame stats
    print(f"\n[v2.1] Frame stats")
    stats = vh.get_frame_stats()
    test("Stats tem 'full'", 'full' in stats, f"keys={list(stats.keys())}")

    has_refs = 'ref' in stats and stats['ref']['count'] > 0
    test("Tem frames ref (dedup)", has_refs, f"stats={stats}")

    if has_refs:
        print(f"         full={stats['full']['count']} ref={stats['ref']['count']}")

    # Ref frames resolve correctly
    print(f"\n[v2.2] Ref frames resolvem corretamente")
    if has_refs:
        ref_row = vh._conn.execute(
            "SELECT frame_id, ref_frame_id FROM frames WHERE frame_type='ref' LIMIT 1"
        ).fetchone()
        if ref_row:
            ref_id, target_id = ref_row
            ref_data = vh.get_frame_image(ref_id)
            target_data = vh.get_frame_image(target_id)
            test(f"Ref frame {ref_id} == target {target_id}", ref_data == target_data)
            test("Ref data nao eh None", ref_data is not None)
    else:
        test("(sem refs para testar)", True)

    # Delta frames (if present)
    print(f"\n[v2.3] Delta frames")
    has_deltas = 'delta' in stats and stats['delta']['count'] > 0
    if has_deltas:
        test("Tem frames delta", True)
        delta_row = vh._conn.execute(
            "SELECT frame_id FROM frames WHERE frame_type='delta' LIMIT 1"
        ).fetchone()
        if delta_row:
            delta_id = delta_row[0]
            pixels = vh.get_frame_pixels(delta_id)
            test(f"Delta frame {delta_id} decodifica", pixels is not None)
            test("Shape correto", pixels.shape == (1340, 2560, 3),
                 f"got {pixels.shape}" if pixels is not None else "None")
            img_data = vh.get_frame_image(delta_id)
            test("get_frame_image retorna bytes", isinstance(img_data, bytes) and len(img_data) > 0)
    else:
        test("(sem deltas neste arquivo)", True)

    # get_frame_pixels
    print(f"\n[v2.4] get_frame_pixels")
    pixels0 = vh.get_frame_pixels(0)
    test("Frame 0 como pixels", pixels0 is not None)
    if pixels0 is not None:
        test("Shape (1340, 2560, 3)", pixels0.shape == (1340, 2560, 3),
             f"got {pixels0.shape}")
        test("Dtype uint8", pixels0.dtype.name == 'uint8')

    test("Inexistente = None", vh.get_frame_pixels(999999) is None)

    # Format version metadata
    print(f"\n[v2.5] Metadata v2")
    test("format_version = 2", vh.get_meta('format_version') == 2)

    vh.close()


def run_player_test(vh_path, label):
    """Test that the player can produce playable output."""
    global passed, failed
    import subprocess
    import tempfile
    import shutil

    print(f"\n{'=' * 60}")
    print(f"  TESTE PLAYER - {label}")
    print(f"{'=' * 60}")

    vh = VHFile(vh_path, mode='r')
    meta = vh.get_all_meta()
    fps = meta.get('fps', 24)

    tmpdir = tempfile.mkdtemp(prefix='vh_test_play_')
    try:
        # Extract first 48 frames (2 seconds)
        print(f"\n[play.1] Extrair 48 frames")
        t0 = time.time()
        for i in range(min(48, vh.frame_count)):
            data = vh.get_frame_image(i)
            ext = 'jpg' if data[:2] == b'\xff\xd8' else 'webp'
            with open(os.path.join(tmpdir, f'frame_{i:07d}.{ext}'), 'wb') as f:
                f.write(data)
        elapsed = (time.time() - t0) * 1000
        test(f"48 frames extraidos em {elapsed:.0f}ms", elapsed < 10000)

        # Mux with ffmpeg
        print(f"\n[play.2] Mux com ffmpeg")
        avi_path = os.path.join(tmpdir, 'test.avi')
        cmd = [
            'ffmpeg', '-y', '-v', 'warning',
            '-framerate', str(fps),
            '-i', os.path.join(tmpdir, f'frame_%07d.{ext}'),
            '-c:v', 'copy', '-t', '2', avi_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        test("ffmpeg mux OK", result.returncode == 0 and os.path.exists(avi_path),
             f"stderr={result.stderr[:200]}" if result.returncode != 0 else "")

        if os.path.exists(avi_path):
            avi_size = os.path.getsize(avi_path)
            test("AVI tem dados", avi_size > 0, f"size={avi_size}")

            # Verify with ffprobe
            probe = subprocess.run(
                ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                 '-show_streams', avi_path],
                capture_output=True, text=True
            )
            if probe.returncode == 0:
                streams = json.loads(probe.stdout).get('streams', [])
                test("AVI tem stream de video", any(s['codec_type'] == 'video' for s in streams))

    finally:
        vh.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    global passed, failed

    files_to_test = []

    # v1
    v1_path = str(BASE / "teste.vh")
    if os.path.exists(v1_path):
        files_to_test.append((v1_path, "v1 (JPEG)", False))

    # v2 fast
    v2_path = str(BASE / "teste_v2.vh")
    if os.path.exists(v2_path):
        files_to_test.append((v2_path, "v2 FAST (JPEG + dedup)", True))

    # v2 delta
    v2d_path = str(BASE / "teste_v2_delta.vh")
    if os.path.exists(v2d_path):
        files_to_test.append((v2d_path, "v2 DELTA (JPEG + dedup + delta)", True))

    if not files_to_test:
        print("Nenhum arquivo .vh encontrado. Rode convert.py primeiro.")
        sys.exit(1)

    for path, label, is_v2 in files_to_test:
        run_common_tests(path, label)
        if is_v2:
            run_v2_specific_tests(path, label)
        run_player_test(path, label)

    # [extra] Create from scratch
    print(f"\n{'=' * 60}")
    print(f"  TESTES EXTRAS")
    print(f"{'=' * 60}")

    print(f"\n[extra.1] Criar .vh do zero")
    new_path = str(BASE / "test_new.vh")
    with VHFile(new_path, mode='w') as nvh:
        nvh.set_meta('test', True)
        nvh.set_meta('width', 1)
        nvh.set_meta('height', 1)
        nvh.set_meta('fps', 30)
        fake = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9'
        nvh.add_frame(0, 0.0, fake, 'jpeg', 1, 1)
        nvh.add_frame(1, 33.3, fake, 'jpeg', 1, 1)
        nvh.add_frame_ref(2, 66.6, 0, 1, 1)
        nvh.commit()

    with VHFile(new_path, mode='r') as nvh:
        test("Criado com 3 frames", nvh.frame_count == 3)
        test("Meta test = True", nvh.get_meta('test') is True)
        test("Frame 0 legivel", nvh.get_frame_image(0) is not None)
        test("Ref frame 2 == frame 0", nvh.get_frame_image(2) == nvh.get_frame_image(0))
        st = nvh.get_frame_stats()
        test("Stats: 2 full + 1 ref", st.get('full', {}).get('count') == 2 and
             st.get('ref', {}).get('count') == 1,
             f"got {st}")
    os.unlink(new_path)

    # Final
    total = passed + failed
    print(f"\n{'=' * 60}")
    if failed == 0:
        print(f"  TODOS OS TESTES PASSARAM: {passed}/{total}")
    else:
        print(f"  RESULTADO: {passed}/{total} passaram, {failed} falharam")
    print(f"{'=' * 60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == '__main__':
    main()
