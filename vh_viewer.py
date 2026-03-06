#!/usr/bin/env python3
"""
VH Viewer - High-performance visual player for .vh files.

Keybindings:
  Space        Play/Pause        Left/Right   -1/+1 frame
  Shift+L/R    -10/+10           Ctrl+L/R     -100/+100
  Home/End     First/Last        Ctrl+G       Go to frame
  +/-          Zoom in/out       M            Mute/unmute
  F            Fullscreen        A            Add annotation
"""

import sys
import os
import io
import math
import time
import argparse
import threading
import subprocess
import tempfile
import collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import tkinter as tk
    from tkinter import simpledialog
except ImportError:
    print("tkinter required: apt-get install python3-tk")
    sys.exit(1)

try:
    from PIL import Image, ImageTk, ImageDraw
except ImportError:
    print("Pillow required: pip install Pillow")
    sys.exit(1)

from vhlib import VHFile

# ─────────────────────────────────────────────────────────
# Design System — Liquid
# ─────────────────────────────────────────────────────────

C = {
    'bg':              '#0a0a0e',
    'surface':         '#12121a',
    'surface_2':       '#1a1a24',
    'surface_3':       '#222230',
    'border':          '#2a2a3a',
    'border_subtle':   '#1a1a28',
    'accent':          '#00b4d8',
    'accent_soft':     '#0090b0',
    'accent_hover':    '#33c9e8',
    'accent_glow':     '#0d2530',
    'accent_dim':      '#004d60',
    'accent_surface':  '#0a1e28',
    'text':            '#d0d0d8',
    'text_dim':        '#585868',
    'text_muted':      '#383848',
    'text_bright':     '#f0f0f8',
    'annotation':      '#4ade80',
    'annotation_dim':  '#1a3a24',
    'timeline_bg':     '#2a2a38',
    'timeline_played': '#00b4d8',
    'timeline_glow':   '#0a3040',
    'handle':          '#f0f0f8',
    'handle_ring':     '#00b4d8',
    'btn_hover':       '#22222e',
    'play_bg':         '#00b4d8',
    'play_hover':      '#22c4e8',
    'play_fg':         '#0a0a0e',
    'mute_off':        '#ef4444',
    'brand':           '#00b4d8',
    'osd_bg':          '#1a1a24',
    'osd_fg':          '#f0f0f8',
    'video_bg':        '#000000',
}

FONT_UI = ('Noto Sans', 10)
FONT_UI_BOLD = ('Noto Sans', 10, 'bold')
FONT_MONO = ('JetBrains Mono', 10)
FONT_MONO_SM = ('JetBrains Mono', 9)
FONT_MONO_XS = ('JetBrains Mono', 8)
FONT_TIME = ('JetBrains Mono', 12)
FONT_ICON = ('Noto Sans', 12)
FONT_ICON_LG = ('Noto Sans', 16)
FONT_BRAND = ('Noto Sans', 13, 'bold')
FONT_OSD = ('Noto Sans', 36)
FONT_BADGE = ('JetBrains Mono', 8, 'bold')


def fmt_time(seconds):
    m = int(seconds) // 60
    s = seconds - m * 60
    return f"{m:02d}:{s:05.2f}"


# ─────────────────────────────────────────────────────────
# PIL Rendering Helpers (anti-aliased via 2x supersampling)
# ─────────────────────────────────────────────────────────

_SS = 2  # supersample factor


