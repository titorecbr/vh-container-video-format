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
FONT_ACTION = ('Noto Sans', 13)
FONT_ACTION_SM = ('Noto Sans', 11)


def fmt_time(seconds):
    m = int(seconds) // 60
    s = seconds - m * 60
    return f"{m:02d}:{s:05.2f}"


# ─────────────────────────────────────────────────────────
# Themed Dialog
# ─────────────────────────────────────────────────────────

class _ThemedDialog(tk.Toplevel):
    """Dark-themed modal dialog matching the player UI."""

    def __init__(self, parent, title="", width=380):
        super().__init__(parent)
        self.configure(bg=C['accent_dim'])
        self.resizable(False, False)
        self.transient(parent)

        # Remove WM decorations, use custom title bar
        self.overrideredirect(True)
        self.withdraw()  # hide until sized

        # Outer border (1px accent)
        self._border = tk.Frame(self, bg=C['accent_dim'], padx=1, pady=1)
        self._border.pack(fill=tk.BOTH, expand=True)

        self._main = tk.Frame(self._border, bg=C['surface'])
        self._main.pack(fill=tk.BOTH, expand=True)

        # Title bar
        tbar = tk.Frame(self._main, bg=C['surface_2'], height=36)
        tbar.pack(fill=tk.X)
        tbar.pack_propagate(False)
        tk.Label(tbar, text=title, fg=C['accent'], bg=C['surface_2'],
                 font=FONT_UI_BOLD).pack(side=tk.LEFT, padx=14)
        close_btn = tk.Label(tbar, text="\u2715", fg=C['text_dim'],
                             bg=C['surface_2'], font=FONT_UI, cursor='hand2')
        close_btn.pack(side=tk.RIGHT, padx=12)
        close_btn.bind('<Button-1>', lambda e: self._cancel())
        close_btn.bind('<Enter>', lambda e: close_btn.config(fg=C['mute_off']))
        close_btn.bind('<Leave>', lambda e: close_btn.config(fg=C['text_dim']))

        # Dragging
        tbar.bind('<Button-1>', self._start_drag)
        tbar.bind('<B1-Motion>', self._do_drag)
        for child in tbar.winfo_children():
            child.bind('<Button-1>', self._start_drag)
            child.bind('<B1-Motion>', self._do_drag)

        tk.Frame(self._main, bg=C['border'], height=1).pack(fill=tk.X)

        # Body
        self.body = tk.Frame(self._main, bg=C['surface'])
        self.body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(16, 8))

        # Button row
        self._btn_row = tk.Frame(self._main, bg=C['surface'])
        self._btn_row.pack(fill=tk.X, padx=20, pady=(4, 16))

        self.result = None
        self._width = width
        self._parent = parent
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind('<Escape>', lambda e: self._cancel())

    def _start_drag(self, e):
        self._dx = e.x
        self._dy = e.y

    def _do_drag(self, e):
        x = self.winfo_x() + e.x - self._dx
        y = self.winfo_y() + e.y - self._dy
        self.geometry(f"+{x}+{y}")

    def _cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()

    def _ok(self):
        self.grab_release()
        self.destroy()

    def _make_btn(self, parent, text, command, primary=False):
        bg = C['accent'] if primary else C['surface_3']
        fg = C['play_fg'] if primary else C['text']
        hover = C['accent_hover'] if primary else C['border']
        btn = tk.Label(parent, text=text, fg=fg, bg=bg,
                       font=FONT_UI_BOLD, cursor='hand2',
                       padx=20, pady=6)
        btn.pack(side=tk.RIGHT, padx=(8, 0))
        btn.bind('<Button-1>', lambda e: command())
        btn.bind('<Enter>', lambda e: btn.config(bg=hover))
        btn.bind('<Leave>', lambda e: btn.config(bg=bg))
        return btn

    def show(self):
        """Size, center, and display the dialog. Call after adding body content."""
        self.update_idletasks()
        w = max(self._width, self.winfo_reqwidth())
        h = self.winfo_reqheight()
        px = self._parent.winfo_rootx()
        py = self._parent.winfo_rooty()
        pw = self._parent.winfo_width()
        ph = self._parent.winfo_height()
        x = px + pw // 2 - w // 2
        y = py + ph // 2 - h // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.deiconify()
        self.grab_set()
        self.focus_force()


# Pre-registered annotation keys (viewer only)
ANNOTATION_KEYS = [
    "label",
    "scene",
    "action",
    "object",
    "emotion",
    "description",
    "tag",
    "category",
    "text",
    "person",
    "event",
    "quality",
    "anomaly",
    "note",
]


