"""BreathCue - listens to your microphone and tells you when to take a breath.

A small always-on-top bar shows your "air". It drains while you talk and
refills when you pause. When it runs low it turns amber ("breathe soon"),
and when it runs out it flashes red and chimes ("BREATHE").

Drag to move. Right-click for settings / pause / quit.
"""
import ctypes
import json
import math
import os
import sys
import tempfile
import time
import wave
import winsound

import numpy as np

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "limit_s": 6.0,       # seconds of continuous talking before "BREATHE"
    "warn_frac": 0.7,     # fraction of the limit where "breathe soon" starts
    "sensitivity": 6,     # 1 (needs loud voice) .. 10 (picks up quiet voice)
    "chime": True,
    "chime_volume": 0.35,
    "mic": "",            # "" = Windows default input
    "opacity": 0.92,
    "x": None,
    "y": None,
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


class Detector:
    """Turns a stream of loudness readings into 'how long since a breath'."""

    HANGOVER_S = 0.15     # voice stays "on" this long after the last loud block
    PAUSE_S = 0.15        # extra silence needed before a pause counts as a breath
    REFILL_S = 0.40       # further silence needed to fully refill from empty

    def __init__(self, cfg):
        self.cfg = cfg
        self.floor_db = None      # running estimate of background noise
        self.level_db = -90.0
        self.spoken = 0.0         # seconds of talking since last full breath
        self.speaking = False
        self._hang = 0.0
        self._silence = 0.0
        self.ignore_loud_until = 0.0   # set while our own chime is playing
        self._warmup = 0.0

    def restart(self):
        """Call when a (new) microphone stream is opened."""
        self.reset()
        self.floor_db = None
        self._warmup = 0.3

    @property
    def margin_db(self):
        return 24.0 - 2.0 * float(self.cfg["sensitivity"])

    @property
    def frac(self):
        return self.spoken / max(0.5, float(self.cfg["limit_s"]))

    @property
    def stage(self):
        f = self.frac
        if f >= 1.0:
            return "alert"
        if f >= float(self.cfg["warn_frac"]):
            return "warn"
        return "ok"

    def reset(self):
        self.spoken = 0.0
        self.speaking = False
        self._hang = 0.0
        self._silence = 0.0

    def process(self, rms, dt, now=None):
        db = 20.0 * math.log10(max(rms, 1e-7))
        self.level_db = db
        if self._warmup > 0.0:   # mics often deliver junk/zeros right after opening
            self._warmup -= dt
            return

        # Noise floor: drops quickly to any quieter level, creeps up slowly.
        if self.floor_db is None:
            self.floor_db = db
        elif db < self.floor_db:
            self.floor_db += (db - self.floor_db) * min(1.0, dt * 8.0)
        else:
            self.floor_db += 0.7 * dt
        self.floor_db = max(-75.0, min(-20.0, self.floor_db))

        loud = db > self.floor_db + self.margin_db and db > -60.0
        if loud and now is not None and now < self.ignore_loud_until:
            return  # probably our own chime coming back through the mic

        if loud:
            self._hang = self.HANGOVER_S
        else:
            self._hang -= dt
        self.speaking = self._hang > 0.0

        limit = max(0.5, float(self.cfg["limit_s"]))
        if self.speaking:
            self._silence = 0.0
            self.spoken = min(self.spoken + dt, limit * 3.0)
        else:
            self._silence += dt
            if self._silence >= self.PAUSE_S:
                refill_rate = max(self.spoken, limit) / self.REFILL_S
                self.spoken = max(0.0, self.spoken - dt * refill_rate)


