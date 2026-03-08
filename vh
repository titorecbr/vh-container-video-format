#!/usr/bin/env python3
"""
VH Format - Unified CLI

Commands:
  vh info     <file.vh>                          Show file info and metadata
  vh convert  <input.mp4> [output.vh]            Convert MP4 to VH
  vh play     <file.vh>                          Play VH file
  vh slice    <file.vh> -o out.vh -s N -e N      Extract frame range
  vh extract  <file.vh> -f N -o frame.jpg        Extract single frame
  vh annotate <file.vh> -f N -k KEY -v VALUE     Add annotation
  vh search   <file.vh> -k KEY [-v VALUE]        Search annotations
  vh export   <file.vh> -o output.mp4            Export to MP4
  vh thumb    <file.vh> -f N -o thumb.jpg         Extract thumbnail
  vh embed    <file.vh> -f N --model clip         Generate embedding
  vh viewer   <file.vh>                           Open visual frame browser
  vh analyze  <file.vh> --fn MODULE.func          Run AI function on all frames
  vh import-images <dir|file> -o out.vh           Import images into VH
  vh doc-add  <file.vh> <doc> [-f N] [-d DESC]   Attach document to frame
  vh doc-list <file.vh> [-f N]                    List attached documents
  vh doc-extract <file.vh> <ID> [-o out]          Extract document
  vh doc-del  <file.vh> <ID>                      Delete document
  vh generate "prompt" -o out.vh [--backend svd]  Generate AI video
"""

import sys
import os
import argparse
import json
import time
from pathlib import Path

# Resolve vhlib from same directory as this script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vhlib import VHFile


def cmd_info(args):
    """Show file info and metadata."""
    vh = VHFile(args.file, mode='r')
    s = vh.summary()

    print(f"{'=' * 55}")
    print(f"  VH Format - File Info")
    print(f"{'=' * 55}")
    print(f"  File:        {Path(args.file).name}")
    print(f"  Size:        {s['file_size_mb']:.1f} MB")
    print(f"  Version:     v{s['version']}")
    print(f"  Frames:      {s['frame_count']}")
    print(f"  Frame data:  {s['total_frame_data_mb']:.1f} MB")
    print(f"  Audio:       {s['audio_tracks']} track(s)")
    print(f"  Annotations: {s['annotations']}")
    print()

    meta = s['metadata']
    print("  Metadata:")
    for k, v in sorted(meta.items()):
        print(f"    {k}: {v}")

    if 'frame_stats' in s:
        print()
        print("  Frame types:")
        for ftype, info in s['frame_stats'].items():
            if isinstance(info, dict):
                print(f"    {ftype}: {info['count']} frames ({info['bytes']/(1024*1024):.1f} MB)")
            else:
                print(f"    {ftype}: {info}")

    # Thumbnails info
    thumb_count = vh._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='thumbnails'"
    ).fetchone()[0]
    if thumb_count:
        tc = vh._conn.execute("SELECT COUNT(*) FROM thumbnails").fetchone()[0]
        print(f"\n  Thumbnails:  {tc}")

    # Embeddings info
    embed_count = vh._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='embeddings'"
    ).fetchone()[0]
    if embed_count:
        ec = vh._conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        models = vh._conn.execute(
            "SELECT DISTINCT model FROM embeddings"
        ).fetchall()
        model_names = [r[0] for r in models]
        print(f"  Embeddings:  {ec} ({', '.join(model_names)})")

    # Documents info
    doc_table = vh._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchone()[0]
    if doc_table:
        dc = vh._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if dc:
            total_bytes = vh._conn.execute(
                "SELECT SUM(size_bytes) FROM documents"
            ).fetchone()[0] or 0
            print(f"  Documents:   {dc} ({total_bytes/(1024*1024):.1f} MB)")

    print(f"{'=' * 55}")
    vh.close()


def cmd_convert(args):
    """Convert MP4 to VH."""
    from convert_optimized import convert
    convert(
        args.input,
        args.output,
        quality=args.quality,
        fps=args.fps,
        use_delta=args.delta,
        keyframe_interval=args.keyframe_interval,
    )