def themed_askselect(parent, title, prompt, options):
    """Themed select/dropdown dialog. Returns selected option or None."""
    dlg = _ThemedDialog(parent, title)

    tk.Label(dlg.body, text=prompt, fg=C['text'], bg=C['surface'],
             font=FONT_UI, anchor='w').pack(fill=tk.X, pady=(0, 8))

    selected = tk.StringVar(value=options[0] if options else "")

    # Dropdown button
    dropdown_frame = tk.Frame(dlg.body, bg=C['border'], highlightthickness=0)
    dropdown_frame.pack(fill=tk.X)

    inner_dd = tk.Frame(dropdown_frame, bg=C['surface_3'])
    inner_dd.pack(fill=tk.X, padx=1, pady=1)

    sel_label = tk.Label(inner_dd, textvariable=selected,
                         fg=C['text_bright'], bg=C['surface_3'],
                         font=FONT_MONO, anchor='w', padx=10, pady=8)
    sel_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

    arrow = tk.Label(inner_dd, text="\u25bc", fg=C['text_dim'],
                     bg=C['surface_3'], font=FONT_MONO_XS, padx=10)
    arrow.pack(side=tk.RIGHT)

    # Scrollable dropdown list (hidden by default)
    MAX_VISIBLE = 8
    list_frame = tk.Frame(dlg.body, bg=C['border'])
    list_visible = [False]

    def _build_list():
        for child in list_frame.winfo_children():
            child.destroy()

        list_border = tk.Frame(list_frame, bg=C['surface_2'])
        list_border.pack(fill=tk.X, padx=1, pady=1)

        # Scrollable canvas
        item_h = 30
        vis_h = min(len(options), MAX_VISIBLE) * item_h
        canvas = tk.Canvas(list_border, bg=C['surface_2'],
                           highlightthickness=0, height=vis_h)
        canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)

        if len(options) > MAX_VISIBLE:
            sb = tk.Scrollbar(list_border, orient=tk.VERTICAL,
                              command=canvas.yview, width=6,
                              bg=C['surface_3'], troughcolor=C['surface_2'],
                              highlightthickness=0, borderwidth=0)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            canvas.configure(yscrollcommand=sb.set)

        inner = tk.Frame(canvas, bg=C['surface_2'])
        canvas.create_window((0, 0), window=inner, anchor=tk.NW)

        for opt in options:
            item = tk.Label(inner, text=opt, fg=C['text'],
                            bg=C['surface_2'], font=FONT_MONO,
                            anchor='w', padx=10, pady=4, cursor='hand2',
                            width=30)
            item.pack(fill=tk.X)
            item.bind('<Enter>', lambda e, w=item: w.config(
                bg=C['accent'], fg=C['play_fg']))
            item.bind('<Leave>', lambda e, w=item: w.config(
                bg=C['surface_2'], fg=C['text']))
            item.bind('<Button-1>', lambda e, o=opt: _select(o))
            # Mouse wheel
            item.bind('<Button-4>',
                      lambda e: canvas.yview_scroll(-3, 'units'))
            item.bind('<Button-5>',
                      lambda e: canvas.yview_scroll(3, 'units'))

        inner.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox('all'))
        canvas.bind('<Button-4>',
                    lambda e: canvas.yview_scroll(-3, 'units'))
        canvas.bind('<Button-5>',
                    lambda e: canvas.yview_scroll(3, 'units'))

    def _select(opt):
        selected.set(opt)
        list_frame.pack_forget()
        list_visible[0] = False
        # Resize dialog back
        dlg.update_idletasks()
        w = max(dlg._width, dlg.winfo_reqwidth())
        h = dlg.winfo_reqheight()
        dlg.geometry(f"{w}x{h}")

    def _toggle_list(e=None):
        if list_visible[0]:
            list_frame.pack_forget()
            list_visible[0] = False
        else:
            _build_list()
            list_frame.pack(fill=tk.X, pady=(2, 0))
            list_visible[0] = True
        # Resize dialog to fit
        dlg.update_idletasks()
        w = max(dlg._width, dlg.winfo_reqwidth())
        h = dlg.winfo_reqheight()
        dlg.geometry(f"{w}x{h}")

    for w in [dropdown_frame, inner_dd, sel_label, arrow]:
        w.configure(cursor='hand2')
        w.bind('<Button-1>', _toggle_list)

    def ok():
        dlg.result = selected.get()
        dlg._ok()

    dlg._make_btn(dlg._btn_row, "Cancel", dlg._cancel)
    dlg._make_btn(dlg._btn_row, "OK", ok, primary=True)

    dlg.show()
    dlg.wait_window()
    return dlg.result


def themed_askstring(parent, title, prompt, initialvalue=""):
    """Themed single-line input dialog (for frame numbers, etc)."""
    dlg = _ThemedDialog(parent, title)

    tk.Label(dlg.body, text=prompt, fg=C['text'], bg=C['surface'],
             font=FONT_UI, anchor='w').pack(fill=tk.X, pady=(0, 8))

    entry = tk.Entry(dlg.body, bg=C['surface_3'], fg=C['text_bright'],
                     insertbackground=C['accent'], font=FONT_MONO,
                     relief=tk.FLAT, highlightthickness=1,
                     highlightbackground=C['border'],
                     highlightcolor=C['accent'])
    entry.pack(fill=tk.X, ipady=6)
    if initialvalue:
        entry.insert(0, initialvalue)
    entry.select_range(0, tk.END)
    entry.focus_set()

    def ok():
        dlg.result = entry.get()
        dlg._ok()

    entry.bind('<Return>', lambda e: ok())

    dlg._make_btn(dlg._btn_row, "Cancel", dlg._cancel)
    dlg._make_btn(dlg._btn_row, "OK", ok, primary=True)

    dlg.show()
    dlg.wait_window()
    return dlg.result


# Placeholder hints per annotation key
_KEY_HINTS = {
    "label":       "e.g. car, person, building, animal...",
    "scene":       "e.g. indoor office, outdoor park, night street...",
    "action":      "e.g. walking, talking, running, sitting...",
    "object":      "e.g. red car on the left, laptop on desk...",
    "emotion":     "e.g. happy, angry, surprised, neutral...",
    "description": "Describe what is happening in this frame...",
    "tag":         "e.g. important, review, highlight, blurry...",
    "category":    "e.g. training, validation, anomaly, reference...",
    "text":        "Any visible text, OCR content, subtitles...",
    "person":      "e.g. John Doe, unknown male, speaker #2...",
    "event":       "e.g. door opens, explosion, scene change...",
    "quality":     "e.g. sharp, blurry, overexposed, dark...",
    "anomaly":     "Describe the anomaly detected...",
    "note":        "Free-form notes about this frame...",
}


