#!/usr/bin/env python3
"""
Desktop Recorder - a tiny Windows recorder for system (desktop) audio and microphone.

Features
  * WASAPI loopback capture of whatever is playing on your speakers/headphones
  * Optional simultaneous microphone capture on a SEPARATE track
  * Global hotkey (default Ctrl+Alt+R) to start/stop from any app
  * On stop you choose: Keep / Delete (moves to Recycle Bin) / Play
  * Crash-safe WAV writing (header patched while recording)

Usage
  pythonw recorder.py          # normal GUI launch
  python recorder.py           # same, with a console
  python recorder.py --selftest  # record 2.5s, verify files, delete them

Requires: Windows 10/11, Python 3.9+, PyAudioWPatch, numpy
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: F401  (needed before ctypes.wintypes.MSG is used)
import json
import os
import queue
import struct
import sys
import threading
import time
import winsound
from pathlib import Path

# ---------------------------------------------------------------- dependencies
try:
    import pyaudiowpatch as pyaudio
    import numpy as np
except Exception as exc:  # pragma: no cover - friendly message instead of stack
    sys.stderr.write(
        "Missing dependency: %s\n"
        "Install with:  python -m pip install PyAudioWPatch numpy\n" % exc
    )
    raise

import tkinter as tk
from tkinter import font as tkfont

APP_NAME = "Desktop Recorder"
CONFIG_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "DesktopRecorder"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_OUT = Path.home() / "Music" / "Desktop Recordings"

# ---------------------------------------------------------------- look & feel
BG = "#15171b"
CARD = "#20242a"
CARD2 = "#2a2f37"
EDGE = "#343a43"
FG = "#e9edf2"
MUTED = "#8b939f"
ACCENT = "#ff4d4f"
GREEN = "#35c46a"
BLUE = "#4da3ff"

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312


# ---------------------------------------------------------------- helpers
def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def wav_header(nbytes: int, rate: int, channels: int, bits: int = 16) -> bytes:
    block = channels * bits // 8
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + nbytes, b"WAVE",
        b"fmt ", 16, 1, channels, rate, rate * block, block, bits,
        b"data", nbytes,
    )


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def work_area() -> tuple[int, int, int, int]:
    """Usable desktop rect (excludes the taskbar), in physical pixels."""
    r = _RECT()
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
        if r.right > r.left and r.bottom > r.top:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return 0, 0, 1920, 1080


def clamp_pos(x: int, y: int, w: int, h: int, chrome: int = 36) -> tuple[int, int]:
    """Keep a window of client size w x h (plus caption) inside the work area."""
    l, t, r, b = work_area()
    x = min(max(l + 6, x), max(l + 6, r - w - 6))
    y = min(max(t + 6, y), max(t + 6, b - h - chrome - 6))
    return x, y


def darken_titlebar(win) -> None:
    """Ask DWM for a dark caption bar so the native frame matches the app."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dark = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new/old builds)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark), ctypes.sizeof(dark))
        for attr, rgb in ((35, 0x00191715), (36, 0x00F2EDE9), (34, 0x00433A34)):
            val = ctypes.c_int(rgb)  # COLORREF = 0x00BBGGRR
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def recycle(paths) -> bool:
    """Send files to the Recycle Bin so a mistaken delete is recoverable."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    items = [str(p) for p in paths if Path(p).exists()]
    if not items:
        return True
    buf = "\0".join(items) + "\0\0"
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = buf
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return res == 0


def load_config() -> dict:
    cfg = {
        "out_dir": str(DEFAULT_OUT),
        "mode": "both",              # both | sys | mic
        "loop_index": None,
        "mic_index": None,
        "always_on_top": True,
        "hotkey": "ctrl+alt+r",
        "geometry": "",
    }
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- WAV writing
class WavWriter:
    """16-bit PCM WAV writer that keeps the RIFF header valid at all times."""

    __slots__ = ("path", "_f", "_rate", "_ch", "_n", "_next_patch")

    def __init__(self, path: Path, rate: int, channels: int):
        self.path = path
        self._rate = rate
        self._ch = channels
        self._n = 0
        self._next_patch = max(65536, rate * channels * 2 // 2)  # ~0.5 s
        self._f = open(path, "wb")
        self._f.write(b"\0" * 44)

    def write(self, data: bytes) -> None:
        self._f.write(data)
        self._n += len(data)
        if self._n >= self._next_patch:
            self._next_patch = self._n + max(65536, self._rate * self._ch)
            self._patch()

    def _patch(self) -> None:
        f = self._f
        f.seek(0)
        f.write(wav_header(self._n, self._rate, self._ch))
        f.seek(0, os.SEEK_END)
        f.flush()

    def close(self) -> None:
        try:
            self._patch()
        finally:
            self._f.close()

    @property
    def size(self) -> int:
        return self._n


class Track:
    """One captured source: loopback or microphone."""

    def __init__(self, label: str, path: Path, rate: int, channels: int, device: int):
        self.label = label
        self.path = path
        self.rate = rate
        self.channels = channels
        self.device = device
        self.writer = WavWriter(path, rate, channels)
        self.frames = 0
        self.peak = 0.0
        self.error: Exception | None = None

    def callback(self, in_data, frame_count, time_info, status):  # PortAudio thread
        try:
            self.writer.write(in_data)
            if in_data:
                a = np.frombuffer(in_data, dtype=np.int16)
                if a.size:
                    p = float(np.abs(a).max()) / 32768.0
                    if p > self.peak:
                        self.peak = p
            self.frames += frame_count
        except Exception as exc:  # disk full, device yanked, ...
            self.error = exc
            return (None, pyaudio.paAbort)
        return (None, pyaudio.paContinue)

    @property
    def seconds(self) -> float:
        return self.frames / self.rate if self.rate else 0.0


# ---------------------------------------------------------------- app
class RecorderApp:
    def __init__(self, root: tk.Tk, config: dict):
        self.root = root
        self.cfg = config
        self.pa = pyaudio.PyAudio()
        self.events: queue.Queue = queue.Queue()
        self.state = "idle"
        self.tracks: list[Track] = []
        self.streams: list = []
        self.started_at = 0.0
        self.base_dir = Path(self.cfg["out_dir"])
        self.mode = self.cfg.get("mode", "both")
        self._levels = {"System": 0.0, "Microphone": 0.0}
        self._meter_w = 1
        self._hotkey_ok = True
        self._closing = False

        self._build_ui()
        try:  # keep a remembered position usable after monitor changes
            self.root.update_idletasks()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            x, y = clamp_pos(self.root.winfo_x(), self.root.winfo_y(), w, h)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass
        darken_titlebar(self.root)
        self._start_hotkey()
        self._pump()

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        r = self.root
        r.title(APP_NAME)
        r.configure(bg=BG)
        geo = self.cfg.get("geometry")
        if not geo:
            sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
            geo = f"392x214+{max(0, sw - 430)}+{max(0, sh - 330)}"
        r.geometry(geo)
        r.resizable(False, False)
        r.attributes("-topmost", bool(self.cfg.get("always_on_top", True)))
        r.protocol("WM_DELETE_WINDOW", self.on_close)
        self.var_pin = tk.BooleanVar(value=bool(self.cfg.get("always_on_top", True)))
        try:
            r.iconphoto(True, self._make_icon())
        except Exception:
            pass

        self.f_timer = tkfont.Font(family="Consolas", size=26, weight="bold")
        self.f_ui = tkfont.Font(family="Segoe UI", size=9)
        self.f_ui_b = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self.f_btn = tkfont.Font(family="Segoe UI", size=11, weight="bold")

        # status card: dot + timer + level meters
        card = tk.Frame(r, bg=CARD, highlightthickness=1, highlightbackground=EDGE)
        card.pack(fill="x", padx=12, pady=(12, 0))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=12, pady=10)

        self.dot = tk.Canvas(inner, width=10, height=10, bg=CARD, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot_id = self.dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")

        self.lbl_time = tk.Label(inner, text="00:00", bg=CARD, fg=FG, font=self.f_timer, anchor="w")
        self.lbl_time.pack(side="left")

        meters = tk.Frame(inner, bg=CARD)
        meters.pack(side="right", fill="x", expand=True, padx=(14, 0))
        self.meter_sys, self.lbl_m_sys = self._meter_row(meters, "SYS", BLUE)
        self.meter_mic, self.lbl_m_mic = self._meter_row(meters, "MIC", GREEN)

        # source selector
        row = tk.Frame(r, bg=BG)
        row.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(row, text="Source", bg=BG, fg=MUTED, font=self.f_ui).pack(side="left")
        self.var_mode = tk.StringVar(value=self._mode_label(self.mode))
        self.mode_menu = tk.OptionMenu(
            row, self.var_mode,
            self._mode_label("both"), self._mode_label("sys"), self._mode_label("mic"),
            command=self._on_mode,
        )
        self.mode_menu.config(
            bg=CARD2, fg=FG, activebackground=CARD2, activeforeground=FG,
            highlightthickness=0, bd=0, relief="flat", font=self.f_ui,
            anchor="w", padx=8, pady=3, cursor="hand2",
        )
        self.mode_menu["menu"].config(
            bg=CARD2, fg=FG, activebackground="#3b414b", activeforeground=FG, bd=0, font=self.f_ui,
        )
        self.mode_menu.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.btn_dev = tk.Label(row, text="⚙", bg=BG, fg=MUTED, font=("Segoe UI", 10), cursor="hand2")
        self.btn_dev.pack(side="right", padx=(8, 0))
        self.btn_dev.bind("<Button-1>", lambda e: self.open_devices())
        self.btn_dev.bind("<Enter>", lambda e: self.btn_dev.config(fg=FG))
        self.btn_dev.bind("<Leave>", lambda e: self.btn_dev.config(fg=MUTED))

        # record button
        self.btn_rec = tk.Button(
            r, text="●  Start recording", bg=ACCENT, fg="white", font=self.f_btn,
            activebackground="#e03e40", activeforeground="white",
            bd=0, relief="flat", cursor="hand2", command=self.toggle_recording,
        )
        self.btn_rec.pack(fill="x", padx=12, pady=(10, 0), ipady=8)

        # footer
        foot = tk.Frame(r, bg=BG)
        foot.pack(fill="x", padx=12, pady=(8, 10))
        self.btn_folder = tk.Label(foot, text="▸ Open folder", bg=BG, fg=MUTED, font=self.f_ui, cursor="hand2")
        self.btn_folder.pack(side="left")
        self.btn_folder.bind("<Button-1>", lambda e: self.open_folder())
        self.btn_folder.bind("<Enter>", lambda e: self.btn_folder.config(fg=FG))
        self.btn_folder.bind("<Leave>", lambda e: self.btn_folder.config(fg=MUTED))

        self.btn_choose = tk.Label(foot, text="Change…", bg=BG, fg=MUTED, font=self.f_ui, cursor="hand2")
        self.btn_choose.pack(side="right")
        self.btn_choose.bind("<Button-1>", lambda e: self.choose_folder())
        self.btn_choose.bind("<Enter>", lambda e: self.btn_choose.config(fg=FG))
        self.btn_choose.bind("<Leave>", lambda e: self.btn_choose.config(fg=MUTED))

        self.btn_pin = tk.Label(foot, text="● On top", bg=BG, fg=MUTED, font=self.f_ui, cursor="hand2")
        self.btn_pin.pack(side="right", padx=(0, 14))
        self.btn_pin.bind("<Button-1>", self._toggle_pin)
        self.btn_pin.bind("<Enter>", lambda e: self.btn_pin.config(fg=FG))
        self.btn_pin.bind("<Leave>", lambda e: self._paint_pin())

        self.lbl_hint = tk.Label(r, text="Ctrl+Alt+R to start / stop", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.lbl_hint.pack(side="bottom", pady=(0, 6))

        self.menu = tk.Menu(r, tearoff=0, bg=CARD2, fg=FG, activebackground="#3b414b",
                            activeforeground=FG, bd=0, font=self.f_ui)
        self.menu.add_command(label="Recordings folder", command=self.open_folder)
        self.menu.add_command(label="Devices…", command=self.open_devices)
        self.menu.add_checkbutton(label="Always on top", variable=self.var_pin, command=self._toggle_pin)
        self.menu.add_separator()
        self.menu.add_command(label="Exit", command=self.on_close)
        r.bind("<Button-3>", self._popup_menu)

        self._paint_pin()
        self._update_source_glyphs()

        # NB: no in-app Ctrl+R binding - the global hotkey already covers the app
        # window, and having both would toggle twice (start+stop) per keypress.
        r.bind("<space>", lambda e: self._space_toggle(e))

    def _popup_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _paint_pin(self):
        on = bool(self.var_pin.get())
        self.btn_pin.config(text="● On top" if on else "○ On top", fg=FG if on else MUTED)

    def _space_toggle(self, e):
        # space only when the record button has focus, so it never fights other widgets
        if e.widget is self.btn_rec:
            self.toggle_recording()
            return "break"

    def _meter_row(self, parent, label, color):
        rowf = tk.Frame(parent, bg=CARD)
        rowf.pack(fill="x", pady=1)
        lbl = tk.Label(rowf, text=label, bg=CARD, fg=MUTED, font=("Segoe UI", 7, "bold"), width=4, anchor="w")
        lbl.pack(side="left")
        c = tk.Canvas(rowf, height=6, bg=CARD2, highlightthickness=0, bd=0)
        c.pack(side="left", fill="x", expand=True)
        return c, lbl

    def _mode_label(self, mode: str) -> str:
        return {
            "both": "System + Mic (2 tracks)",
            "sys": "System audio only",
            "mic": "Microphone only",
        }.get(mode, mode)

    def _mode_from_label(self, label: str) -> str:
        return {
            "System + Mic (2 tracks)": "both",
            "System audio only": "sys",
            "Microphone only": "mic",
        }.get(label, "both")

    def _on_mode(self, label: str) -> None:
        if self.state == "recording":
            self.var_mode.set(self._mode_label(self.mode))
            self.flash("Stop the recording before changing source.")
            return
        self.mode = self._mode_from_label(label)
        self.cfg["mode"] = self.mode
        save_config(self.cfg)
        self._update_source_glyphs()

    def _update_source_glyphs(self):
        sys_on = self.mode in ("both", "sys")
        mic_on = self.mode in ("both", "mic")
        self.lbl_m_sys.config(fg=MUTED if sys_on else "#4a505a")
        self.lbl_m_mic.config(fg=MUTED if mic_on else "#4a505a")

    def _make_icon(self):
        img = tk.PhotoImage(width=32, height=32)
        img.put(BG, to=(0, 0, 32, 32))
        img.put(ACCENT, to=(7, 7, 25, 25))
        return img

    # ------------------------------------------------------------ window chrome
    def _toggle_pin(self, _e=None):
        on = not bool(self.var_pin.get())
        self.var_pin.set(on)
        self.root.attributes("-topmost", on)
        self.cfg["always_on_top"] = on
        save_config(self.cfg)
        self._paint_pin()

    # ------------------------------------------------------------ hotkey
    def _start_hotkey(self) -> None:
        spec = str(self.cfg.get("hotkey", "ctrl+alt+r")).lower().replace(" ", "")
        mods = 0
        key = ""
        for part in spec.split("+"):
            if part == "ctrl":
                mods |= MOD_CONTROL
            elif part == "alt":
                mods |= MOD_ALT
            elif part == "shift":
                mods |= MOD_SHIFT
            elif part in ("win", "windows"):
                mods |= MOD_WIN
            elif part:
                key = part
        if not (len(key) == 1 and key.isalnum()):
            self._hotkey_ok = False
            return
        vk = ord(key.upper())

        def run():
            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk):
                self.events.put(("hotkey_fail",))
                return
            self.events.put(("hotkey_ok",))
            msg = ctypes.wintypes.MSG()
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got <= 0:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == 1:
                    self.events.put(("toggle",))
            user32.UnregisterHotKey(None, 1)

        threading.Thread(target=run, daemon=True, name="hotkey").start()

    # ------------------------------------------------------------ event pump
    def _pump(self) -> None:
        try:
            while True:
                kind, *rest = self.events.get_nowait()
                if kind == "toggle":
                    self.toggle_recording()
                elif kind == "hotkey_fail":
                    self._hotkey_ok = False
                    self.lbl_hint.config(text="Hotkey unavailable (already in use)")
                elif kind == "hotkey_ok":
                    self.lbl_hint.config(text=f"{self.cfg.get('hotkey', 'ctrl+alt+r').upper()} to start / stop")
                elif kind == "flash":
                    self.flash(rest[0])
        except queue.Empty:
            pass
        self._tick_meter()
        if not self._closing:
            self.root.after(50, self._pump)

    def _tick_meter(self) -> None:
        if self.state == "recording":
            elapsed = time.monotonic() - self.started_at
            self.lbl_time.config(text=f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}", fg=FG)
        for name, canvas, color in (
            ("System", self.meter_sys, BLUE),
            ("Microphone", self.meter_mic, GREEN),
        ):
            target = 0.0
            for t in self.tracks:
                if t.label == name:
                    target = t.peak
                    t.peak = 0.0  # consume
                    break
            cur = self._levels[name] = max(target, self._levels.get(name, 0.0) * 0.80)
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            canvas.delete("all")
            if w > 2:
                fill = int((w - 2) * min(1.0, cur ** 0.6))
                col = ACCENT if cur > 0.985 else color
                if fill > 0:
                    canvas.create_rectangle(1, 1, 1 + fill, h - 1, fill=col, outline="")

    # ------------------------------------------------------------ devices
    def loopbacks(self):
        try:
            return list(self.pa.get_loopback_device_info_generator())
        except Exception:
            return []

    def inputs(self):
        out = []
        for d in self.pa.get_device_info_generator():
            if d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice"):
                out.append(d)
        return out

    def default_loopback(self):
        try:
            return self.pa.get_default_wasapi_loopback()
        except Exception:
            devs = self.loopbacks()
            return devs[0] if devs else None

    def _resolve_loopback(self):
        idx = self.cfg.get("loop_index")
        if idx is not None:
            for d in self.loopbacks():
                if d["index"] == idx:
                    return d
        # the loopback of the *default output* is what the user actually hears
        d = self.default_loopback()
        if d:
            return d
        devs = self.loopbacks()
        return devs[0] if devs else None

    def _resolve_mic(self):
        idx = self.cfg.get("mic_index")
        if idx is not None:
            for d in self.inputs():
                if d["index"] == idx:
                    return d
        try:
            return self.pa.get_default_input_device_info()
        except Exception:
            devs = self.inputs()
            return devs[0] if devs else None

    # ------------------------------------------------------------ recording
    def toggle_recording(self) -> None:
        if self.state == "recording":
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if self.state == "recording":
            return
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.flash(f"Cannot create folder: {exc}")
            return

        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        base = f"rec_{stamp}"
        specs = []  # (Track, device_index)
        try:
            if self.mode in ("both", "sys"):
                d = self._resolve_loopback()
                if not d:
                    raise RuntimeError("No system-audio (loopback) device found.")
                ch = min(2, int(d["maxInputChannels"]))
                rate = int(d["defaultSampleRate"])
                suffix = "_sys" if self.mode == "both" else ""
                t = Track("System", self.base_dir / f"{base}{suffix}.part.wav", rate, ch, d["index"])
                specs.append((t, d["index"]))

            if self.mode in ("both", "mic"):
                d = self._resolve_mic()
                if not d:
                    raise RuntimeError("No microphone device found.")
                ch = min(2, int(d["maxInputChannels"]))
                rate = int(d["defaultSampleRate"])
                suffix = "_mic" if self.mode == "both" else ""
                t = Track("Microphone", self.base_dir / f"{base}{suffix}.part.wav", rate, ch, d["index"])
                specs.append((t, d["index"]))
        except Exception as exc:
            self.flash(str(exc))
            return

        self.tracks = [s[0] for s in specs]
        self.streams = []
        try:
            for track, dev in specs:
                s = self.pa.open(
                    format=pyaudio.paInt16,
                    channels=track.channels,
                    rate=track.rate,
                    input=True,
                    input_device_index=dev,
                    frames_per_buffer=1024,
                    stream_callback=track.callback,
                    start=False,
                )
                self.streams.append(s)
        except Exception as exc:
            for s in self.streams:
                try:
                    s.close()
                except Exception:
                    pass
            for t in self.tracks:
                try:
                    t.writer.close()
                except Exception:
                    pass
                try:
                    t.path.unlink()
                except Exception:
                    pass
            self.tracks, self.streams = [], []
            self.flash(f"Could not open device: {exc}")
            return

        for s in self.streams:
            try:
                s.start_stream()
            except Exception as exc:
                self.flash(f"Could not start stream: {exc}")
                self._teardown_streams()
                return

        self.state = "recording"
        self.started_at = time.monotonic()
        self.btn_rec.config(text="■  Stop & review", bg="#3a3f47", activebackground="#454b55")
        self.dot.itemconfig(self._dot_id, fill=ACCENT)
        self._update_source_glyphs()
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    def _teardown_streams(self) -> None:
        for s in self.streams:
            try:
                s.stop_stream()
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
        self.streams = []
        for t in self.tracks:
            try:
                t.writer.close()
            except Exception:
                pass

    def stop_recording(self) -> None:
        if self.state != "recording":
            return
        duration = time.monotonic() - self.started_at
        self._teardown_streams()
        self.state = "idle"
        self.btn_rec.config(text="●  Start recording", bg=ACCENT, activebackground="#e03e40")
        self.dot.itemconfig(self._dot_id, fill=MUTED)
        self.lbl_time.config(text="00:00")
        self._levels = {k: 0.0 for k in self._levels}
        try:
            winsound.MessageBeep(winsound.MB_OK)
        except Exception:
            pass

        audio_tracks = [t for t in self.tracks if t.frames > 0]
        leftovers = [t for t in self.tracks if t.frames <= 0]
        errors = [t.error for t in self.tracks if t.error]
        self.tracks = []

        for t in leftovers:  # nothing captured: drop empty files
            try:
                t.path.unlink()
            except Exception:
                pass

        if not audio_tracks:
            self.flash("Nothing was captured (device is silent or busy).")
            return

        # restore window if it was hidden/minimized so the dialog is visible
        try:
            if self.root.state() == "iconic":
                self.root.deiconify()
        except Exception:
            pass

        choice = self.ask_keep(audio_tracks, duration)
        if choice == "keep":
            self._finalize(audio_tracks)
        else:
            recycle([t.path for t in audio_tracks])
        if errors:
            self.flash("Warning: a stream stopped early (device issue?).")

    def _finalize(self, tracks) -> None:
        kept = []
        for t in tracks:
            final = t.path.with_name(t.path.name.replace(".part.wav", ".wav"))
            n = 2
            while final.exists():
                final = t.path.with_name(t.path.name.replace(".part.wav", f" ({n}).wav"))
                n += 1
            try:
                os.replace(t.path, final)
                kept.append(final)
            except Exception as exc:
                self.flash(f"Could not save: {exc}")
        if kept:
            self.last_saved = kept
            self.flash("Saved: " + ", ".join(p.name for p in kept))

    # ------------------------------------------------------------ keep/delete
    def ask_keep(self, tracks, duration: float) -> str:
        dlg = tk.Toplevel(self.root)
        dlg.title("Recording finished")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(self.root)
        result = {"v": "keep"}

        w = 420
        rows = len(tracks)
        h = 168 + 46 * rows
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2
        x, y = clamp_pos(x, y, w, h)
        dlg.geometry(f"{w}x{h}+{x}+{y}")
        darken_titlebar(dlg)

        tk.Label(dlg, text="Recording finished", bg=BG, fg=FG, font=("Segoe UI", 12, "bold")).pack(
            anchor="w", padx=18, pady=(16, 2)
        )
        mins, secs = divmod(int(duration), 60)
        tk.Label(
            dlg, text=f"{mins:02d}:{secs:02d}  •  {len(tracks)} track(s)", bg=BG, fg=MUTED, font=("Segoe UI", 9)
        ).pack(anchor="w", padx=18)

        body = tk.Frame(dlg, bg=BG)
        body.pack(fill="x", padx=14, pady=(10, 0))
        for t in tracks:
            row = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=EDGE)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=t.label, bg=CARD, fg=BLUE if t.label == "System" else GREEN,
                     font=("Segoe UI", 9, "bold"), width=11, anchor="w").pack(side="left", padx=(10, 0), pady=7)
            tk.Label(row, text=human_size(t.path.stat().st_size), bg=CARD, fg=MUTED,
                     font=("Segoe UI", 8)).pack(side="left")
            play = tk.Label(row, text="▶ play", bg=CARD, fg=MUTED, font=("Segoe UI", 8), cursor="hand2")
            play.pack(side="right", padx=10)
            play.bind("<Button-1>", lambda e, p=t.path: self._play(p))
            play.bind("<Enter>", lambda e, w=play: w.config(fg=FG))
            play.bind("<Leave>", lambda e, w=play: w.config(fg=MUTED))

        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", padx=16, pady=14)

        def choose(v):
            result["v"] = v
            dlg.destroy()

        del_btn = tk.Button(bf, text="Delete", bg=CARD2, fg="#ff8a8c", font=("Segoe UI", 10),
                            bd=0, relief="flat", cursor="hand2", activebackground="#3b414b",
                            activeforeground="#ff8a8c", command=lambda: choose("delete"))
        del_btn.pack(side="left", ipadx=16, ipady=6)
        keep_btn = tk.Button(bf, text="Keep", bg=GREEN, fg="#08130c", font=("Segoe UI", 10, "bold"),
                             bd=0, relief="flat", cursor="hand2", activebackground="#2eae5e",
                             activeforeground="#08130c", command=lambda: choose("keep"))
        keep_btn.pack(side="right", ipadx=26, ipady=6)

        tk.Label(bf, text="  Enter = keep   Esc = delete", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="right", padx=10)

        dlg.bind("<Return>", lambda e: choose("keep"))
        dlg.bind("<Escape>", lambda e: choose("delete"))
        dlg.protocol("WM_DELETE_WINDOW", lambda: choose("keep"))
        dlg.grab_set()
        # make sure the dialog really owns the keyboard even when the app was
        # triggered from the global hotkey while another program had focus
        dlg.lift()
        dlg.focus_force()
        keep_btn.focus_set()
        dlg.after(80, lambda: (dlg.lift(), dlg.focus_force(), keep_btn.focus_set()))
        self.root.wait_window(dlg)
        return result["v"]

    def _play(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except Exception as exc:
            self.flash(f"Cannot play: {exc}")

    # ------------------------------------------------------------ misc UI
    def flash(self, text: str, ms: int = 4200) -> None:
        self.lbl_hint.config(text=text, fg="#ffd479")
        try:
            self.root.after(ms, lambda: self.lbl_hint.config(
                text=f"{self.cfg.get('hotkey', 'ctrl+alt+r').upper()} to start / stop", fg=MUTED))
        except Exception:
            pass

    def open_folder(self) -> None:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(self.base_dir))
        except Exception as exc:
            self.flash(f"Cannot open folder: {exc}")

    def choose_folder(self) -> None:
        from tkinter import filedialog
        p = filedialog.askdirectory(initialdir=str(self.base_dir), title="Recordings folder")
        if p:
            self.base_dir = Path(p)
            self.cfg["out_dir"] = str(self.base_dir)
            save_config(self.cfg)
            self.flash(f"Saving to {self.base_dir}")

    def open_devices(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Devices")
        dlg.configure(bg=BG)
        dlg.attributes("-topmost", True)
        dlg.transient(self.root)
        dlg.geometry("520x430")
        darken_titlebar(dlg)

        loops = self.loopbacks()
        mics = self.inputs()

        tk.Label(dlg, text="System audio (loopback)", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(
            anchor="w", padx=16, pady=(14, 4))
        lb1 = tk.Listbox(dlg, bg=CARD, fg=FG, selectbackground=CARD2, selectforeground=FG,
                         highlightthickness=1, highlightbackground=EDGE, bd=0, font=("Segoe UI", 9),
                         activestyle="none", height=6)
        lb1.pack(fill="x", padx=16)

        tk.Label(dlg, text="Microphone", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(
            anchor="w", padx=16, pady=(12, 4))
        lb2 = tk.Listbox(dlg, bg=CARD, fg=FG, selectbackground=CARD2, selectforeground=FG,
                         highlightthickness=1, highlightbackground=EDGE, bd=0, font=("Segoe UI", 9),
                         activestyle="none", height=6)
        lb2.pack(fill="x", padx=16)

        cur_loop = self.cfg.get("loop_index")
        cur_mic = self.cfg.get("mic_index")
        try:
            dflt = self.pa.get_default_wasapi_loopback()
            dflt_loop = dflt["index"] if dflt else None
        except Exception:
            dflt_loop = None
        for i, d in enumerate(loops):
            star = "★ " if d["index"] == dflt_loop else "  "
            lb1.insert("end", f"{star}{d['name']}   ({int(d['defaultSampleRate'])} Hz)")
            if d["index"] == cur_loop or (cur_loop is None and d["index"] == dflt_loop):
                lb1.selection_set(i)
        for i, d in enumerate(mics):
            lb2.insert("end", f"  {d['name']}   ({int(d['defaultSampleRate'])} Hz, {d['maxInputChannels']} ch)")
            if d["index"] == cur_mic or (cur_mic is None and i == 0):
                lb2.selection_set(i)

        note = tk.Label(dlg, text="Tip: pick the device you actually hear audio on. "
                                  "If it is missing, play something and reopen this window.",
                        bg=BG, fg=MUTED, font=("Segoe UI", 8), wraplength=480, justify="left")
        note.pack(anchor="w", padx=16, pady=(10, 0))

        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", padx=16, pady=12)

        def save():
            s1 = lb1.curselection()
            s2 = lb2.curselection()
            if s1:
                self.cfg["loop_index"] = loops[s1[0]]["index"]
            if s2:
                self.cfg["mic_index"] = mics[s2[0]]["index"]
            save_config(self.cfg)
            dlg.destroy()
            self.flash("Devices updated.")

        tk.Button(bf, text="Save", bg=GREEN, fg="#08130c", font=("Segoe UI", 10, "bold"), bd=0,
                  relief="flat", cursor="hand2", command=save).pack(side="right", ipadx=22, ipady=5)
        tk.Button(bf, text="Cancel", bg=CARD2, fg=FG, font=("Segoe UI", 10), bd=0, relief="flat",
                  cursor="hand2", activebackground="#3b414b", command=dlg.destroy).pack(
            side="right", padx=8, ipadx=14, ipady=5)
        tk.Button(bf, text="Refresh", bg=CARD2, fg=FG, font=("Segoe UI", 10), bd=0, relief="flat",
                  cursor="hand2", activebackground="#3b414b",
                  command=lambda: (dlg.destroy(), self.open_devices())).pack(side="left", ipadx=14, ipady=5)

    # ------------------------------------------------------------ shutdown
    def on_close(self) -> None:
        if self.state == "recording":
            self.stop_recording()
        self._closing = True
        try:
            self.cfg["geometry"] = self.root.geometry()
            save_config(self.cfg)
        except Exception:
            pass
        try:
            self.pa.terminate()
        except Exception:
            pass
        self.root.destroy()


# ---------------------------------------------------------------- selftest
def selftest() -> int:
    """Record 2.5 s from loopback + mic, verify WAV files, then delete them."""
    pa = pyaudio.PyAudio()
    tmp = Path(os.getenv("TEMP", ".")) / "deskrec_selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    made = []
    try:
        loop = pa.get_default_wasapi_loopback()
        mic = pa.get_default_input_device_info()
        print("loopback:", loop["index"], loop["name"], int(loop["defaultSampleRate"]), "Hz")
        print("mic     :", mic["index"], mic["name"], int(mic["defaultSampleRate"]), "Hz")

        tracks, streams = [], []
        for label, d in (("System", loop), ("Microphone", mic)):
            ch = min(2, int(d["maxInputChannels"]))
            t = Track(label, tmp / f"{label}.part.wav", int(d["defaultSampleRate"]), ch, d["index"])
            tracks.append(t)
            made.append(t.path)
            s = pa.open(format=pyaudio.paInt16, channels=ch, rate=t.rate, input=True,
                        input_device_index=d["index"], frames_per_buffer=1024,
                        stream_callback=t.callback, start=False)
            streams.append(s)
        for s in streams:
            s.start_stream()
        time.sleep(2.5)
        for s in streams:
            s.stop_stream()
            s.close()
        for t in tracks:
            t.writer.close()

        ok = True
        for t in tracks:
            with open(t.path, "rb") as f:
                head = f.read(44)
            riff, sz, wave = struct.unpack("<4sI4s", head[:12])
            n = t.path.stat().st_size - 44
            dur = n / (t.rate * t.channels * 2)
            print(f"{t.label:11s} {t.path.name:22s} frames={t.frames:7d} "
                  f"bytes={n:8d} dur={dur:5.2f}s header_ok={riff == b'RIFF' and wave == b'WAVE'} "
                  f"riff_size_ok={sz == n + 36}")
            ok = ok and riff == b"RIFF" and wave == b"WAVE" and sz == n + 36 and dur > 1.8
    except Exception as exc:
        print("SELFTEST FAILED:", type(exc).__name__, exc)
        ok = False
    finally:
        for p in made:
            try:
                p.unlink()
            except Exception:
                pass
        try:
            tmp.rmdir()
        except Exception:
            pass
        pa.terminate()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------- main
def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    cfg = load_config()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = RecorderApp(root, cfg)
    if not app._hotkey_ok:
        app.flash("Hotkey could not be registered.")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