def cmd_play(args):
    """Play VH file."""
    from vh_play import play
    play(args.file, args.player, args.start, args.end)


def cmd_slice(args):
    """Extract frame range to new VH file."""
    vh = VHFile(args.file, mode='r')
    print(f"Slicing frames {args.start}-{args.end} to {args.output}...")
    t0 = time.time()
    vh.slice_to_file(args.output, args.start, args.end)
    t1 = time.time()
    out_size = os.path.getsize(args.output) / (1024 * 1024)
    print(f"Done in {t1-t0:.1f}s. Output: {out_size:.1f} MB")
    vh.close()


def cmd_extract(args):
    """Extract single frame."""
    vh = VHFile(args.file, mode='r')
    output = args.output or f"frame_{args.frame}.jpg"
    if vh.export_frame(args.frame, output):
        size = os.path.getsize(output) / 1024
        print(f"Frame {args.frame} -> {output} ({size:.1f} KB)")
    else:
        print(f"Frame {args.frame} not found.")
        sys.exit(1)
    vh.close()


def cmd_annotate(args):
    """Add annotation to a frame."""
    vh = VHFile(args.file, mode='a')
    # Try to parse value as JSON, fallback to string
    try:
        value = json.loads(args.value)
    except (json.JSONDecodeError, TypeError):
        value = args.value
    vh.annotate(args.frame, args.key, value)
    vh.commit()
    print(f"Annotated frame {args.frame}: {args.key} = {value}")
    vh.close()


def cmd_search(args):
    """Search annotations."""
    vh = VHFile(args.file, mode='r')
    results = vh.search_annotations(args.key, args.value)
    if not results:
        print("No results found.")
    else:
        print(f"Found {len(results)} annotation(s):")
        for r in results:
            print(f"  Frame {r['frame_id']}: {r['key']} = {r['value']}")
    vh.close()


def cmd_edit_ann(args):
    """Edit an existing annotation."""
    vh = VHFile(args.file, mode='a')
    try:
        value = json.loads(args.value)
    except (json.JSONDecodeError, TypeError):
        value = args.value
    if vh.update_annotation(args.frame, args.key, value):
        vh.commit()
        print(f"Updated frame {args.frame}: {args.key} = {value}")
    else:
        print(f"Annotation not found: frame {args.frame}, key '{args.key}'")
        sys.exit(1)
    vh.close()


def cmd_del_ann(args):
    """Delete annotation(s) from a frame."""
    vh = VHFile(args.file, mode='a')
    if args.key:
        if vh.delete_annotation(args.frame, args.key):
            vh.commit()
            print(f"Deleted annotation: frame {args.frame}, key '{args.key}'")
        else:
            print(f"Annotation not found: frame {args.frame}, key '{args.key}'")
            sys.exit(1)
    else:
        count = vh.delete_annotations(args.frame)
        vh.commit()
        print(f"Deleted {count} annotation(s) from frame {args.frame}")
    vh.close()


def cmd_export(args):
    """Export VH to MP4."""
    vh = VHFile(args.file, mode='r')
    output = args.output or str(Path(args.file).with_suffix('.mp4'))
    fmt = args.format or 'mp4'

    print(f"Exporting to {output} ({fmt})...")
    t0 = time.time()

    if fmt == 'mp4':
        vh.export_to_mp4(output, fps=args.fps)
    else:
        print(f"Unsupported format: {fmt}")
        sys.exit(1)

    t1 = time.time()
    out_size = os.path.getsize(output) / (1024 * 1024)
    print(f"Done in {t1-t0:.1f}s. Output: {out_size:.1f} MB")
    vh.close()