def themed_asktext(parent, title, prompt, key=None, initialvalue=""):
    """Rich multi-line text input dialog for annotation values."""
    dlg = _ThemedDialog(parent, title, width=480)

    # Prompt
    tk.Label(dlg.body, text=prompt, fg=C['text'], bg=C['surface'],
             font=FONT_UI, anchor='w').pack(fill=tk.X, pady=(0, 4))

    # Hint based on key
    hint = _KEY_HINTS.get(key, "")
    if hint:
        tk.Label(dlg.body, text=hint, fg=C['text_dim'], bg=C['surface'],
                 font=FONT_MONO_XS, anchor='w').pack(fill=tk.X, pady=(0, 8))

    # Text area with border
    text_border = tk.Frame(dlg.body, bg=C['border'])
    text_border.pack(fill=tk.BOTH, expand=True)

    text_inner = tk.Frame(text_border, bg=C['surface_3'])
    text_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

    text = tk.Text(text_inner, bg=C['surface_3'], fg=C['text_bright'],
                   insertbackground=C['accent'], font=FONT_MONO,
                   relief=tk.FLAT, highlightthickness=0,
                   wrap=tk.WORD, height=6, padx=10, pady=8,
                   undo=True, maxundo=50,
                   selectbackground=C['accent'],
                   selectforeground=C['play_fg'])
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    scrollbar = tk.Scrollbar(text_inner, orient=tk.VERTICAL,
                             command=text.yview, width=8,
                             bg=C['surface_3'], troughcolor=C['surface_2'],
                             highlightthickness=0, borderwidth=0)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    text.configure(yscrollcommand=scrollbar.set)

    if initialvalue:
        text.insert('1.0', initialvalue)
        text.tag_add('sel', '1.0', tk.END)
    text.focus_set()

    # Footer: char count + shortcuts
    footer = tk.Frame(dlg.body, bg=C['surface'])
    footer.pack(fill=tk.X, pady=(6, 0))

    char_label = tk.Label(footer, text="0 chars",
                          fg=C['text_dim'], bg=C['surface'],
                          font=FONT_MONO_XS)
    char_label.pack(side=tk.LEFT)

    tk.Label(footer, text="Ctrl+Enter to confirm",
             fg=C['text_muted'], bg=C['surface'],
             font=FONT_MONO_XS).pack(side=tk.RIGHT)

    def _update_count(e=None):
        content = text.get('1.0', f'{tk.END}-1c')
        n = len(content)
        char_label.config(text=f"{n} char{'s' if n != 1 else ''}")

    text.bind('<KeyRelease>', _update_count)
    _update_count()

    def ok():
        content = text.get('1.0', f'{tk.END}-1c').strip()
        dlg.result = content if content else None
        dlg._ok()

    text.bind('<Control-Return>', lambda e: ok())

    dlg._make_btn(dlg._btn_row, "Cancel", dlg._cancel)
    dlg._make_btn(dlg._btn_row, "OK", ok, primary=True)

    dlg.show()
    dlg.wait_window()
    return dlg.result


def themed_askyesno(parent, title, message):
    """Themed replacement for messagebox.askyesno."""
    dlg = _ThemedDialog(parent, title)

    tk.Label(dlg.body, text=message, fg=C['text'], bg=C['surface'],
             font=FONT_UI, anchor='w', wraplength=340, justify='left').pack(
                 fill=tk.X, pady=(0, 4))

    def yes():
        dlg.result = True
        dlg._ok()

    def no():
        dlg.result = False
        dlg._cancel()

    dlg._make_btn(dlg._btn_row, "No", no)
    dlg._make_btn(dlg._btn_row, "Yes", yes, primary=True)

    dlg.show()
    dlg.wait_window()
    return dlg.result