def hex_rgb(h):
    """Convert '#RRGGBB' to (R, G, B) tuple."""
    h = h.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def blend_rgb(fg, bg, alpha):
    """Blend fg over bg with alpha 0.0-1.0."""
    return tuple(int(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


def make_icon(size, draw_fn, fg_rgb, bg_rgb):
    """Render an icon with PIL at 4x supersample, return PhotoImage."""
    S = 4
    ss = size * S
    img = Image.new('RGB', (ss, ss), bg_rgb)
    draw = ImageDraw.Draw(img)
    draw_fn(draw, ss, fg_rgb)
    img = img.resize((size, size), Image.LANCZOS)
    return ImageTk.PhotoImage(img)


def _icon_volume(draw, s, fg):
    """Speaker icon."""
    m = s // 4
    # Speaker body
    bw = s // 6
    draw.rectangle([m, s * 3 // 8, m + bw, s * 5 // 8], fill=fg)
    # Speaker cone
    draw.polygon([m + bw, s * 3 // 8, s // 2, s // 4,
                  s // 2, s * 3 // 4, m + bw, s * 5 // 8], fill=fg)
    # Sound waves
    r1 = s // 5
    cx = s // 2 + s // 10
    cy = s // 2
    for i, r in enumerate([r1, r1 + s // 8]):
        lw = max(s // 20, 2)
        draw.arc([cx - r, cy - r, cx + r, cy + r], -45, 45, fill=fg, width=lw)


def _icon_volume_off(draw, s, fg):
    """Muted speaker icon."""
    m = s // 4
    bw = s // 6
    draw.rectangle([m, s * 3 // 8, m + bw, s * 5 // 8], fill=fg)
    draw.polygon([m + bw, s * 3 // 8, s // 2, s // 4,
                  s // 2, s * 3 // 4, m + bw, s * 5 // 8], fill=fg)
    # X mark
    lw = max(s // 16, 2)
    x0, x1 = s * 5 // 8, s * 3 // 4
    y0, y1 = s * 3 // 8, s * 5 // 8
    draw.line([x0, y0, x1, y1], fill=fg, width=lw)
    draw.line([x0, y1, x1, y0], fill=fg, width=lw)


def _icon_zoom_in(draw, s, fg):
    """Magnifying glass + icon."""
    lw = max(s // 14, 2)
    r = s * 3 // 10
    cx, cy = s * 2 // 5, s * 2 // 5
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fg, width=lw)
    # Handle
    hx = cx + int(r * 0.7)
    hy = cy + int(r * 0.7)
    draw.line([hx, hy, s * 3 // 4, s * 3 // 4], fill=fg, width=lw)
    # Plus
    pl = r // 2
    draw.line([cx - pl, cy, cx + pl, cy], fill=fg, width=lw)
    draw.line([cx, cy - pl, cx, cy + pl], fill=fg, width=lw)


def _icon_zoom_out(draw, s, fg):
    """Magnifying glass - icon."""
    lw = max(s // 14, 2)
    r = s * 3 // 10
    cx, cy = s * 2 // 5, s * 2 // 5
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fg, width=lw)
    hx = cx + int(r * 0.7)
    hy = cy + int(r * 0.7)
    draw.line([hx, hy, s * 3 // 4, s * 3 // 4], fill=fg, width=lw)
    pl = r // 2
    draw.line([cx - pl, cy, cx + pl, cy], fill=fg, width=lw)


def _icon_tag(draw, s, fg):
    """Tag/annotation icon."""
    lw = max(s // 14, 2)
    m = s // 4
    draw.rounded_rectangle([m, m, s - m, s - m], radius=s // 8,
                            outline=fg, width=lw)
    # Lines inside
    lm = m + s // 8
    rm = s - m - s // 8
    for y in [s * 2 // 5, s // 2, s * 3 // 5]:
        draw.line([lm, y, rm, y], fill=fg, width=max(lw // 2, 1))


# ─────────────────────────────────────────────────────────
# Custom Timeline Widget — Liquid
# ─────────────────────────────────────────────────────────

class Timeline(tk.Canvas):
    """Anti-aliased timeline rendered via PIL 2x supersampling."""

    TRACK_H = 6
    HANDLE_R = 9
    PAD_X = 24
    HEIGHT = 50
    GLOW_R = 18

    def __init__(self, parent, total_frames, fps, on_seek=None, **kw):
        super().__init__(parent, height=self.HEIGHT, bg=C['surface'],
                         highlightthickness=0, cursor='hand2', **kw)
        self.total = max(total_frames, 1)
        self.fps = fps
        self.on_seek = on_seek
        self.position = 0.0
        self._hovering = False
        self._hover_x = 0
        self._dragging = False
        self._annotation_positions = set()
        self._photo = None
        self._bg = hex_rgb(C['surface'])

        self.bind('<Configure>', self._on_configure)
        self.bind('<Motion>', self._on_motion)
        self.bind('<Leave>', self._on_leave)
        self.bind('<Button-1>', self._on_press)
        self.bind('<B1-Motion>', self._on_drag)
        self.bind('<ButtonRelease-1>', self._on_release)

    def set_position(self, frame_id):
        self.position = frame_id / self.total if self.total > 0 else 0
        self._draw()

    def set_annotations(self, positions):
        self._annotation_positions = positions
        self._draw()

    def _track_line(self):
        w = self.winfo_width()
        x0 = self.PAD_X
        x1 = w - self.PAD_X
        cy = 20
        return x0, cy, x1

    def _pos_to_x(self, pos):
        x0, _, x1 = self._track_line()
        return x0 + pos * (x1 - x0)

    def _x_to_pos(self, x):
        x0, _, x1 = self._track_line()
        return max(0.0, min(1.0, (x - x0) / max(1, x1 - x0)))

    def _draw(self):
        w = self.winfo_width()
        h = self.HEIGHT
        if w < 60:
            return

        S = _SS
        sw, sh = w * S, h * S
        bg = self._bg

        img = Image.new('RGB', (sw, sh), bg)
        draw = ImageDraw.Draw(img)

        x0, cy, x1 = self._track_line()
        sx0, scy, sx1 = x0 * S, cy * S, x1 * S
        sth = self.TRACK_H * S
        tr = sth // 2

        # Track background
        draw.rounded_rectangle(
            [sx0, scy - tr, sx1, scy + tr],
            radius=tr, fill=hex_rgb(C['timeline_bg']))

        # Played portion
        px = self._pos_to_x(self.position)
        spx = int(px * S)
        if spx > sx0 + sth:
            draw.rounded_rectangle(
                [sx0, scy - tr, spx, scy + tr],
                radius=tr, fill=hex_rgb(C['timeline_played']))
        elif spx > sx0:
            draw.ellipse([sx0, scy - tr, sx0 + sth, scy + tr],
                         fill=hex_rgb(C['timeline_played']))

        # Annotation dots
        for apos in self._annotation_positions:
            ax = int(self._pos_to_x(apos) * S)
            dr = 3 * S
            dy = scy + tr + 6 * S
            draw.ellipse([ax - dr, dy - dr, ax + dr, dy + dr],
                         fill=hex_rgb(C['annotation']))

        # Handle
        active = self._hovering or self._dragging
        shr = self.HANDLE_R * S

        if active:
            sgr = self.GLOW_R * S
            glow_c = blend_rgb(hex_rgb(C['accent']), bg, 0.18)
            draw.ellipse([spx - sgr, scy - sgr, spx + sgr, scy + sgr],
                         fill=glow_c)

        # Accent border ring
        sbr = shr + 3 * S
        bc = hex_rgb(C['accent']) if active else hex_rgb(C['accent_soft'])
        draw.ellipse([spx - sbr, scy - sbr, spx + sbr, scy + sbr], fill=bc)

        # White core
        draw.ellipse([spx - shr, scy - shr, spx + shr, scy + shr],
                     fill=hex_rgb(C['handle']))

        # Tooltip background
        if self._hovering and not self._dragging:
            hpos = self._x_to_pos(self._hover_x)
            shx = int(self._pos_to_x(hpos) * S)
            ttw, tth = 92 * S, 26 * S
            tx = max(ttw // 2 + 4 * S, min(sw - ttw // 2 - 4 * S, shx))
            ty = scy + tr + 14 * S
            draw.rounded_rectangle(
                [tx - ttw // 2 + S, ty + S, tx + ttw // 2 + S, ty + tth + S],
                radius=8 * S, fill=hex_rgb(C['bg']))
            draw.rounded_rectangle(
                [tx - ttw // 2, ty, tx + ttw // 2, ty + tth],
                radius=8 * S, fill=hex_rgb(C['surface_3']),
                outline=hex_rgb(C['border']), width=S)

        # Downscale → anti-aliased
        img = img.resize((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)

        self.delete('all')
        self.create_image(0, 0, image=self._photo, anchor=tk.NW)

        # Canvas text overlays (crisp font rendering)
        if self._hovering and not self._dragging:
            hpos = self._x_to_pos(self._hover_x)
            hx = self._pos_to_x(hpos)
            hframe = int(hpos * self.total)
            htime = fmt_time(hframe / self.fps)

            self.create_line(hx, cy - 10, hx, cy + 10,
                             fill=C['text_dim'], width=1, dash=(2, 2))

            ttw_1x = 92
            tth_1x = 26
            tx = max(ttw_1x // 2 + 4, min(w - ttw_1x // 2 - 4, int(hx)))
            ty = cy + self.TRACK_H // 2 + 14
            self.create_text(tx, ty + tth_1x // 2, text=f"{htime}  f{hframe}",
                             fill=C['text'], font=FONT_MONO_XS)

    def _on_configure(self, e):
        self._draw()

    def _on_motion(self, e):
        self._hovering = True
        self._hover_x = e.x
        if not self._dragging:
            self._draw()

    def _on_leave(self, e):
        self._hovering = False
        self._draw()

    def _on_press(self, e):
        self._dragging = True
        self.position = self._x_to_pos(e.x)
        self._draw()

    def _on_drag(self, e):
        if self._dragging:
            self.position = self._x_to_pos(e.x)
            self._draw()

    def _on_release(self, e):
        if self._dragging:
            self._dragging = False
            self.position = self._x_to_pos(e.x)
            self._draw()
            if self.on_seek:
                frame = max(0, min(self.total - 1, int(self.position * self.total)))
                self.on_seek(frame)


# ─────────────────────────────────────────────────────────
# Icon Button — PIL rendered
# ─────────────────────────────────────────────────────────

class IconBtn(tk.Canvas):
    """Anti-aliased icon button with PIL-rendered backgrounds."""

    def __init__(self, parent, icon, command=None, size=36, font=None,
                 fg=None, bg=None, hover_bg=None, circular=False, tooltip=''):
        self._size = size
        self._circular = circular

        parent_bg = C['surface']
        try:
            parent_bg = parent.cget('bg')
        except Exception:
            pass

        if circular:
            canvas_bg = parent_bg
            self._shape_bg = bg or C['accent']
            self._hover_bg = hover_bg or C['accent_hover']
        else:
            canvas_bg = parent_bg
            self._shape_bg = parent_bg
            self._hover_bg = hover_bg or C['btn_hover']

        self._canvas_bg_rgb = hex_rgb(canvas_bg)

        super().__init__(parent, width=size, height=size,
                         bg=canvas_bg, highlightthickness=0, cursor='hand2')
        self._icon = icon
        self._command = command
        self._fg = fg or C['text']
        self._font = font or FONT_ICON
        self._hovered = False
        self._photo = None

        self.bind('<Enter>', lambda e: self._set_hover(True))
        self.bind('<Leave>', lambda e: self._set_hover(False))
        self.bind('<Button-1>', lambda e: self._command() if self._command else None)
        self._draw()

    def set_icon(self, icon):
        self._icon = icon
        self._draw()

    def set_fg(self, fg):
        self._fg = fg
        self._draw()

    def _set_hover(self, val):
        self._hovered = val
        self._draw()

    def _draw(self):
        self.delete('all')
        s = self._size
        S = _SS
        ss = s * S
        bg = self._canvas_bg_rgb

        if self._circular:
            fill = hex_rgb(self._hover_bg if self._hovered else self._shape_bg)
            img = Image.new('RGB', (ss, ss), bg)
            draw = ImageDraw.Draw(img)
            m = S
            draw.ellipse([m, m, ss - m - 1, ss - m - 1], fill=fill)
            img = img.resize((s, s), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self.create_image(0, 0, image=self._photo, anchor=tk.NW)
        elif self._hovered:
            img = Image.new('RGB', (ss, ss), bg)
            draw = ImageDraw.Draw(img)
            m = 3 * S
            draw.rounded_rectangle([m, m, ss - m, ss - m], radius=8 * S,
                                    fill=hex_rgb(self._hover_bg))
            img = img.resize((s, s), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(img)
            self.create_image(0, 0, image=self._photo, anchor=tk.NW)
        else:
            self._photo = None

        self.create_text(s // 2, s // 2, text=self._icon,
                         fill=self._fg, font=self._font)


# ─────────────────────────────────────────────────────────
# Performance Pipeline
# ─────────────────────────────────────────────────────────

class FramePipeline:
    def __init__(self, vh, buffer_size=16):
        self.vh = vh
        self._db_path = str(vh.path)
        self.buffer_size = buffer_size
        self._buffer = {}
        self._photo_cache = {}
        self._photo_cache_order = collections.deque()
        self._photo_cache_max = 60
        self._lock = threading.Lock()
        self._prefetch_thread = None
        self._prefetch_stop = threading.Event()
        self._total = vh.frame_count

    def prefetch_from(self, start, count=None):
        if count is None:
            count = self.buffer_size
        self._prefetch_stop.set()
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=0.1)
        self._prefetch_stop.clear()
        self._prefetch_thread = threading.Thread(
            target=self._worker, args=(start, count), daemon=True)
        self._prefetch_thread.start()

    def _worker(self, start, count):
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA cache_size=-32000")
        try:
            for i in range(count):
                if self._prefetch_stop.is_set():
                    return
                fid = start + i
                if fid >= self._total:
                    return
                with self._lock:
                    if fid in self._buffer:
                        continue
                data = self._read(conn, fid)
                if not data:
                    continue
                img = Image.open(io.BytesIO(data))
                with self._lock:
                    self._buffer[fid] = img
                    if len(self._buffer) > self.buffer_size * 2:
                        for k in sorted(self._buffer)[:self.buffer_size // 2]:
                            if k < start:
                                del self._buffer[k]
        finally:
            conn.close()

    @staticmethod
    def _read(conn, fid, depth=0):
        if depth > 10:
            return None
        row = conn.execute(
            "SELECT frame_type, ref_frame_id, image_data FROM frames WHERE frame_id=?",
            (fid,)).fetchone()
        if not row:
            return None
        ft, ref, data = row
        if ft == 'full':
            return data
        if ft in ('ref', 'delta'):
            return FramePipeline._read(conn, ref, depth + 1)
        return None

    def get_image(self, fid):
        with self._lock:
            if fid in self._buffer:
                return self._buffer[fid]
        data = self.vh.get_frame_image(fid)
        if not data:
            return None
        img = Image.open(io.BytesIO(data))
        with self._lock:
            self._buffer[fid] = img
        return img

    def get_photo(self, fid, zoom, fast=False):
        key = (fid, round(zoom, 3))
        if key in self._photo_cache:
            return self._photo_cache[key]
        img = self.get_image(fid)
        if not img:
            return None
        nw, nh = int(img.width * zoom), int(img.height * zoom)
        if nw <= 0 or nh <= 0:
            return None
        r = Image.NEAREST if fast else Image.LANCZOS
        photo = ImageTk.PhotoImage(img.resize((nw, nh), r))
        self._photo_cache[key] = photo
        self._photo_cache_order.append(key)
        while len(self._photo_cache) > self._photo_cache_max:
            self._photo_cache.pop(self._photo_cache_order.popleft(), None)
        return photo

    def invalidate_zoom(self):
        self._photo_cache.clear()
        self._photo_cache_order.clear()


class AudioPlayer:
    def __init__(self, vh):
        self._proc = None
        self._temp = None
        self._has_audio = False
        self._muted = False
        audio = vh.get_audio()
        if audio and audio['data']:
            self._temp = tempfile.NamedTemporaryFile(
                suffix='.opus', prefix='vh_audio_', delete=False)
            self._temp.write(audio['data'])
            self._temp.flush()
            if os.path.getsize(self._temp.name) > 0:
                self._has_audio = True

    @property
    def has_audio(self):
        return self._has_audio and not self._muted

    def play_from(self, sec):
        self.stop()
        if not self._has_audio or self._muted:
            return
        try:
            self._proc = subprocess.Popen(
                ['ffplay', '-nodisp', '-autoexit', '-ss', f'{sec:.3f}',
                 '-i', self._temp.name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            self._has_audio = False

    def stop(self):
        if self._proc:
            try:
                self._proc.kill()
                self._proc.wait(timeout=0.1)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            self._proc = None

    def toggle_mute(self):
        self._muted = not self._muted
        if self._muted:
            self.stop()
        return self._muted

    def cleanup(self):
        self.stop()
        if self._temp:
            try:
                os.unlink(self._temp.name)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────
# Main Viewer — Liquid
# ─────────────────────────────────────────────────────────

class VHViewer:
    def __init__(self, vh_path, start_frame=0):
        self.vh = VHFile(vh_path, mode='a')
        self.meta = self.vh.get_all_meta()
        self.total = self.vh.frame_count
        self.fps = self.meta.get('fps', 24)
        self.width = self.meta.get('width', 1920)
        self.height = self.meta.get('height', 1080)

        self.current_frame = start_frame
        self.playing = False
        self.zoom = 1.0
        self._after_id = None
        self._fullscreen = False
        self._osd_items = []
        self._osd_after = None

        self.pipeline = FramePipeline(self.vh, buffer_size=20)
        self.audio = AudioPlayer(self.vh)

        self._ann_frames = set()
        try:
            rows = self.vh._conn.execute(
                "SELECT DISTINCT frame_id FROM annotations").fetchall()
            self._ann_frames = {r[0] for r in rows}
        except Exception:
            pass

        self._build_ui(vh_path)
        self.pipeline.prefetch_from(start_frame)
        self._display_frame(self.current_frame)
        self.root.mainloop()

    def _build_ui(self, vh_path):
        self.root = tk.Tk()
        self.root.title("VH Viewer")
        self.root.configure(bg=C['bg'])

        # Window sizing
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        max_w = int(screen_w * 0.82)
        max_h = int(screen_h * 0.82) - 160
        scale = min(max_w / self.width, max_h / self.height, 1.0)
        self.zoom = scale
        win_w = max(900, int(self.width * scale) + 32)
        win_h = int(self.height * scale) + 180
        x = (screen_w - win_w) // 2
        y = (screen_h - win_h) // 2
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.root.minsize(700, 400)

        # ── Header bar ──
        header = tk.Frame(self.root, bg=C['surface'], height=46)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        # Brand
        tk.Label(header, text="\u25c9  VH", fg=C['brand'], bg=C['surface'],
                 font=FONT_BRAND).pack(side=tk.LEFT, padx=(16, 6))

        # Separator
        tk.Frame(header, bg=C['border'], width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=11)

        # Filename
        fname = Path(vh_path).name
        tk.Label(header, text=fname, fg=C['text'], bg=C['surface'],
                 font=FONT_UI).pack(side=tk.LEFT, padx=6)

        # Separator
        tk.Frame(header, bg=C['border'], width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=11)

        # Resolution & FPS
        res_text = f"{self.width}\u00d7{self.height}  {self.fps:.0f}fps"
        tk.Label(header, text=res_text, fg=C['text_dim'], bg=C['surface'],
                 font=FONT_MONO_SM).pack(side=tk.LEFT, padx=6)

        # Version badge
        ver = self.meta.get('format_version', 1)
        badge_fg = C['accent'] if ver >= 2 else C['text_dim']
        badge_bg = C['accent_surface'] if ver >= 2 else C['surface_3']
        tk.Label(header, text=f" v{ver} ", fg=badge_fg, bg=badge_bg,
                 font=FONT_BADGE, padx=8, pady=2).pack(side=tk.LEFT, padx=6)

        # Right side: frame count
        tk.Label(header, text=f"{self.total:,} frames", fg=C['text_dim'],
                 bg=C['surface'], font=FONT_MONO_SM).pack(side=tk.RIGHT, padx=16)

        # Audio badge
        if self.audio._has_audio:
            tk.Label(header, text=" \u266b Audio ", fg=C['annotation'],
                     bg=C['annotation_dim'],
                     font=FONT_BADGE, padx=8, pady=2).pack(side=tk.RIGHT, padx=6)

        # Header accent underline
        tk.Frame(self.root, bg=C['accent_dim'], height=1).pack(fill=tk.X)

        # ── Video canvas ──
        self.canvas = tk.Canvas(self.root, bg=C['video_bg'], highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        # ── Bottom panel ──
        # Accent top edge
        tk.Frame(self.root, bg=C['accent_dim'], height=1).pack(fill=tk.X)

        bottom = tk.Frame(self.root, bg=C['surface'])
        bottom.pack(fill=tk.X)

        # ── Timeline ──
        self.timeline = Timeline(
            bottom, self.total, self.fps, on_seek=self._seek_to)
        self.timeline.pack(fill=tk.X, padx=12, pady=(10, 0))

        ann_positions = {f / self.total for f in self._ann_frames if f < self.total}
        self.timeline.set_annotations(ann_positions)

        # ── Controls row ──
        ctrl = tk.Frame(bottom, bg=C['surface'])
        ctrl.pack(fill=tk.X, padx=20, pady=(8, 16))

        # Transport buttons
        transport = tk.Frame(ctrl, bg=C['surface'])
        transport.pack(side=tk.LEFT)

        self.btn_start = IconBtn(transport, "\u23ee", self._goto_start,
                                 size=36, font=FONT_ICON)
        self.btn_start.pack(side=tk.LEFT, padx=3)

        self.btn_prev = IconBtn(transport, "\u23f4", self._prev_frame,
                                size=36, font=FONT_ICON)
        self.btn_prev.pack(side=tk.LEFT, padx=3)

        # Play button — circular, accent-filled
        self.btn_play = IconBtn(transport, "\u25b6", self._toggle_play,
                                size=48, font=FONT_ICON_LG,
                                fg=C['play_fg'], bg=C['play_bg'],
                                hover_bg=C['play_hover'], circular=True)
        self.btn_play.pack(side=tk.LEFT, padx=10)

        self.btn_next = IconBtn(transport, "\u23f5", self._next_frame,
                                size=36, font=FONT_ICON)
        self.btn_next.pack(side=tk.LEFT, padx=3)

        self.btn_end = IconBtn(transport, "\u23ed", self._goto_end,
                               size=36, font=FONT_ICON)
        self.btn_end.pack(side=tk.LEFT, padx=3)

        # Time display
        time_frame = tk.Frame(ctrl, bg=C['surface'])
        time_frame.pack(side=tk.LEFT, padx=(24, 0))

        self.time_current = tk.Label(time_frame, text="00:00.00",
                                     fg=C['text_bright'], bg=C['surface'],
                                     font=FONT_TIME)
        self.time_current.pack(side=tk.LEFT)

        tk.Label(time_frame, text=" / ", fg=C['text_muted'],
                 bg=C['surface'], font=FONT_TIME).pack(side=tk.LEFT)

        dur = fmt_time(self.total / self.fps)
        self.time_total = tk.Label(time_frame, text=dur,
                                   fg=C['text_dim'], bg=C['surface'],
                                   font=FONT_TIME)
        self.time_total.pack(side=tk.LEFT)

        # Right side controls
        right_ctrl = tk.Frame(ctrl, bg=C['surface'])
        right_ctrl.pack(side=tk.RIGHT)

        bg_rgb = hex_rgb(C['surface'])

        # Annotate button (PIL icon)
        self._icon_tag_img = make_icon(28, _icon_tag, hex_rgb(C['annotation']), bg_rgb)
        self.btn_ann = tk.Button(
            right_ctrl, image=self._icon_tag_img, command=self._add_annotation,
            bg=C['surface'], activebackground=C['surface_2'],
            relief=tk.FLAT, bd=0, highlightthickness=0, cursor='hand2')
        self.btn_ann.pack(side=tk.RIGHT, padx=6)

        # Zoom controls
        self.zoom_label = tk.Label(right_ctrl, text="100%",
                                   fg=C['text_dim'], bg=C['surface'],
                                   font=FONT_MONO_SM, width=5)
        self.zoom_label.pack(side=tk.RIGHT, padx=2)

        self._icon_zin_img = make_icon(24, _icon_zoom_in, hex_rgb(C['text_dim']), bg_rgb)
        self._icon_zout_img = make_icon(24, _icon_zoom_out, hex_rgb(C['text_dim']), bg_rgb)

        tk.Button(right_ctrl, image=self._icon_zin_img, command=self._zoom_in,
                  bg=C['surface'], activebackground=C['surface_2'],
                  relief=tk.FLAT, bd=0, highlightthickness=0,
                  cursor='hand2').pack(side=tk.RIGHT, padx=3)
        tk.Button(right_ctrl, image=self._icon_zout_img, command=self._zoom_out,
                  bg=C['surface'], activebackground=C['surface_2'],
                  relief=tk.FLAT, bd=0, highlightthickness=0,
                  cursor='hand2').pack(side=tk.RIGHT, padx=3)

        # Separator
        tk.Frame(right_ctrl, bg=C['border'], width=1, height=24).pack(
            side=tk.RIGHT, padx=10)

        # Volume button (PIL icon)
        if self.audio._has_audio:
            self._icon_vol_img = make_icon(28, _icon_volume, hex_rgb(C['text']), bg_rgb)
            self._icon_mute_img = make_icon(28, _icon_volume_off, hex_rgb(C['mute_off']), bg_rgb)
            self.btn_mute = tk.Button(
                right_ctrl, image=self._icon_vol_img, command=self._toggle_mute,
                bg=C['surface'], activebackground=C['surface_2'],
                relief=tk.FLAT, bd=0, highlightthickness=0, cursor='hand2')
        else:
            self._icon_vol_img = make_icon(28, _icon_volume_off, hex_rgb(C['text_muted']), bg_rgb)
            self._icon_mute_img = self._icon_vol_img
            self.btn_mute = tk.Button(
                right_ctrl, image=self._icon_vol_img,
                bg=C['surface'], activebackground=C['surface'],
                relief=tk.FLAT, bd=0, highlightthickness=0)
        self.btn_mute.pack(side=tk.RIGHT, padx=6)

        # Separator
        tk.Frame(right_ctrl, bg=C['border'], width=1, height=24).pack(
            side=tk.RIGHT, padx=10)

        # Frame number entry
        frame_box = tk.Frame(right_ctrl, bg=C['surface_3'],
                             highlightthickness=1, highlightbackground=C['border'])
        frame_box.pack(side=tk.RIGHT, padx=6)

        inner = tk.Frame(frame_box, bg=C['surface_3'])
        inner.pack(padx=8, pady=4)

        tk.Label(inner, text="Frame", fg=C['text_dim'], bg=C['surface_3'],
                 font=FONT_MONO_XS).pack(side=tk.LEFT, padx=(0, 4))
        self.frame_entry = tk.Entry(
            inner, width=7, bg=C['surface_3'], fg=C['text'],
            insertbackground=C['accent'], font=FONT_MONO,
            relief=tk.FLAT, borderwidth=0, highlightthickness=0)
        self.frame_entry.pack(side=tk.LEFT)
        self.frame_entry.bind('<Return>', self._goto_frame_entry)

        # ── Annotation bar ──
        self.ann_bar = tk.Frame(bottom, bg=C['surface'], height=0)
        self.ann_bar.pack(fill=tk.X, padx=24)

        self.ann_label = tk.Label(
            self.ann_bar, text='', fg=C['annotation'], bg=C['surface'],
            font=FONT_MONO_SM, anchor='w')

        # ── Keybindings ──
        self.root.bind('<Left>', lambda e: self._prev_frame())
        self.root.bind('<Right>', lambda e: self._next_frame())
        self.root.bind('<space>', lambda e: self._toggle_play())
        self.root.bind('<Home>', lambda e: self._goto_start())
        self.root.bind('<End>', lambda e: self._goto_end())
        self.root.bind('<Control-g>', lambda e: self._goto_dialog())
        self.root.bind('<plus>', lambda e: self._zoom_in())
        self.root.bind('<equal>', lambda e: self._zoom_in())
        self.root.bind('<minus>', lambda e: self._zoom_out())
        self.root.bind('<Shift-Left>', lambda e: self._skip(-10))
        self.root.bind('<Shift-Right>', lambda e: self._skip(10))
        self.root.bind('<Control-Left>', lambda e: self._skip(-100))
        self.root.bind('<Control-Right>', lambda e: self._skip(100))
        self.root.bind('<m>', lambda e: self._toggle_mute())
        self.root.bind('<a>', lambda e: self._add_annotation())
        self.root.bind('<f>', lambda e: self._toggle_fullscreen())
        self.canvas.bind('<Button-4>', lambda e: self._zoom_in())
        self.canvas.bind('<Button-5>', lambda e: self._zoom_out())

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Display ──

    def _display_frame(self, frame_id, fast=False):
        if frame_id < 0 or frame_id >= self.total:
            return
        self.current_frame = frame_id

        photo = self.pipeline.get_photo(frame_id, self.zoom, fast=fast)
        if not photo:
            return

        self.canvas.delete('vid')
        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        self.canvas.create_image(cx, cy, image=photo, anchor=tk.CENTER, tags='vid')
        self.canvas._photo = photo

        # Raise OSD above video if present
        for item in self._osd_items:
            self.canvas.tag_raise(item)

        # Time
        ts = frame_id / self.fps
        self.time_current.config(text=fmt_time(ts))

        # Timeline
        self.timeline.set_position(frame_id)

        # Frame entry
        self.frame_entry.delete(0, tk.END)
        self.frame_entry.insert(0, str(frame_id))

        # Annotations
        annotations = self.vh.get_annotations(frame_id)
        if annotations:
            parts = []
            for k, v in annotations.items():
                if isinstance(v, list):
                    v = ', '.join(str(i) for i in v)
                elif isinstance(v, dict):
                    v = ', '.join(f"{dk}: {dv}" for dk, dv in v.items())
                parts.append(f"{k}: {v}")
            self.ann_label.config(text="    \u2502    ".join(parts))
            self.ann_label.pack(fill=tk.X, pady=(0, 4))
        else:
            self.ann_label.pack_forget()

        # Zoom label
        self.zoom_label.config(text=f"{self.zoom:.0%}")

    # ── OSD (On-Screen Display) ──

    def _show_osd(self, icon):
        """Flash a large icon on the video canvas."""
        self._clear_osd()
        cx = self.canvas.winfo_width() // 2
        cy = self.canvas.winfo_height() // 2
        # Outer glow
        r2 = 44
        glow = self.canvas.create_oval(cx - r2, cy - r2, cx + r2, cy + r2,
                                        fill=C['accent_glow'], outline='')
        # Main circle
        r = 38
        bg = self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                      fill=C['osd_bg'], outline=C['accent_dim'], width=2)
        txt = self.canvas.create_text(cx, cy, text=icon,
                                       fill=C['osd_fg'], font=FONT_OSD)
        self._osd_items = [glow, bg, txt]
        self._osd_after = self.root.after(500, self._clear_osd)

    def _clear_osd(self):
        if self._osd_after:
            self.root.after_cancel(self._osd_after)
            self._osd_after = None
        for item in self._osd_items:
            self.canvas.delete(item)
        self._osd_items = []

    # ── Playback ──

    def _toggle_play(self):
        if self.playing:
            self._stop_playback()
            self._show_osd("\u23f8")
        else:
            self._clear_osd()
            self._start_playback()

    def _start_playback(self):
        self.playing = True
        self.btn_play.set_icon("\u23f8")
        self._play_t0 = time.monotonic()
        self._play_f0 = self.current_frame

        self.pipeline.prefetch_from(self.current_frame, count=24)
        self.audio.play_from(self.current_frame / self.fps)
        self._tick()

    def _stop_playback(self):
        self.playing = False
        self.btn_play.set_icon("\u25b6")
        self.audio.stop()
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self._display_frame(self.current_frame, fast=False)

    def _tick(self):
        if not self.playing:
            return
        if self.current_frame >= self.total - 1:
            self._stop_playback()
            return

        elapsed = time.monotonic() - self._play_t0
        target = self._play_f0 + int(elapsed * self.fps)
        target = min(target, self.total - 1)

        if target > self.current_frame:
            self._display_frame(target, fast=True)
            if target % 8 == 0:
                self.pipeline.prefetch_from(target + 1, count=16)

        next_t = (target + 1 - self._play_f0) / self.fps
        delay = max(1, int((next_t - elapsed) * 1000))
        self._after_id = self.root.after(delay, self._tick)

    # ── Navigation ──

    def _seek_to(self, fid):
        fid = max(0, min(self.total - 1, fid))
        was_playing = self.playing
        if was_playing:
            self.audio.stop()
            if self._after_id:
                self.root.after_cancel(self._after_id)
                self._after_id = None
        self.pipeline.prefetch_from(fid)
        self._display_frame(fid, fast=was_playing)
        if was_playing:
            self._play_t0 = time.monotonic()
            self._play_f0 = fid
            self.audio.play_from(fid / self.fps)
            self._tick()

    def _next_frame(self):
        if not self.playing and self.current_frame < self.total - 1:
            self._display_frame(self.current_frame + 1)
            self.pipeline.prefetch_from(self.current_frame + 1, count=8)

    def _prev_frame(self):
        if not self.playing and self.current_frame > 0:
            self._display_frame(self.current_frame - 1)

    def _skip(self, n):
        self._seek_to(self.current_frame + n)

    def _goto_start(self):
        self._seek_to(0)

    def _goto_end(self):
        self._seek_to(self.total - 1)

    def _goto_dialog(self):
        was = self.playing
        if was:
            self._stop_playback()
        r = simpledialog.askinteger(
            "Go to frame", f"Frame (0\u2013{self.total-1}):",
            parent=self.root, minvalue=0, maxvalue=self.total - 1)
        if r is not None:
            self._seek_to(r)
        elif was:
            self._start_playback()

    def _goto_frame_entry(self, event=None):
        try:
            self._seek_to(int(self.frame_entry.get()))
        except ValueError:
            pass

    # ── Zoom ──

    def _zoom_in(self):
        self.zoom = min(3.0, self.zoom * 1.15)
        self.pipeline.invalidate_zoom()
        self._display_frame(self.current_frame)

    def _zoom_out(self):
        self.zoom = max(0.1, self.zoom / 1.15)
        self.pipeline.invalidate_zoom()
        self._display_frame(self.current_frame)

    # ── Audio ──

    def _toggle_mute(self):
        if not self.audio._has_audio:
            return
        muted = self.audio.toggle_mute()
        if muted:
            self.btn_mute.config(image=self._icon_mute_img)
        else:
            self.btn_mute.config(image=self._icon_vol_img)
            if self.playing:
                self.audio.play_from(self.current_frame / self.fps)

    # ── Fullscreen ──

    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.root.attributes('-fullscreen', self._fullscreen)

    # ── Annotation ──

    def _add_annotation(self):
        was = self.playing
        if was:
            self._stop_playback()
        key = simpledialog.askstring("Annotation", "Key:", parent=self.root)
        if not key:
            if was:
                self._start_playback()
            return
        value = simpledialog.askstring("Annotation", f"Value for '{key}':",
                                       parent=self.root)
        if value is None:
            if was:
                self._start_playback()
            return
        self.vh.annotate(self.current_frame, key, value)
        self.vh.commit()
        self._ann_frames.add(self.current_frame)
        self.timeline.set_annotations(
            {f / self.total for f in self._ann_frames})
        self._display_frame(self.current_frame)
        if was:
            self._start_playback()

    # ── Cleanup ──

    def _on_close(self):
        self.playing = False
        if self._after_id:
            self.root.after_cancel(self._after_id)
        self.audio.cleanup()
        self.vh.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description='VH Viewer')
    parser.add_argument('input', help='Input .vh file')
    parser.add_argument('--start', type=int, default=0, help='Start frame')
    args = parser.parse_args()
    VHViewer(args.input, args.start)


if __name__ == '__main__':
    main()