def cmd_thumb(args):
    """Extract thumbnail for a frame."""
    vh = VHFile(args.file, mode='r')
    output = args.output or f"thumb_{args.frame}.jpg"

    data = vh.get_thumbnail(args.frame)
    if data:
        with open(output, 'wb') as f:
            f.write(data)
        print(f"Thumbnail frame {args.frame} -> {output} ({len(data)/1024:.1f} KB)")
    else:
        # Generate from full frame
        img_data = vh.get_frame_image(args.frame)
        if img_data:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(img_data))
            img.thumbnail((args.size, args.size))
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=75)
            with open(output, 'wb') as f:
                f.write(buf.getvalue())
            print(f"Generated thumbnail frame {args.frame} -> {output} ({buf.tell()/1024:.1f} KB)")
        else:
            print(f"Frame {args.frame} not found.")
            sys.exit(1)
    vh.close()


def cmd_embed(args):
    """Generate or show embedding for a frame."""
    vh = VHFile(args.file, mode='r')

    if args.show:
        emb = vh.get_embedding(args.frame, args.model)
        if emb:
            print(f"Frame {args.frame} ({args.model}): dim={emb['dimensions']}")
            print(f"  Vector (first 10): {emb['vector'][:10]}...")
        else:
            print(f"No embedding found for frame {args.frame} model={args.model}")
    else:
        print(f"Embedding generation requires a model runner. Use vhlib API:")
        print(f"  vh.add_embedding(frame_id, model_name, vector)")
    vh.close()


def cmd_viewer(args):
    """Open visual frame browser."""
    from vh_viewer import VHViewer
    VHViewer(args.file, args.start)


def cmd_analyze(args):
    """Run AI function on all frames."""
    import importlib

    # Parse function reference: module.func or module.submod.func
    parts = args.fn.rsplit('.', 1)
    if len(parts) != 2:
        print(f"Error: --fn must be module.function (got: {args.fn})")
        sys.exit(1)

    mod_name, func_name = parts
    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, func_name)
    except (ImportError, AttributeError) as e:
        print(f"Error loading {args.fn}: {e}")
        sys.exit(1)

    # Parse frame list
    frames = None
    if args.frames:
        if '-' in args.frames and ',' not in args.frames:
            s, e = args.frames.split('-')
            frames = list(range(int(s), int(e) + 1))
        else:
            frames = [int(x) for x in args.frames.split(',')]

    vh = VHFile(args.file, mode='a')
    print(f"Running {args.fn} on {len(frames) if frames else vh.frame_count} frames...")
    stats = vh.analyze(fn, frames=frames, batch_size=args.batch, key=args.key)
    print(f"Processed: {stats['processed']}, Errors: {stats['errors']}, "
          f"Time: {stats['elapsed']:.1f}s")
    vh.close()


