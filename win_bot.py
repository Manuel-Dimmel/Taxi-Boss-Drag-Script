#!/usr/bin/env python3
"""Taxi Boss Drag Bot V2 – Windows-Starter.

Start:  python win_bot.py   (ggf. Konsole "Als Administrator ausführen",
        falls die globalen Hotkeys in Roblox nicht reagieren)
"""
from __future__ import annotations

import atexit
import ctypes
import importlib
import os
import signal
import sys
import threading
import time

PLATFORM_NAME = "Windows"

# ---------------------------------------------------------------------------
# DPI-Awareness MUSS vor mss/tkinter gesetzt werden, sonst stimmen bei
# Skalierung ≠ 100 % die Koordinaten nicht.
# ---------------------------------------------------------------------------
def _set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI aware
        return "PerMonitor(2)"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "System"
    except Exception:
        return "nicht gesetzt"


_DPI_MODE = _set_dpi_awareness() if sys.platform.startswith("win") else "n/a"

_REQUIRED = [
    ("mss", "mss", True),
    ("pydirectinput", "pydirectinput", True),
    ("keyboard", "keyboard", True),
    ("tkinter", "(tkinter ist Teil von Python – beim Installer 'tcl/tk' "
                "aktivieren)", True),
]


def check_dependencies():
    missing_req, missing_opt = [], []
    for mod, pkg, required in _REQUIRED:
        try:
            importlib.import_module(mod)
        except Exception as e:
            (missing_req if required else missing_opt).append((mod, pkg, e))
    for mod, pkg, e in missing_opt:
        print("[WARNUNG] Optionales Modul '%s' fehlt (%s). pip install %s"
              % (mod, e, pkg))
    if missing_req:
        print("\n[FEHLER] Fehlende Pflicht-Abhängigkeiten:")
        for mod, pkg, e in missing_req:
            print("   - %s  →  pip install %s   (%s)" % (mod, pkg, e))
        print("\nInstallation:  python -m venv .venv && .venv\\Scripts\\activate"
              " && pip install -r requirements.txt\n")
        sys.exit(1)


check_dependencies()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from core_utils import BotEngine, ConfigManager, InputBackend, ScreenReader
except ImportError as _e:
    print("[FEHLER] core_utils.py konnte nicht importiert werden: %s" % _e)
    print("         Fehlt ein Paket? → pip install -r requirements.txt")
    sys.exit(1)

from core_utils import create_gui  # GUI lebt in core_utils.py (3-Dateien-System)

import keyboard  # noqa: E402
import pydirectinput  # noqa: E402

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False


def _log_fallback(msg):
    print(msg, flush=True)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# ---------------------------------------------------------------------------
# Input-Backend
# ---------------------------------------------------------------------------
class WinInput(InputBackend):
    """Tastatur/Maus über pydirectinput (DirectInput-Scancodes → Roblox)."""

    MOVE_PAUSE = 0.03
    CLICK_HOLD = 0.05

    def __init__(self, log=None):
        try:
            super(WinInput, self).__init__()
        except Exception:
            pass
        self.log = log or _log_fallback
        self._lock = threading.RLock()
        self._pressed = set()

    @staticmethod
    def _norm(key):
        k = str(key).lower().strip()
        return {"escape": "esc", "return": "enter"}.get(k, k)

    def press(self, key):
        key = self._norm(key)
        with self._lock:
            pydirectinput.keyDown(key)
            self._pressed.add(key)

    def release(self, key):
        key = self._norm(key)
        with self._lock:
            try:
                pydirectinput.keyUp(key)
            finally:
                self._pressed.discard(key)

    def tap(self, key, duration):
        self.press(key)
        try:
            time.sleep(max(0.0, float(duration)))
        finally:
            self.release(key)

    def release_all(self):
        with self._lock:
            for k in set(self._pressed) | {"w", "e"}:
                try:
                    pydirectinput.keyUp(k)
                except Exception as e:
                    print("release_all(%s) Fehler: %s" % (k, e))
            self._pressed.clear()

    def click(self, x, y):
        x, y = int(x), int(y)
        pydirectinput.moveTo(x + 1, y)
        time.sleep(self.MOVE_PAUSE)
        pydirectinput.moveTo(x, y)
        time.sleep(self.MOVE_PAUSE)
        pydirectinput.mouseDown(x, y, button="left")
        time.sleep(self.CLICK_HOLD)
        pydirectinput.mouseUp(x, y, button="left")

    def get_mouse_position(self):
        pt = _POINT()
        try:
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
        try:
            pos = pydirectinput.position()
            return int(pos[0]), int(pos[1])
        except Exception:
            return 0, 0