def themed_filedialog(parent, title="Select File", mode="open", initialdir=None,
                      initialfile="", filetypes=None):
    """Themed file browser dialog matching the player UI.

    mode='open' for selecting a file, mode='save' for saving.
    Returns filepath string or None if cancelled.
    """
    import os

    dlg = _ThemedDialog(parent, title, width=600)
    dlg.result = None

    if initialdir is None:
        initialdir = os.path.expanduser("~")
    current_dir = [os.path.abspath(initialdir)]

    # ── Path bar ──
    path_frame = tk.Frame(dlg.body, bg=C['surface'])
    path_frame.pack(fill=tk.X, pady=(0, 8))

    path_var = tk.StringVar(value=current_dir[0])
    path_entry = tk.Entry(path_frame, textvariable=path_var,
                          bg=C['surface_3'], fg=C['text_bright'],
                          insertbackground=C['accent'], font=FONT_MONO_SM,
                          relief=tk.FLAT, highlightthickness=1,
                          highlightbackground=C['border'],
                          highlightcolor=C['accent'])
    path_entry.pack(fill=tk.X, ipady=5)

    # ── File list area ──
    list_outer = tk.Frame(dlg.body, bg=C['border'], highlightthickness=0)
    list_outer.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

    list_inner = tk.Frame(list_outer, bg=C['surface_2'])
    list_inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

    scrollbar = tk.Scrollbar(list_inner, orient=tk.VERTICAL,
                             bg=C['surface_3'], troughcolor=C['surface_2'],
                             activebackground=C['accent_soft'],
                             highlightthickness=0, bd=0, width=10,
                             relief=tk.FLAT)
    canvas = tk.Canvas(list_inner, bg=C['surface_2'], highlightthickness=0,
                       height=300, yscrollcommand=scrollbar.set)
    scrollbar.config(command=canvas.yview)

    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    scroll_inner = tk.Frame(canvas, bg=C['surface_2'])
    canvas_win = canvas.create_window((0, 0), window=scroll_inner, anchor='nw')

    def _on_canvas_cfg(e):
        canvas.itemconfig(canvas_win, width=e.width)
    canvas.bind('<Configure>', _on_canvas_cfg)

    def _on_mousewheel(e):
        canvas.yview_scroll(-1 if e.delta > 0 or e.num == 4 else 1, "units")

    def _bind_scroll(widget):
        widget.bind('<Button-4>', _on_mousewheel)
        widget.bind('<Button-5>', _on_mousewheel)
        widget.bind('<MouseWheel>', _on_mousewheel)

    _bind_scroll(canvas)
    _bind_scroll(scroll_inner)

    scroll_inner.bind('<Configure>',
                      lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    selected_file = [None]
    file_rows = []

    def _populate(dirpath):
        for w in scroll_inner.winfo_children():
            w.destroy()
        file_rows.clear()
        selected_file[0] = None
        if mode == 'save':
            _update_filename_entry()

        try:
            entries = sorted(os.listdir(dirpath),
                             key=lambda x: (not os.path.isdir(os.path.join(dirpath, x)),
                                            x.lower()))
        except PermissionError:
            entries = []

        parent = os.path.dirname(dirpath)
        if parent != dirpath:
            _make_row("..", is_dir=True, fullpath=parent, is_parent=True)

        for name in entries:
            if name.startswith('.'):
                continue
            full = os.path.join(dirpath, name)
            is_dir = os.path.isdir(full)
            if not is_dir and filetypes and mode == 'open':
                ext = os.path.splitext(name)[1].lower()
                all_exts = set()
                for _, pattern in (filetypes or []):
                    for p in pattern.split():
                        if p == '*.*':
                            all_exts = None
                            break
                        all_exts.add(p.replace('*', '').lower())
                    if all_exts is None:
                        break
                if all_exts is not None and ext not in all_exts:
                    continue
            _make_row(name, is_dir=is_dir, fullpath=full)

        current_dir[0] = dirpath
        path_var.set(dirpath)
        canvas.yview_moveto(0)

    def _make_row(name, is_dir=False, fullpath="", is_parent=False):
        row = tk.Frame(scroll_inner, bg=C['surface_2'], cursor='hand2')
        row.pack(fill=tk.X, padx=4, pady=1)

        if is_parent:
            icon_text = "\u2190"
            icon_color = C['accent']
        elif is_dir:
            icon_text = "\U0001F4C1"
            icon_color = C['accent']
        else:
            icon_text = "\U0001F4C4"
            icon_color = C['text_dim']

        icon = tk.Label(row, text=icon_text, fg=icon_color, bg=C['surface_2'],
                        font=FONT_UI, width=3)
        icon.pack(side=tk.LEFT, padx=(6, 0))

        label = tk.Label(row, text=name, fg=C['text'] if not is_dir else C['text_bright'],
                         bg=C['surface_2'], font=FONT_UI if not is_dir else FONT_UI_BOLD,
                         anchor='w')
        label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2, 8))

        if not is_dir:
            try:
                size = os.path.getsize(fullpath)
                if size < 1024:
                    sz_text = f"{size} B"
                elif size < 1024 * 1024:
                    sz_text = f"{size/1024:.1f} KB"
                else:
                    sz_text = f"{size/(1024*1024):.1f} MB"
            except OSError:
                sz_text = ""
            sz_label = tk.Label(row, text=sz_text, fg=C['text_dim'],
                                bg=C['surface_2'], font=FONT_MONO_XS)
            sz_label.pack(side=tk.RIGHT, padx=(0, 10))

        row_data = {'name': name, 'fullpath': fullpath, 'is_dir': is_dir,
                    'row': row, 'icon': icon, 'label': label}
        file_rows.append(row_data)

        def _highlight(bg):
            row.config(bg=bg)
            icon.config(bg=bg)
            label.config(bg=bg)
            for child in row.winfo_children():
                child.config(bg=bg)

        def _on_enter(e):
            if selected_file[0] != fullpath:
                _highlight(C['surface_3'])

        def _on_leave(e):
            if selected_file[0] != fullpath:
                _highlight(C['surface_2'])

        def _on_click(e):
            if is_dir:
                _populate(fullpath)
            else:
                for fr in file_rows:
                    if not fr['is_dir']:
                        fr['row'].config(bg=C['surface_2'])
                        fr['icon'].config(bg=C['surface_2'])
                        fr['label'].config(bg=C['surface_2'])
                        for ch in fr['row'].winfo_children():
                            ch.config(bg=C['surface_2'])
                _highlight(C['accent_glow'])
                label.config(fg=C['accent'])
                selected_file[0] = fullpath
                if mode == 'save':
                    _update_filename_entry()

        def _on_dblclick(e):
            if is_dir:
                _populate(fullpath)
            else:
                selected_file[0] = fullpath
                _confirm()

        for widget in [row, icon, label] + [c for c in row.winfo_children()]:
            widget.bind('<Enter>', _on_enter)
            widget.bind('<Leave>', _on_leave)
            widget.bind('<Button-1>', _on_click)
            widget.bind('<Double-Button-1>', _on_dblclick)
            _bind_scroll(widget)

    filename_var = tk.StringVar(value=initialfile)

    def _update_filename_entry():
        if mode == 'save' and selected_file[0]:
            filename_var.set(os.path.basename(selected_file[0]))

    if mode == 'save':
        filename_frame = tk.Frame(dlg.body, bg=C['surface'])
        filename_frame.pack(fill=tk.X, pady=(0, 4))
        tk.Label(filename_frame, text="Filename:", fg=C['text_dim'],
                 bg=C['surface'], font=FONT_UI).pack(side=tk.LEFT, padx=(0, 8))
        fn_entry = tk.Entry(filename_frame, textvariable=filename_var,
                            bg=C['surface_3'], fg=C['text_bright'],
                            insertbackground=C['accent'], font=FONT_MONO_SM,
                            relief=tk.FLAT, highlightthickness=1,
                            highlightbackground=C['border'],
                            highlightcolor=C['accent'])
        fn_entry.pack(fill=tk.X, expand=True, ipady=5)

    def _on_path_enter(e):
        p = path_var.get().strip()
        if os.path.isdir(p):
            _populate(p)

    path_entry.bind('<Return>', _on_path_enter)

    def _confirm():
        if mode == 'open':
            if selected_file[0] and os.path.isfile(selected_file[0]):
                dlg.result = selected_file[0]
                dlg._ok()
        else:
            fn = filename_var.get().strip()
            if fn:
                dlg.result = os.path.join(current_dir[0], fn)
                dlg._ok()

    dlg._make_btn(dlg._btn_row, "Cancel", dlg._cancel)
    btn_text = "Open" if mode == 'open' else "Save"
    dlg._make_btn(dlg._btn_row, btn_text, _confirm, primary=True)

    _populate(current_dir[0])

    dlg.show()
    dlg.wait_window()
    return dlg.result


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
        ann_panel_w = 300 if self._ann_frames else 0
        win_w = max(900, int(self.width * scale) + 32 + ann_panel_w)
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

        # ── Main content: Video + Annotation Panel ──
        self._content = tk.Frame(self.root, bg=C['bg'])
        self._content.pack(fill=tk.BOTH, expand=True)

        # Video canvas (left)
        self.canvas = tk.Canvas(self._content, bg=C['video_bg'], highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Annotation panel (right) — hidden by default, toggled with P
        self._ann_panel_visible = False
        self._ann_panel = tk.Frame(self._content, bg=C['surface'], width=300)
        self._ann_panel_border = tk.Frame(self._content, bg=C['border'], width=1)

        # Panel header
        panel_header = tk.Frame(self._ann_panel, bg=C['surface_2'], height=44)
        panel_header.pack(fill=tk.X)
        panel_header.pack_propagate(False)

        tk.Label(panel_header, text="\u2691  Annotations",
                 fg=C['annotation'], bg=C['surface_2'],
                 font=FONT_UI_BOLD).pack(side=tk.LEFT, padx=12)

        self._ann_count_label = tk.Label(
            panel_header, text="0", fg=C['text_dim'], bg=C['surface_2'],
            font=FONT_MONO_XS)
        self._ann_count_label.pack(side=tk.RIGHT, padx=(0, 12))

        # Attach document button (paperclip)
        self._doc_add_btn = tk.Label(
            panel_header, text="\U0001F4CE", fg=C['text_dim'], bg=C['surface_3'],
            font=FONT_ACTION, cursor='hand2', padx=6, pady=2)
        self._doc_add_btn.pack(side=tk.RIGHT, padx=(0, 8))
        self._doc_add_btn.bind('<Button-1>', lambda e: self._attach_document())
        self._doc_add_btn.bind('<Enter>',
            lambda e: self._doc_add_btn.config(fg=C['accent'], bg=C['border']))
        self._doc_add_btn.bind('<Leave>',
            lambda e: self._doc_add_btn.config(fg=C['text_dim'], bg=C['surface_3']))

        # Add annotation button in panel header
        self._ann_add_btn = tk.Label(
            panel_header, text="\u271a", fg=C['accent'], bg=C['surface_3'],
            font=FONT_ACTION, cursor='hand2', padx=6, pady=2)
        self._ann_add_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self._ann_add_btn.bind('<Button-1>', lambda e: self._add_annotation())
        self._ann_add_btn.bind('<Enter>',
            lambda e: self._ann_add_btn.config(fg=C['text_bright'], bg=C['border']))
        self._ann_add_btn.bind('<Leave>',
            lambda e: self._ann_add_btn.config(fg=C['accent'], bg=C['surface_3']))

        tk.Frame(self._ann_panel, bg=C['border'], height=1).pack(fill=tk.X)

        # Scrollable list
        list_container = tk.Frame(self._ann_panel, bg=C['surface'])
        list_container.pack(fill=tk.BOTH, expand=True)

        self._ann_canvas = tk.Canvas(
            list_container, bg=C['surface'], highlightthickness=0,
            borderwidth=0)
        self._ann_scrollbar = tk.Scrollbar(
            list_container, orient=tk.VERTICAL,
            command=self._ann_canvas.yview,
            bg=C['surface_2'], troughcolor=C['surface'],
            highlightthickness=0, borderwidth=0, width=8)

        self._ann_list_frame = tk.Frame(self._ann_canvas, bg=C['surface'])
        self._ann_list_frame.bind('<Configure>',
            lambda e: self._ann_canvas.configure(
                scrollregion=self._ann_canvas.bbox('all')))

        self._ann_canvas_window = self._ann_canvas.create_window(
            (0, 0), window=self._ann_list_frame, anchor=tk.NW)
        self._ann_canvas.configure(yscrollcommand=self._ann_scrollbar.set)

        self._ann_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._ann_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Bind mouse wheel on the panel
        self._ann_canvas.bind('<Button-4>',
            lambda e: self._ann_canvas.yview_scroll(-3, 'units'))
        self._ann_canvas.bind('<Button-5>',
            lambda e: self._ann_canvas.yview_scroll(3, 'units'))
        self._ann_list_frame.bind('<Button-4>',
            lambda e: self._ann_canvas.yview_scroll(-3, 'units'))
        self._ann_list_frame.bind('<Button-5>',
            lambda e: self._ann_canvas.yview_scroll(3, 'units'))

        # Resize list frame width to match canvas
        self._ann_canvas.bind('<Configure>', self._on_ann_canvas_configure)

        # Load and show if annotations exist
        self._ann_items = []  # list of (frame_id, widget)
        self._load_annotation_list()
        if self._ann_frames:
            self._show_ann_panel()

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

        # Annotation panel toggle
        self._icon_tag_img = make_icon(28, _icon_tag, hex_rgb(C['annotation']), bg_rgb)
        tk.Button(right_ctrl, image=self._icon_tag_img,
                  command=self._toggle_ann_panel,
                  bg=C['surface'], activebackground=C['surface_2'],
                  relief=tk.FLAT, bd=0, highlightthickness=0,
                  cursor='hand2').pack(side=tk.RIGHT, padx=6)

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

        # (annotation display moved to right panel)

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
        self.root.bind('<p>', lambda e: self._toggle_ann_panel())
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

        # Zoom label
        self.zoom_label.config(text=f"{self.zoom:.0%}")

        # Annotation panel highlight
        self._highlight_current_annotation()

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
        val = themed_askstring(self.root, "Go to Frame",
                               f"Frame number (0\u2013{self.total-1}):")
        if val is not None:
            try:
                r = int(val)
                if 0 <= r < self.total:
                    self._seek_to(r)
            except ValueError:
                pass
        if was and not self.playing:
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
        key = themed_askselect(self.root, "New Annotation",
                               f"Key (frame {self.current_frame}):",
                               ANNOTATION_KEYS)
        if not key:
            if was:
                self._start_playback()
            return
        value = themed_asktext(self.root, "New Annotation",
                               f"Value for '{key}':", key=key)
        if value is None:
            if was:
                self._start_playback()
            return
        self.vh.annotate(self.current_frame, key, value)
        self.vh.commit()
        self._ann_frames.add(self.current_frame)
        self.timeline.set_annotations(
            {f / self.total for f in self._ann_frames})
        self._load_annotation_list()
        if not self._ann_panel_visible:
            self._show_ann_panel()
        self._display_frame(self.current_frame)
        self._scroll_to_annotation(self.current_frame)
        if was:
            self._start_playback()

    def _edit_annotation(self, frame_id, key, current_value):
        """Edit an existing annotation via dialog."""
        was = self.playing
        if was:
            self._stop_playback()
        import json as _json
        try:
            parsed = _json.loads(current_value)
        except (ValueError, TypeError):
            parsed = current_value
        # Show current value as default
        if isinstance(parsed, (list, dict)):
            default = _json.dumps(parsed)
        else:
            default = str(parsed)
        new_value = themed_asktext(
            self.root, "Edit Annotation",
            f"Edit '{key}' (frame {frame_id}):",
            key=key, initialvalue=default)
        if new_value is None:
            if was:
                self._start_playback()
            return
        # Try to parse as JSON
        try:
            parsed_new = _json.loads(new_value)
        except (ValueError, TypeError):
            parsed_new = new_value
        self.vh.update_annotation(frame_id, key, parsed_new)
        self.vh.commit()
        self._load_annotation_list()
        self._display_frame(self.current_frame)
        if was:
            self._start_playback()

    def _delete_annotation(self, frame_id, key):
        """Delete a single annotation after confirmation."""
        if not themed_askyesno(self.root, "Delete Annotation",
                f"Delete '{key}' from frame {frame_id}?"):
            return
        self.vh.delete_annotation(frame_id, key)
        self.vh.commit()
        # Update ann_frames set
        remaining = self.vh.get_annotations(frame_id)
        if not remaining:
            self._ann_frames.discard(frame_id)
        self.timeline.set_annotations(
            {f / self.total for f in self._ann_frames})
        self._load_annotation_list()
        self._display_frame(self.current_frame)

    def _delete_all_annotations(self, frame_id):
        """Delete all annotations from a frame after confirmation."""
        if not themed_askyesno(self.root, "Delete All Annotations",
                f"Delete all annotations from frame {frame_id}?"):
            return
        self.vh.delete_annotations(frame_id)
        self.vh.commit()
        self._ann_frames.discard(frame_id)
        self.timeline.set_annotations(
            {f / self.total for f in self._ann_frames})
        self._load_annotation_list()
        self._display_frame(self.current_frame)

    # ── Documents ──

    def _attach_document(self):
        """Attach a document to the current frame via themed file dialog."""
        filepath = themed_filedialog(
            self.root,
            title="Attach Document",
            mode="open",
            filetypes=[
                ("All files", "*.*"),
                ("PDF", "*.pdf"),
                ("Images", "*.png *.jpg *.jpeg *.gif *.webp *.bmp"),
                ("Text", "*.txt *.csv *.md *.json *.xml"),
                ("Office", "*.doc *.docx *.xls *.xlsx *.ppt *.pptx"),
            ]
        )
        if not filepath:
            return
        desc = themed_askstring(self.root, "Document Description",
                                "Description (optional):")
        doc_id = self.vh.add_document(filepath, frame_id=self.current_frame,
                                      description=desc if desc else None)
        self.vh.commit()
        self._load_annotation_list()
        if not self._ann_panel_visible:
            self._show_ann_panel()

    def _export_doc(self, doc_id):
        """Export a document to disk."""
        doc = self.vh.get_document(doc_id)
        if not doc:
            return
        filepath = themed_filedialog(
            self.root,
            title="Export Document",
            mode="save",
            initialfile=doc['filename'])
        if not filepath:
            return
        Path(filepath).write_bytes(doc['data'])

    def _delete_doc(self, doc_id, filename):
        """Delete a document after confirmation."""
        if not themed_askyesno(self.root, "Delete Document",
                f"Delete '{filename}'?"):
            return
        self.vh.delete_document(doc_id)
        self.vh.commit()
        self._load_annotation_list()

    # ── Annotation Panel ──

    def _on_ann_canvas_configure(self, event):
        self._ann_canvas.itemconfig(self._ann_canvas_window, width=event.width)

    def _toggle_ann_panel(self):
        if self._ann_panel_visible:
            self._hide_ann_panel()
        else:
            self._show_ann_panel()

    def _show_ann_panel(self):
        if self._ann_panel_visible:
            return
        self._ann_panel_visible = True
        self._ann_panel_border.pack(side=tk.RIGHT, fill=tk.Y)
        self._ann_panel.pack(side=tk.RIGHT, fill=tk.Y)
        self._ann_panel.pack_propagate(False)
        self._highlight_current_annotation()

    def _hide_ann_panel(self):
        if not self._ann_panel_visible:
            return
        self._ann_panel_visible = False
        self._ann_panel.pack_forget()
        self._ann_panel_border.pack_forget()

    def _load_annotation_list(self):
        """Load all annotations and documents from DB and build the list widgets."""
        # Clear existing
        for _, widget in self._ann_items:
            widget.destroy()
        self._ann_items = []

        # Fetch all annotations grouped by frame
        rows = self.vh._conn.execute(
            "SELECT frame_id, key, value FROM annotations ORDER BY frame_id, key"
        ).fetchall()

        # Group by frame_id
        from collections import OrderedDict
        grouped = OrderedDict()
        for fid, key, val in rows:
            if fid not in grouped:
                grouped[fid] = []
            grouped[fid].append((key, val))

        # Fetch documents per frame
        self.vh._ensure_documents_table()
        doc_rows = self.vh._conn.execute(
            "SELECT id, frame_id, filename, mime_type, size_bytes, description "
            "FROM documents ORDER BY frame_id, created_at"
        ).fetchall()
        docs_by_frame = OrderedDict()
        for did, fid, fname, mime, sz, desc in doc_rows:
            if fid not in docs_by_frame:
                docs_by_frame[fid] = []
            docs_by_frame[fid].append({'id': did, 'filename': fname,
                                        'mime_type': mime, 'size_bytes': sz,
                                        'description': desc})

        # Merge all frame IDs that have annotations or documents
        all_frames = sorted(set(grouped.keys()) | set(docs_by_frame.keys()),
                            key=lambda x: (x is None, x or 0))

        ann_count = len([f for f in all_frames if f in grouped])
        doc_count = sum(len(v) for v in docs_by_frame.values())
        count_text = str(ann_count)
        if doc_count:
            count_text += f" | {doc_count}\U0001F4CE"
        self._ann_count_label.config(text=count_text)

        if not all_frames:
            self._ann_count_label.config(text="0")
            return

        for fid in all_frames:
            anns = grouped.get(fid, [])
            docs = docs_by_frame.get(fid, [])
            item = self._create_ann_item(fid, anns, docs)
            self._ann_items.append((fid, item))

    def _create_ann_item(self, frame_id, annotations, documents=None):
        """Create a single annotation card widget."""
        if documents is None:
            documents = []

        # Card colors — high contrast
        CARD_BG = '#1e1e2a'
        CARD_HOVER = '#282838'
        CLR_TIME = '#ffffff'       # white timestamp — max readability
        CLR_FRAME = '#b0b0c8'     # bright grey for frame id
        CLR_KEY = '#80f0b0'        # bright green for keys
        CLR_VAL = '#ffffff'        # pure white for values
        CLR_BTN = '#c0c0d0'        # bright button icons
        CLR_BTN_BG = '#303044'     # button bg — visible
        CLR_DOC = '#90d8f0'        # bright doc color

        # Outer wrapper with margin
        wrapper = tk.Frame(self._ann_list_frame, bg=C['surface'])
        wrapper.pack(fill=tk.X, padx=8, pady=(6, 0))

        # Card with border (2px — visible selection)
        card_border = tk.Frame(wrapper, bg='#2a2a3a')
        card_border.pack(fill=tk.X, padx=0, pady=0)

        item = tk.Frame(card_border, bg=CARD_BG, cursor='hand2')
        item.pack(fill=tk.X, padx=2, pady=2)

        # ── Card header: accent left stripe + time + frame ──
        header = tk.Frame(item, bg=CARD_BG)
        header.pack(fill=tk.X, padx=0, pady=0)

        # Accent stripe on the left
        tk.Frame(header, bg=C['accent'], width=3).pack(side=tk.LEFT, fill=tk.Y)

        header_inner = tk.Frame(header, bg=CARD_BG)
        header_inner.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 8), pady=(10, 6))

        if frame_id is not None:
            ts = frame_id / self.fps
            ts_text = fmt_time(ts)
            fid_text = f"Frame {frame_id}"
        else:
            ts_text = "\U0001F4C1"
            fid_text = "Global"

        tk.Label(header_inner, text=ts_text,
                 fg=CLR_TIME, bg=CARD_BG,
                 font=FONT_TIME).pack(side=tk.LEFT)

        tk.Label(header_inner, text=f"  \u2022  {fid_text}",
                 fg=CLR_FRAME, bg=CARD_BG,
                 font=FONT_MONO_SM).pack(side=tk.LEFT)

        # ── Annotation entries ──
        import json as _json
        for key, val in annotations:
            try:
                parsed = _json.loads(val)
            except (ValueError, TypeError):
                parsed = val

            # Annotation block
            ann_block = tk.Frame(item, bg=CARD_BG)
            ann_block.pack(fill=tk.X, padx=(14, 8), pady=(3, 3))

            # Key row with action buttons
            key_row = tk.Frame(ann_block, bg=CARD_BG)
            key_row.pack(fill=tk.X)

            tk.Label(key_row, text=f"\u25cf  {key}",
                     fg=CLR_KEY, bg=CARD_BG,
                     font=FONT_UI_BOLD).pack(side=tk.LEFT)

            # Action buttons — visible contrast
            btn_bar = tk.Frame(key_row, bg=CARD_BG)
            btn_bar.pack(side=tk.RIGHT)

            del_btn = tk.Label(btn_bar, text=" \U0001F5D1 ", fg=CLR_BTN,
                               bg=CLR_BTN_BG, font=FONT_ACTION_SM,
                               cursor='hand2', padx=5, pady=2)
            del_btn.pack(side=tk.RIGHT, padx=(4, 0))
            del_btn.bind('<Button-1>',
                lambda e, f=frame_id, k=key: (self._delete_annotation(f, k), 'break')[1])
            del_btn.bind('<Enter>',
                lambda e, w=del_btn: w.config(fg='#ff6666', bg='#3a2a2a'))
            del_btn.bind('<Leave>',
                lambda e, w=del_btn: w.config(fg=CLR_BTN, bg=CLR_BTN_BG))

            edit_btn = tk.Label(btn_bar, text=" \u270f ", fg=CLR_BTN,
                                bg=CLR_BTN_BG, font=FONT_ACTION_SM,
                                cursor='hand2', padx=5, pady=2)
            edit_btn.pack(side=tk.RIGHT, padx=(4, 0))
            edit_btn.bind('<Button-1>',
                lambda e, f=frame_id, k=key, v=val: (self._edit_annotation(f, k, v), 'break')[1])
            edit_btn.bind('<Enter>',
                lambda e, w=edit_btn: w.config(fg='#60d8f0', bg='#1a3040'))
            edit_btn.bind('<Leave>',
                lambda e, w=edit_btn: w.config(fg=CLR_BTN, bg=CLR_BTN_BG))

            # Value — clear white on dark, with wrap
            if isinstance(parsed, list):
                display_val = ', '.join(str(x) for x in parsed)
            elif isinstance(parsed, dict):
                display_val = ', '.join(f"{k}: {v}" for k, v in parsed.items())
            else:
                display_val = str(parsed)

            if len(display_val) > 80:
                display_val = display_val[:77] + '...'

            val_label = tk.Label(ann_block, text=display_val,
                     fg=CLR_VAL, bg=CARD_BG,
                     font=FONT_MONO, anchor='w', justify='left',
                     wraplength=240)
            val_label.pack(fill=tk.X, padx=(18, 0), pady=(0, 2))

        # ── Document entries ──
        if documents:
            if annotations:
                tk.Frame(item, bg='#303044', height=1).pack(
                    fill=tk.X, padx=14, pady=(4, 4))

            for doc in documents:
                doc_block = tk.Frame(item, bg=CARD_BG)
                doc_block.pack(fill=tk.X, padx=(14, 8), pady=(3, 3))

                # Row 1: icon + filename (truncated if long)
                doc_name_row = tk.Frame(doc_block, bg=CARD_BG)
                doc_name_row.pack(fill=tk.X)

                tk.Label(doc_name_row, text="\U0001F4CE",
                         fg=CLR_DOC, bg=CARD_BG,
                         font=FONT_ACTION).pack(side=tk.LEFT)

                fname = doc['filename']
                if len(fname) > 30:
                    fname = fname[:27] + '...'
                tk.Label(doc_name_row, text=f"  {fname}",
                         fg=CLR_VAL, bg=CARD_BG,
                         font=FONT_UI, anchor='w').pack(side=tk.LEFT, fill=tk.X)

                # Row 2: size + action buttons
                doc_action_row = tk.Frame(doc_block, bg=CARD_BG)
                doc_action_row.pack(fill=tk.X, pady=(2, 0))

                sz_kb = doc['size_bytes'] / 1024
                sz_text = f"{sz_kb:.0f} KB" if sz_kb < 1024 else f"{sz_kb/1024:.1f} MB"

                tk.Label(doc_action_row, text=f"    {sz_text}",
                         fg=CLR_FRAME, bg=CARD_BG,
                         font=FONT_MONO_SM).pack(side=tk.LEFT)

                doc_exp = tk.Label(doc_action_row, text=" \u2913 Save ", fg=CLR_BTN,
                                   bg=CLR_BTN_BG, font=FONT_MONO_XS,
                                   cursor='hand2', padx=5, pady=2)
                doc_exp.pack(side=tk.RIGHT, padx=(4, 0))
                doc_exp.bind('<Button-1>',
                    lambda e, d=doc: (self._export_doc(d['id']), 'break')[1])
                doc_exp.bind('<Enter>',
                    lambda e, w=doc_exp: w.config(fg='#60d8f0', bg='#1a3040'))
                doc_exp.bind('<Leave>',
                    lambda e, w=doc_exp: w.config(fg=CLR_BTN, bg=CLR_BTN_BG))

                doc_del = tk.Label(doc_action_row, text=" \U0001F5D1 Del ", fg=CLR_BTN,
                                   bg=CLR_BTN_BG, font=FONT_MONO_XS,
                                   cursor='hand2', padx=5, pady=2)
                doc_del.pack(side=tk.RIGHT, padx=(4, 0))
                doc_del.bind('<Button-1>',
                    lambda e, d=doc: (self._delete_doc(d['id'], d['filename']), 'break')[1])
                doc_del.bind('<Enter>',
                    lambda e, w=doc_del: w.config(fg='#ff6666', bg='#3a2a2a'))
                doc_del.bind('<Leave>',
                    lambda e, w=doc_del: w.config(fg=CLR_BTN, bg=CLR_BTN_BG))

                if doc.get('description'):
                    tk.Label(doc_block, text=doc['description'],
                             fg=CLR_FRAME, bg=CARD_BG,
                             font=FONT_MONO_SM, anchor='w',
                             wraplength=220).pack(
                                 fill=tk.X, padx=(28, 0), pady=(2, 0))

        # Bottom padding
        tk.Frame(item, bg=CARD_BG, height=8).pack(fill=tk.X)

        # ── Click: entire card navigates to frame ──
        def on_click(e, fid=frame_id):
            if fid is not None:
                self._seek_to(fid)

        def _bind_click_all(widget):
            # Skip widgets that have their own action (buttons)
            if str(widget.cget('cursor')) == 'hand2' and widget is not item:
                return
            widget.bind('<Button-1>', on_click)
            for child in widget.winfo_children():
                _bind_click_all(child)

        _bind_click_all(wrapper)

        # Store card_border ref for highlight
        wrapper._card_border = card_border

        # ── Mouse wheel passthrough ──
        def _bind_scroll(widget):
            widget.bind('<Button-4>',
                lambda e: self._ann_canvas.yview_scroll(-3, 'units'))
            widget.bind('<Button-5>',
                lambda e: self._ann_canvas.yview_scroll(3, 'units'))
            for child in widget.winfo_children():
                _bind_scroll(child)

        _bind_scroll(wrapper)

        return wrapper

    def _highlight_current_annotation(self):
        """Highlight the annotation card for the current frame."""
        if not self._ann_panel_visible:
            return

        for fid, wrapper in self._ann_items:
            is_current = fid is not None and fid == self.current_frame
            if hasattr(wrapper, '_card_border'):
                if is_current:
                    wrapper._card_border.config(bg=C['accent'])
                    for child in wrapper._card_border.winfo_children():
                        if isinstance(child, tk.Frame):
                            self._set_card_bg_safe(child, '#142840')
                else:
                    wrapper._card_border.config(bg='#2a2a3a')
                    for child in wrapper._card_border.winfo_children():
                        if isinstance(child, tk.Frame):
                            self._set_card_bg_safe(child, '#1e1e2a')

    def _set_card_bg_safe(self, widget, bg):
        """Set bg recursively, skip action buttons."""
        try:
            if isinstance(widget, tk.Label) and widget.cget('cursor') == 'hand2':
                return
            widget.config(bg=bg)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._set_card_bg_safe(child, bg)

    def _scroll_to_annotation(self, frame_id):
        """Scroll the annotation list so the given frame is visible."""
        if not self._ann_panel_visible or not self._ann_items:
            return
        for idx, (fid, widget) in enumerate(self._ann_items):
            if fid >= frame_id:
                # Scroll to this item's position
                total_items = len(self._ann_items)
                if total_items > 0:
                    fraction = max(0.0, (idx - 1) / total_items)
                    self._ann_canvas.yview_moveto(fraction)
                break

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