def cmd_import_images(args):
    """Import images from a directory into a new VH file."""
    from PIL import Image
    import io
    import hashlib

    src = Path(args.input)
    if src.is_file():
        files = [src]
    elif src.is_dir():
        exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.gif'}
        files = sorted(f for f in src.iterdir() if f.suffix.lower() in exts)
    else:
        print(f"Error: '{args.input}' is not a file or directory.")
        sys.exit(1)

    if not files:
        print(f"No images found in '{args.input}'.")
        sys.exit(1)

    output = args.output or (str(src.with_suffix('.vh')) if src.is_dir() else str(src.with_suffix('.vh')))
    fps = args.fps
    duration_per = args.duration

    # If duration-per-image is set, calculate effective fps
    if duration_per:
        fps = 1.0 / duration_per

    print(f"Importing {len(files)} image(s) -> {output}")
    print(f"  FPS: {fps}, Duration per image: {1.0/fps:.2f}s")
    if args.resize:
        print(f"  Resize: {args.resize}")

    # Parse target size
    target_w, target_h = None, None
    if args.resize:
        parts = args.resize.lower().split('x')
        target_w, target_h = int(parts[0]), int(parts[1])

    t0 = time.time()
    vh = VHFile(output, mode='w')

    # Process first image to get dimensions
    first_img = Image.open(files[0])
    if target_w and target_h:
        w, h = target_w, target_h
    else:
        w, h = first_img.size
    first_img.close()

    vh.set_meta('width', w)
    vh.set_meta('height', h)
    vh.set_meta('fps', fps)
    vh.set_meta('source', 'import-images')
    vh.set_meta('source_files', len(files))

    prev_hash = None
    frames_added = 0
    refs_added = 0

    for i, filepath in enumerate(files):
        img = Image.open(filepath).convert('RGB')

        # Resize if needed
        if target_w and target_h:
            img = img.resize((target_w, target_h), Image.LANCZOS)
        elif i == 0:
            # Use first image dimensions as reference
            w, h = img.size

        # Encode to JPEG
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=args.quality)
        jpeg_bytes = buf.getvalue()
        img.close()

        ts_ms = i * (1000.0 / fps)

        # Deduplication
        frame_hash = hashlib.md5(jpeg_bytes).hexdigest()
        if frame_hash == prev_hash and i > 0:
            vh.add_frame_ref(i, ts_ms, ref_frame_id=i - 1)
            refs_added += 1
        else:
            vh.add_frame(i, ts_ms, jpeg_bytes, 'jpeg', w, h)
            frames_added += 1
        prev_hash = frame_hash

        # Auto-annotate with source filename
        if args.annotate_source:
            vh.annotate(i, 'source_file', filepath.name)

        # Progress
        if (i + 1) % 50 == 0 or i == len(files) - 1:
            print(f"  [{i+1}/{len(files)}] {filepath.name}")

        # Commit periodically
        if (i + 1) % 200 == 0:
            vh.commit()

    total_duration = len(files) / fps
    vh.set_meta('duration_s', total_duration)
    vh.set_meta('total_frames', len(files))
    vh.commit()
    vh.close()

    t1 = time.time()
    out_size = os.path.getsize(output) / (1024 * 1024)
    print(f"\nDone in {t1-t0:.1f}s")
    print(f"  Output:   {output} ({out_size:.1f} MB)")
    print(f"  Frames:   {frames_added} full + {refs_added} refs = {len(files)} total")
    print(f"  Duration: {total_duration:.1f}s @ {fps} fps")


def cmd_doc_add(args):
    """Attach a document to a frame."""
    vh = VHFile(args.file, mode='a')
    doc_id = vh.add_document(args.input, frame_id=args.frame,
                             description=args.desc)
    vh.commit()
    size = os.path.getsize(args.input) / 1024
    print(f"Attached '{Path(args.input).name}' ({size:.1f} KB) -> doc #{doc_id}"
          f"{f' (frame {args.frame})' if args.frame is not None else ' (global)'}")
    vh.close()


def cmd_doc_list(args):
    """List documents in VH file."""
    vh = VHFile(args.file, mode='r')
    docs = vh.list_documents(frame_id=args.frame)
    if not docs:
        print("No documents found.")
    else:
        print(f"{'ID':>4}  {'Frame':>7}  {'Size':>8}  {'Type':>20}  {'Filename'}")
        print(f"{'─'*4}  {'─'*7}  {'─'*8}  {'─'*20}  {'─'*30}")
        for d in docs:
            fid = str(d['frame_id']) if d['frame_id'] is not None else 'global'
            sz = f"{d['size_bytes']/1024:.1f}KB"
            print(f"{d['id']:>4}  {fid:>7}  {sz:>8}  {d['mime_type']:>20}  {d['filename']}")
            if d['description']:
                print(f"      └─ {d['description']}")
    vh.close()


def cmd_doc_extract(args):
    """Extract a document from VH file."""
    vh = VHFile(args.file, mode='r')
    doc = vh.get_document(args.id)
    if not doc:
        print(f"Document #{args.id} not found.")
        sys.exit(1)
    output = args.output or doc['filename']
    Path(output).write_bytes(doc['data'])
    print(f"Extracted '{doc['filename']}' ({doc['size_bytes']/1024:.1f} KB) -> {output}")
    vh.close()