# ---------------------------------------------------------------------------
# Globale Hotkeys (keyboard-Lib)
# ---------------------------------------------------------------------------
class WinHotkeys(object):
    DEBOUNCE = 0.3

    def __init__(self, bindings, log=None):
        self.bindings = {str(k).lower(): v for k, v in bindings.items()}
        self.log = log or _log_fallback
        self._last = {}
        self._handles = []
        self.mode = None

    def _make(self, name, cb):
        def handler():
            now = time.monotonic()
            if now - self._last.get(name, 0.0) < self.DEBOUNCE:
                return
            self._last[name] = now
            try:
                cb()
            except Exception as e:
                self.log("Hotkey-Fehler (%s): %s" % (name, e))
        return handler

    def start(self):
        try:
            for name, cb in self.bindings.items():
                self._handles.append(
                    keyboard.add_hotkey(name, self._make(name, cb),
                                        suppress=False, trigger_on_release=False))
            self.mode = "keyboard"
            return True
        except Exception as e:
            self.log("Globale Hotkeys fehlgeschlagen: %s" % e)
            return False

    def stop(self):
        for h in self._handles:
            try:
                keyboard.remove_hotkey(h)
            except Exception:
                pass
        self._handles = []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    gui_holder = {"gui": None}

    def log(msg):
        gui = gui_holder["gui"]
        print(msg, flush=True)
        if gui is not None:
            gui.log(msg)

    config = ConfigManager()
    try:
        config.load()
    except Exception as e:
        log("Config-Ladefehler: %s" % e)

    inp = WinInput(log=log)
    screen = ScreenReader()
    engine = BotEngine(config, inp, screen=screen, log=log)

    _done = {"v": False}

    def safety_exit():
        if _done["v"]:
            return
        _done["v"] = True
        for fn in (engine.safety_release, engine.emergency_stop, inp.release_all):
            try:
                fn()
            except Exception:
                pass

    atexit.register(safety_exit)

    hotkeys_holder = {"hk": None}

    def on_close():
        hk = hotkeys_holder["hk"]
        if hk is not None:
            hk.stop()
        safety_exit()

    gui = create_gui(engine, config, PLATFORM_NAME, on_close=on_close)
    gui_holder["gui"] = gui

    def on_signal(signum, _frame):
        print("Signal %s empfangen – stoppe Bot." % signum, flush=True)
        safety_exit()
        gui.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except Exception:
            pass

    log("Taxi Boss Drag Bot V2 – %s gestartet (Python %s)."
        % (PLATFORM_NAME, sys.version.split()[0]))
    log("DPI-Awareness: %s" % _DPI_MODE)
    try:
        mon = screen.monitor()
        log("Monitor: %sx%s, Faktor %.2f"
            % (mon.get("width"), mon.get("height"), screen.scale()))
    except Exception as e:
        log("⚠ Bildschirm konnte nicht gelesen werden: %s" % e)

    hk_cfg = config.get("hotkeys", default=None) or {}
    k_cal = str(hk_cfg.get("calibrate", "f8")).lower()
    k_tog = str(hk_cfg.get("toggle", "f9")).lower()
    k_emg = str(hk_cfg.get("emergency", "f10")).lower()
    hotkeys = WinHotkeys({k_cal: gui.calibrate_selected,
                          k_tog: gui.toggle_pause,
                          k_emg: gui.emergency}, log=log)
    hotkeys_holder["hk"] = hotkeys
    if hotkeys.start():
        log("Globale Hotkeys aktiv: %s=Kalibrieren, %s=Start/Pause, %s=NOT-AUS"
            % (k_cal.upper(), k_tog.upper(), k_emg.upper()))
    else:
        gui.bind_local_hotkeys(k_cal.upper(), k_tog.upper(), k_emg.upper())
        log("⚠ Keine globalen Hotkeys – nur bei fokussiertem Bot-Fenster. "
            "Konsole ggf. als Administrator starten.")

    engine.start_thread()
    log("Engine läuft (PAUSED). Roblox fokussieren und F9 drücken.")

    try:
        gui.run()
    finally:
        on_close()
        safety_exit()


if __name__ == "__main__":
    main()