def write_chime(path, volume):
    sr = 44100
    out = np.zeros(int(sr * 0.42), dtype=np.float64)
    for start, freq in ((0.0, 659.25), (0.13, 880.0)):
        n = int(sr * 0.28)
        t = np.arange(n) / sr
        env = np.minimum(1.0, t / 0.008) * np.exp(-t * 14.0)
        tone = np.sin(2 * np.pi * freq * t) * env
        i = int(start * sr)
        out[i:i + n] += tone[: len(out) - i]
    out = np.clip(out * volume, -1.0, 1.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((out * 32767).astype("<i2").tobytes())


def lerp_color(a, b, t):
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


BG = (24, 26, 31)
GREEN = (64, 192, 120)
AMBER = (240, 170, 50)
RED = (232, 60, 60)
DARK_RED = (96, 20, 20)
TRACK = (48, 52, 60)
TEXT = "#e8eaed"
MUTED = "#8a9099"


class App:
    W, H = 300, 80
    CHIME_EVERY_S = 2.5

    def __init__(self, smoke=False):
        import tkinter as tk
        self.tk = tk
        self.smoke = smoke
        self.cfg = load_config()
        self.det = Detector(self.cfg)
        self.stream = None
        self.mic_error = ""
        self.paused = False
        self.last_chime = 0.0
        self.settings_win = None
        self.chime_path = os.path.join(tempfile.gettempdir(), "breathcue_chime.wav")
        self._make_chime()

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = root = tk.Tk()
        root.title("BreathCue")
        self.S = S = root.winfo_fpixels("1i") / 96.0
        w, h = int(self.W * S), int(self.H * S)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", float(self.cfg["opacity"]))

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        x, y = self.cfg.get("x"), self.cfg.get("y")
        vx = ctypes.windll.user32.GetSystemMetrics(76)
        vy = ctypes.windll.user32.GetSystemMetrics(77)
        vw = ctypes.windll.user32.GetSystemMetrics(78)
        vh = ctypes.windll.user32.GetSystemMetrics(79)
        if (not isinstance(x, int) or not isinstance(y, int)
                or not (vx <= x <= vx + vw - w) or not (vy <= y <= vy + vh - h)):
            x, y = (sw - w) // 2, int(40 * S)
        root.geometry(f"{w}x{h}+{x}+{y}")

        c = self.canvas = tk.Canvas(root, width=w, height=h, highlightthickness=0,
                                    bg=lerp_color(BG, BG, 0))
        c.pack(fill="both", expand=True)
        p = lambda v: int(v * S)
        self.bar_x0, self.bar_x1 = p(14), p(self.W - 14)
        self.t_status = c.create_text(p(14), p(21), anchor="w", fill=TEXT,
                                      font=("Segoe UI", 13, "bold"), text="Starting…")
        self.t_time = c.create_text(p(self.W - 14), p(21), anchor="e", fill=MUTED,
                                    font=("Segoe UI", 10), text="")
        c.create_rectangle(self.bar_x0, p(38), self.bar_x1, p(58), width=0,
                           fill=lerp_color(TRACK, TRACK, 0), tags="track")
        self.r_bar = c.create_rectangle(self.bar_x0, p(38), self.bar_x1, p(58), width=0,
                                        fill=lerp_color(GREEN, GREEN, 0))
        # thin mic meter: shows that the app can hear you
        self.m_y0, self.m_y1 = p(67), p(70)
        c.create_rectangle(self.bar_x0, self.m_y0, self.bar_x1, self.m_y1, width=0,
                           fill=lerp_color(TRACK, TRACK, 0), tags="track")
        self.r_meter = c.create_rectangle(self.bar_x0, self.m_y0, self.bar_x0, self.m_y1,
                                          width=0, fill=MUTED)
        self.r_thresh = c.create_rectangle(0, self.m_y0 - p(2), 0, self.m_y1 + p(2),
                                           width=0, fill=TEXT)

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Settings…", command=self.open_settings)
        self.menu.add_command(label="Pause listening", command=self.toggle_pause)
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.quit)

        c.bind("<ButtonPress-1>", self._drag_start)
        c.bind("<B1-Motion>", self._drag_move)
        c.bind("<ButtonRelease-1>", self._drag_end)
        c.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        c.bind("<Double-Button-1>", lambda e: self.open_settings())

        self.start_stream()
        self._t0 = time.monotonic()
        root.after(30, self.tick)

    # ---------- audio ----------
    def input_devices(self):
        import sounddevice as sd
        try:
            api = sd.default.hostapi
            return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                    if d["max_input_channels"] > 0 and d["hostapi"] == api]
        except Exception:
            return []

    def start_stream(self):
        import sounddevice as sd
        self.stop_stream()
        self.det.restart()
        device = None
        want = self.cfg.get("mic") or ""
        if want:
            for i, name in self.input_devices():
                if name == want:
                    device = i
                    break
        try:
            info = sd.query_devices(device, "input")
            sr = int(info["default_samplerate"])
            self.stream = sd.InputStream(device=device, channels=1, samplerate=sr,
                                         blocksize=int(sr * 0.02), dtype="float32",
                                         callback=self._audio_cb)
            self.stream.start()
            self.mic_name = info["name"]
            self.mic_error = ""
        except Exception as e:  # no mic, device busy, privacy setting, ...
            self.stream = None
            self.mic_error = str(e).splitlines()[0][:120]

    def stop_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _audio_cb(self, indata, frames, t, status):
        rms = float(np.sqrt(np.mean(np.square(indata, dtype=np.float64))))
        self.det.process(rms, frames / self.stream.samplerate, time.monotonic())

    # ---------- chime ----------
    def _make_chime(self):
        try:
            winsound.PlaySound(None, 0)
            write_chime(self.chime_path, float(self.cfg["chime_volume"]))
        except Exception:
            pass

    def play_chime(self):
        now = time.monotonic()
        self.last_chime = now
        self.det.ignore_loud_until = now + 0.6
        try:
            winsound.PlaySound(self.chime_path, winsound.SND_FILENAME
                               | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass

    # ---------- UI loop ----------
    def tick(self):
        c, det, now = self.canvas, self.det, time.monotonic()
        limit = float(self.cfg["limit_s"])
        stage = det.stage
        bg = BG
        if self.mic_error:
            status, color, remaining = "No microphone", MUTED, 1.0
        elif self.paused:
            status, color, remaining = "Paused", MUTED, 1.0
        else:
            remaining = max(0.0, 1.0 - det.frac)
            if stage == "alert":
                pulse = 0.5 + 0.5 * math.sin(now * 2 * math.pi * 1.6)
                bg = tuple(DARK_RED[i] + (RED[i] - DARK_RED[i]) * pulse for i in range(3))
                status, color = "BREATHE", "#ffffff"
                if self.cfg["chime"] and now - self.last_chime >= self.CHIME_EVERY_S:
                    self.play_chime()
            elif stage == "warn":
                status, color = "Breathe soon", lerp_color(AMBER, AMBER, 0)
            elif det.speaking:
                status, color = "Speaking", TEXT
            else:
                status, color = "Listening", MUTED

        c.configure(bg=lerp_color(bg, bg, 0))
        c.itemconfigure(self.t_status, text=status, fill=color)
        if self.mic_error or self.paused:
            c.itemconfigure(self.t_time, text="right-click for menu")
        else:
            left = max(0.0, limit - det.spoken)
            c.itemconfigure(self.t_time, text=f"{left:0.1f}s",
                            fill="#ffffff" if stage == "alert" else MUTED)

        warn = float(self.cfg["warn_frac"])
        used = 1.0 - remaining
        if used < warn:
            bar = lerp_color(GREEN, AMBER, (used / max(warn, 0.01)) ** 3)
        else:
            bar = lerp_color(AMBER, RED, (used - warn) / max(1.0 - warn, 0.01))
        x1 = self.bar_x0 + (self.bar_x1 - self.bar_x0) * remaining
        y0, y1 = int(38 * self.S), int(58 * self.S)
        c.coords(self.r_bar, self.bar_x0, y0, x1, y1)
        c.itemconfigure(self.r_bar, fill=MUTED if (self.paused or self.mic_error) else bar)
        track = lerp_color(TRACK, (140, 40, 40), 1.0 if stage == "alert" and not self.paused else 0.0)
        c.itemconfigure("track", fill=track)

        # mic meter: 0..40 dB above the noise floor, with a tick at the voice threshold
        span = self.bar_x1 - self.bar_x0
        floor = det.floor_db if det.floor_db is not None else -60.0
        lvl = 0.0 if (self.paused or self.mic_error) else max(0.0, min(1.0, (det.level_db - floor) / 40.0))
        c.coords(self.r_meter, self.bar_x0, self.m_y0, self.bar_x0 + span * lvl, self.m_y1)
        c.itemconfigure(self.r_meter, fill=lerp_color(GREEN, GREEN, 0) if det.speaking else MUTED)
        tx = self.bar_x0 + span * min(1.0, det.margin_db / 40.0)
        c.coords(self.r_thresh, tx, self.m_y0 - 2, tx + max(1, int(self.S)), self.m_y1 + 2)

        if self.smoke and now - self._t0 > 3.0:
            print(f"SMOKE mic={getattr(self, 'mic_name', None)!r} error={self.mic_error!r} "
                  f"level={det.level_db:.1f}dB floor={floor:.1f}dB stage={stage}")
            self.quit()
            return
        self.root.after(30, self.tick)

    # ---------- window handling ----------
    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        dx, dy = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _drag_end(self, e):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.cfg)

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.stop_stream()
            self.det.reset()
        else:
            self.start_stream()
        self.menu.entryconfigure(1, label="Resume listening" if self.paused else "Pause listening")

    def quit(self):
        self.stop_stream()
        try:
            winsound.PlaySound(None, 0)
        except Exception:
            pass
        if not self.smoke:
            save_config(self.cfg)
        self.root.destroy()

    # ---------- settings ----------
    def open_settings(self):
        tk = self.tk
        from tkinter import ttk
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = self.settings_win = tk.Toplevel(self.root)
        win.title("BreathCue settings")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=16)
        frm.pack(fill="both", expand=True)
        row = [0]

        def slider(label, key, lo, hi, fmt, step, on_done=None):
            ttk.Label(frm, text=label).grid(row=row[0], column=0, sticky="w", pady=6)
            val = ttk.Label(frm, width=8, anchor="e")
            val.grid(row=row[0], column=2, sticky="e")

            def changed(v):
                v = round(float(v) / step) * step
                self.cfg[key] = int(v) if step >= 1 and isinstance(DEFAULTS[key], int) else v
                val.configure(text=fmt(v))
                if key == "opacity":
                    self.root.attributes("-alpha", v)

            s = ttk.Scale(frm, from_=lo, to=hi, length=220, command=changed)
            s.set(self.cfg[key])
            changed(self.cfg[key])
            s.grid(row=row[0], column=1, padx=10)

            def released(_e):
                save_config(self.cfg)
                if on_done:
                    on_done()
            s.bind("<ButtonRelease-1>", released)
            row[0] += 1

        slider("Breathe after talking for", "limit_s", 2, 20, lambda v: f"{v:.1f} s", 0.5)
        slider("Early warning at", "warn_frac", 0.4, 0.95, lambda v: f"{v * 100:.0f} %", 0.05)
        slider("Mic sensitivity", "sensitivity", 1, 10, lambda v: f"{v:.0f}", 1)
        slider("Chime volume", "chime_volume", 0.05, 1.0, lambda v: f"{v * 100:.0f} %", 0.05,
               on_done=lambda: (self._make_chime(), self.play_chime()))
        slider("Window opacity", "opacity", 0.3, 1.0, lambda v: f"{v * 100:.0f} %", 0.05)

        chime = tk.BooleanVar(value=bool(self.cfg["chime"]))

        def chime_changed():
            self.cfg["chime"] = chime.get()
            save_config(self.cfg)
        ttk.Checkbutton(frm, text="Play a chime when it's time to breathe", variable=chime,
                        command=chime_changed).grid(row=row[0], column=0, columnspan=3,
                                                    sticky="w", pady=6)
        row[0] += 1

        ttk.Label(frm, text="Microphone").grid(row=row[0], column=0, sticky="w", pady=6)
        names = ["(Windows default)"] + [n for _, n in self.input_devices()]
        mic = tk.StringVar(value=self.cfg["mic"] or names[0])
        combo = ttk.Combobox(frm, textvariable=mic, values=names, state="readonly", width=38)
        combo.grid(row=row[0], column=1, columnspan=2, sticky="e", padx=(10, 0))

        def mic_changed(_e):
            self.cfg["mic"] = "" if mic.get() == names[0] else mic.get()
            save_config(self.cfg)
            if not self.paused:
                self.start_stream()
        combo.bind("<<ComboboxSelected>>", mic_changed)
        row[0] += 1

        ttk.Label(frm, foreground="#666", wraplength=int(430 * self.S), justify="left", text=(
            "The thin line under the bar is your mic level; the white tick is where "
            "sound starts counting as your voice. If it counts background noise as "
            "talking, lower the sensitivity. If it misses your voice, raise it."
        )).grid(row=row[0], column=0, columnspan=3, sticky="w", pady=(10, 0))


def main():
    app = App(smoke="--smoke" in sys.argv)
    app.root.mainloop()


if __name__ == "__main__":
    main()