def cmd_doc_del(args):
    """Delete a document from VH file."""
    vh = VHFile(args.file, mode='a')
    doc = vh.get_document(args.id)
    if not doc:
        print(f"Document #{args.id} not found.")
        sys.exit(1)
    vh.delete_document(args.id)
    vh.commit()
    print(f"Deleted document #{args.id} ('{doc['filename']}')")
    vh.close()


def cmd_generate(args):
    """Generate video from AI model and save as VH."""
    from PIL import Image
    import io

    if not args.prompt and not args.image:
        print("Error: provide a prompt, --image, or both.")
        sys.exit(1)

    # Lazy import — only loads torch/diffusers when actually used
    from vh_video_container.generate import get_backend
    from vh_video_container.generate.base import GenerateRequest

    backend = get_backend(args.backend)

    # Load conditioning image if provided
    cond_image = None
    if args.image:
        cond_image = Image.open(args.image).convert('RGB')

    # Build extra options (for API backends like Kling)
    extra = {}
    if hasattr(args, 'model') and args.model:
        extra['model'] = args.model
    if hasattr(args, 'mode') and args.mode:
        extra['mode'] = args.mode
    if hasattr(args, 'duration') and args.duration:
        extra['duration'] = args.duration
    if hasattr(args, 'negative_prompt') and args.negative_prompt:
        extra['negative_prompt'] = args.negative_prompt
    if hasattr(args, 'aspect_ratio') and args.aspect_ratio:
        extra['aspect_ratio'] = args.aspect_ratio

    # Build request
    request = GenerateRequest(
        prompt=args.prompt,
        image=cond_image,
        num_frames=args.num_frames,
        width=args.width,
        height=args.height,
        fps=args.fps,
        seed=args.seed,
        extra=extra,
    )

    print(f"Backend: {backend.name()}")
    print(f"Prompt: {args.prompt or '(image only)'}")
    if extra.get('model'):
        print(f"Model: {extra['model']}")
    if extra.get('mode'):
        print(f"Mode: {extra['mode']}")
    if extra.get('duration'):
        print(f"Duration: {extra['duration']}s per generation")
    print(f"Chains: {args.chains}")

    vh = VHFile(args.output, mode='w')
    vh.set_meta('width', args.width)
    vh.set_meta('height', args.height)
    vh.set_meta('fps', args.fps)
    vh.set_meta('source', f'generate:{backend.name()}')
    if args.prompt:
        vh.set_meta('prompt', args.prompt)
    if args.image:
        vh.set_meta('conditioning_image', Path(args.image).name)

    frame_offset = 0
    t0 = time.time()

    for chain_idx in range(args.chains):
        if args.chains > 1:
            print(f"\n--- Chain {chain_idx + 1}/{args.chains} ---")

        result = backend.generate(request)

        for i, pil_img in enumerate(result.frames):
            frame_id = frame_offset + i
            ts_ms = frame_id * (1000.0 / args.fps)
            buf = io.BytesIO()
            pil_img.save(buf, format='JPEG', quality=args.quality)
            vh.add_frame(frame_id, ts_ms, buf.getvalue(), 'jpeg',
                         pil_img.width, pil_img.height)

        frame_offset += len(result.frames)

        # Chain: use last frame as conditioning for next generation
        if chain_idx < args.chains - 1:
            request.image = result.frames[-1]

        vh.commit()

    total_frames = frame_offset
    elapsed = time.time() - t0
    vh.set_meta('total_frames', total_frames)
    vh.set_meta('duration_s', total_frames / args.fps)
    vh.set_meta('generation_backend', backend.name())
    if result.seed:
        vh.set_meta('seed', result.seed)
    vh.commit()
    backend.cleanup()
    vh.close()

    out_size = os.path.getsize(args.output) / (1024 * 1024)
    print(f"\nDone in {elapsed:.1f}s: {total_frames} frames, "
          f"{total_frames / args.fps:.1f}s @ {args.fps}fps")
    print(f"Output: {args.output} ({out_size:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(
        prog='vh',
        description='VH Format - Video container optimized for AI workloads'
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # info
    p = subparsers.add_parser('info', help='Show file info')
    p.add_argument('file', help='Input .vh file')

    # convert
    p = subparsers.add_parser('convert', help='Convert MP4 to VH')
    p.add_argument('input', help='Input video file')
    p.add_argument('output', nargs='?', help='Output .vh file')
    p.add_argument('--quality', type=int, default=10, help='JPEG quality 2-31 (default: 10)')
    p.add_argument('--fps', type=float, help='Target FPS')
    p.add_argument('--delta', action='store_true', help='Enable delta compression')
    p.add_argument('--keyframe-interval', type=int, default=24, help='Keyframe interval (default: 24)')

    # play
    p = subparsers.add_parser('play', help='Play VH file')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('--player', help='Player: vlc, ffplay, mpv')
    p.add_argument('--start', type=int, help='Start frame')
    p.add_argument('--end', type=int, help='End frame')

    # slice
    p = subparsers.add_parser('slice', help='Extract frame range')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-o', '--output', required=True, help='Output .vh file')
    p.add_argument('-s', '--start', type=int, required=True, help='Start frame')
    p.add_argument('-e', '--end', type=int, required=True, help='End frame')

    # extract
    p = subparsers.add_parser('extract', help='Extract single frame')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-f', '--frame', type=int, required=True, help='Frame ID')
    p.add_argument('-o', '--output', help='Output file')

    # annotate
    p = subparsers.add_parser('annotate', help='Add annotation')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-f', '--frame', type=int, required=True, help='Frame ID')
    p.add_argument('-k', '--key', required=True, help='Annotation key')
    p.add_argument('-v', '--value', required=True, help='Annotation value')

    # search
    p = subparsers.add_parser('search', help='Search annotations')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-k', '--key', required=True, help='Annotation key')
    p.add_argument('-v', '--value', default=None, help='Value filter (substring)')

    # edit-ann
    p = subparsers.add_parser('edit-ann', help='Edit an annotation')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-f', '--frame', type=int, required=True, help='Frame ID')
    p.add_argument('-k', '--key', required=True, help='Annotation key')
    p.add_argument('-v', '--value', required=True, help='New value')

    # del-ann
    p = subparsers.add_parser('del-ann', help='Delete annotation(s)')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-f', '--frame', type=int, required=True, help='Frame ID')
    p.add_argument('-k', '--key', default=None, help='Annotation key (omit to delete all from frame)')

    # export
    p = subparsers.add_parser('export', help='Export to MP4')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-o', '--output', help='Output file')
    p.add_argument('--format', default='mp4', help='Output format (default: mp4)')
    p.add_argument('--fps', type=float, help='Output FPS')

    # thumb
    p = subparsers.add_parser('thumb', help='Extract/generate thumbnail')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-f', '--frame', type=int, required=True, help='Frame ID')
    p.add_argument('-o', '--output', help='Output file')
    p.add_argument('--size', type=int, default=320, help='Thumbnail max size (default: 320)')

    # embed
    p = subparsers.add_parser('embed', help='Show/manage embeddings')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-f', '--frame', type=int, required=True, help='Frame ID')
    p.add_argument('--model', default='clip', help='Model name (default: clip)')
    p.add_argument('--show', action='store_true', help='Show existing embedding')

    # viewer
    p = subparsers.add_parser('viewer', help='Open visual frame browser (tkinter)')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('--start', type=int, default=0, help='Start frame')

    # analyze
    p = subparsers.add_parser('analyze', help='Run AI function on all frames')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('--fn', required=True,
                   help='Python function as module.func (receives image bytes, returns value)')
    p.add_argument('--key', default='ai_result', help='Annotation key (default: ai_result)')
    p.add_argument('--batch', type=int, default=1, help='Batch size (default: 1)')
    p.add_argument('--frames', help='Frame range: START-END or START,END,... (default: all)')

    # import-images
    p = subparsers.add_parser('import-images', help='Import images into a VH file')
    p.add_argument('input', help='Image file or directory of images')
    p.add_argument('-o', '--output', help='Output .vh file')
    p.add_argument('--fps', type=float, default=24, help='Frames per second (default: 24)')
    p.add_argument('--duration', type=float, default=None,
                   help='Duration per image in seconds (overrides --fps)')
    p.add_argument('--quality', type=int, default=90, help='JPEG quality 1-100 (default: 90)')
    p.add_argument('--resize', default=None, help='Resize to WIDTHxHEIGHT (e.g. 1920x1080)')
    p.add_argument('--annotate-source', action='store_true',
                   help='Annotate each frame with its source filename')

    # generate
    p = subparsers.add_parser('generate', help='Generate AI video')
    p.add_argument('prompt', nargs='?', default=None, help='Text prompt for generation')
    p.add_argument('-o', '--output', required=True, help='Output .vh file')
    p.add_argument('--image', default=None, help='Conditioning image (img2vid)')
    p.add_argument('--backend', default='svd', help='Generation backend (default: svd)')
    p.add_argument('--num-frames', type=int, default=25, help='Frames per generation (default: 25)')
    p.add_argument('--width', type=int, default=1024, help='Width (default: 1024)')
    p.add_argument('--height', type=int, default=576, help='Height (default: 576)')
    p.add_argument('--fps', type=int, default=7, help='Output FPS (default: 7)')
    p.add_argument('--seed', type=int, default=None, help='Random seed')
    p.add_argument('--quality', type=int, default=90, help='JPEG quality (default: 90)')
    p.add_argument('--chains', type=int, default=1,
                   help='Chain multiple generations for longer video (default: 1)')
    # Kling-specific options (passed via extra dict)
    p.add_argument('--model', default=None,
                   help='API model name (e.g. kling-v2-master, kling-v2-1-master)')
    p.add_argument('--mode', default=None, choices=['std', 'pro'],
                   help='Quality mode: std (720p) or pro (1080p)')
    p.add_argument('--duration', type=int, default=None, choices=[5, 10],
                   help='Video duration per generation in seconds (5 or 10)')
    p.add_argument('--negative-prompt', default=None,
                   help='Negative prompt (what to avoid)')
    p.add_argument('--aspect-ratio', default=None,
                   help='Aspect ratio (16:9, 9:16, 1:1, 4:3, etc.)')

    # doc-add
    p = subparsers.add_parser('doc-add', help='Attach a document to a frame')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('input', help='Document file to attach')
    p.add_argument('-f', '--frame', type=int, default=None, help='Frame ID (omit for global)')
    p.add_argument('-d', '--desc', default=None, help='Description')

    # doc-list
    p = subparsers.add_parser('doc-list', help='List attached documents')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('-f', '--frame', type=int, default=None, help='Filter by frame ID')

    # doc-extract
    p = subparsers.add_parser('doc-extract', help='Extract a document')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('id', type=int, help='Document ID')
    p.add_argument('-o', '--output', help='Output file (default: original filename)')

    # doc-del
    p = subparsers.add_parser('doc-del', help='Delete a document')
    p.add_argument('file', help='Input .vh file')
    p.add_argument('id', type=int, help='Document ID')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        'info': cmd_info,
        'convert': cmd_convert,
        'play': cmd_play,
        'slice': cmd_slice,
        'extract': cmd_extract,
        'annotate': cmd_annotate,
        'search': cmd_search,
        'edit-ann': cmd_edit_ann,
        'del-ann': cmd_del_ann,
        'export': cmd_export,
        'thumb': cmd_thumb,
        'embed': cmd_embed,
        'viewer': cmd_viewer,
        'analyze': cmd_analyze,
        'import-images': cmd_import_images,
        'generate': cmd_generate,
        'doc-add': cmd_doc_add,
        'doc-list': cmd_doc_list,
        'doc-extract': cmd_doc_extract,
        'doc-del': cmd_doc_del,
    }

    commands[args.command](args)


if __name__ == '__main__':
    main()
