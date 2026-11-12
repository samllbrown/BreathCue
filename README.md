# BreathCue

A tiny always-on-top Windows bar that listens to your microphone and reminds you to breathe while you talk.

The bar is your "air": it drains while you speak and refills when you pause. It turns amber when it runs low ("Breathe soon") and flashes red with a chime when it runs out ("BREATHE").

## Run

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw breathcue.py
```

Windows only (uses `winsound` and Win32 calls). Requires Python 3 with tkinter.

## Use

- Drag to move.
- Right-click for Settings, Pause, Quit (or double-click for Settings).
- Settings: talk-time limit, early-warning point, mic sensitivity, chime volume, window opacity, microphone choice.

Settings are saved to `config.json` next to the script.

The thin line under the bar is your mic level; the white tick marks where sound starts counting as your voice. If background noise counts as talking, lower the sensitivity; if your voice is missed, raise it.
