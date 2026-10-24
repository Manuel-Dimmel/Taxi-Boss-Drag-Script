#!/usr/bin/env python3
"""Taxi Boss Drag Bot V2 – macOS-Starter.

Start:  python3 mac_bot.py

Benötigte macOS-Berechtigungen (Systemeinstellungen → Datenschutz & Sicherheit)
für die App, aus der gestartet wird (Terminal / iTerm / PyCharm):
  * Bildschirmaufnahme      – Pixel lesen (mss)
  * Bedienungshilfen        – Tastatur/Maus an Roblox senden
  * Eingabeüberwachung      – globale Hotkeys F8/F9/F10
Nach dem Erteilen die App komplett neu starten!
"""
from __future__ import annotations

import atexit
import importlib
import os
import signal
import sys
import threading
import time

PLATFORM_NAME = "macOS"

# ---------------------------------------------------------------------------
# Abhängigkeiten prüfen (klare Fehlermeldungen statt Tracebacks)
# ---------------------------------------------------------------------------
_REQUIRED = [
    # (Import-Name, pip-Paket, Pflicht?)
    ("mss", "mss", True),
    ("pynput", "pynput", True),
    ("Quartz", "pyobjc-framework-Quartz", False),
    ("tkinter", "(tkinter ist Teil von Python – ggf. python.org-Installer "
                "oder 'brew install python-tk' nutzen)", True),
]


def check_dependencies():
    missing_req, missing_opt = [], []
    for mod, pkg, required in _REQUIRED:
        try:
            importlib.import_module(mod)
        except Exception as e:  # ImportError, aber auch kaputte Installationen
            (missing_req if required else missing_opt).append((mod, pkg, e))
    for mod, pkg, e in missing_opt:
        print("[WARNUNG] Optionales Modul '%s' fehlt (%s). pip install %s"
              % (mod, e, pkg))
    if missing_req:
        print("\n[FEHLER] Fehlende Pflicht-Abhängigkeiten:")
        for mod, pkg, e in missing_req:
            print("   - %s  →  pip install %s   (%s)" % (mod, pkg, e))
        print("\nInstallation:  python3 -m venv .venv && source .venv/bin/activate"
              " && pip install -r requirements.txt\n")
        sys.exit(1)


check_dependencies()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import core_utils
    from core_utils import BotEngine, ConfigManager, InputBackend, ScreenReader
except ImportError as _e:
    print("[FEHLER] core_utils.py konnte nicht importiert werden: %s" % _e)
    print("         Fehlt ein Paket? → pip install -r requirements.txt")
    sys.exit(1)

from core_utils import create_gui  # GUI lebt in core_utils.py (3-Dateien-System)

# Quartz (pyobjc) bevorzugt, pynput als Fallback
try:
    import Quartz  # type: ignore
    HAVE_QUARTZ = True
except Exception:
    Quartz = None
    HAVE_QUARTZ = False

from pynput import keyboard as pk_keyboard
from pynput import mouse as pk_mouse

# macOS virtuelle Keycodes (ANSI-Layout; physische Tastenposition)
MAC_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38,
    "k": 40, "n": 45, "m": 46,
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51,
    "esc": 53, "escape": 53, "shift": 56, "ctrl": 59, "alt": 58, "cmd": 55,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


def _log_fallback(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Input-Backend
# ---------------------------------------------------------------------------
class MacInput(InputBackend):
    """Tastatur/Maus für macOS: Quartz-CGEvents, Fallback pynput."""

    MOVE_PAUSE = 0.03
    CLICK_HOLD = 0.05

    def __init__(self, log=None, use_quartz=True):
        try:
            super(MacInput, self).__init__()
        except Exception:
            pass
        self.log = log or _log_fallback
        self.use_quartz = bool(use_quartz and HAVE_QUARTZ)
        self._lock = threading.RLock()
        self._pressed = set()
        self._kb = pk_keyboard.Controller()
        self._mouse = pk_mouse.Controller()

    # ---- Tastatur ----
    @staticmethod
    def _norm(key):
        return str(key).lower().strip()

    def _quartz_key(self, key, down):
        code = MAC_KEYCODES.get(key)
        if code is None:
            return False
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, bool(down))
        if ev is None:
            return False
        Quartz.CGEventSetFlags(ev, 0)  # keine geerbten Modifier (Shift etc.)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True

    def _pynput_key(self, key):
        if len(key) == 1:
            return key
        alias = {"enter": "enter", "return": "enter", "escape": "esc",
                 "ctrl": "ctrl", "alt": "alt", "cmd": "cmd"}
        return getattr(pk_keyboard.Key, alias.get(key, key))

    def _send_key(self, key, down):
        ok = False
        if self.use_quartz:
            try:
                ok = self._quartz_key(key, down)
            except Exception as e:
                self.log("Quartz-Key-Fehler (%s) – nutze pynput: %s" % (key, e))
                ok = False
        if not ok:
            k = self._pynput_key(key)
            if down:
                self._kb.press(k)
            else:
                self._kb.release(k)

    def press(self, key):
        key = self._norm(key)
        with self._lock:
            self._send_key(key, True)
            self._pressed.add(key)

    def release(self, key):
        key = self._norm(key)
        with self._lock:
            try:
                self._send_key(key, False)
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
            keys = set(self._pressed) | {"w", "e"}
            for k in keys:
                try:
                    self._send_key(k, False)
                except Exception as e:
                    print("release_all(%s) Fehler: %s" % (k, e))
            self._pressed.clear()

    # ---- Maus ----
    def _quartz_mouse(self, etype, x, y):
        ev = Quartz.CGEventCreateMouseEvent(
            None, etype, (float(x), float(y)), Quartz.kCGMouseButtonLeft)
        if etype in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            Quartz.CGEventSetIntegerValueField(
                ev, Quartz.kCGMouseEventClickState, 1)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def click(self, x, y):
        x, y = int(x), int(y)
        if self.use_quartz:
            try:
                self._quartz_mouse(Quartz.kCGEventMouseMoved, x + 1, y)
                time.sleep(self.MOVE_PAUSE)
                self._quartz_mouse(Quartz.kCGEventMouseMoved, x, y)
                time.sleep(self.MOVE_PAUSE)
                self._quartz_mouse(Quartz.kCGEventLeftMouseDown, x, y)
                time.sleep(self.CLICK_HOLD)
                self._quartz_mouse(Quartz.kCGEventLeftMouseUp, x, y)
                return
            except Exception as e:
                self.log("Quartz-Klick-Fehler – nutze pynput: %s" % e)
        m = self._mouse
        m.position = (x + 1, y)
        time.sleep(self.MOVE_PAUSE)
        m.position = (x, y)
        time.sleep(self.MOVE_PAUSE)
        m.press(pk_mouse.Button.left)
        time.sleep(self.CLICK_HOLD)
        m.release(pk_mouse.Button.left)

    def get_mouse_position(self):
        if self.use_quartz:
            try:
                loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
                return int(round(loc.x)), int(round(loc.y))
            except Exception:
                pass
        x, y = self._mouse.position
        return int(round(x)), int(round(y))


# ---------------------------------------------------------------------------
# Globale Hotkeys
# ---------------------------------------------------------------------------
class MacHotkeys(object):
    """Globale Hotkeys. Bevorzugt Quartz-EventTap (listen-only, vergleicht nur
    Keycodes → keine TSM-Aufrufe aus Nebenthreads, die auf neueren macOS-
    Versionen mit pynput abstürzen können). Fallback: pynput.Listener."""

    DEBOUNCE = 0.3

    def __init__(self, bindings, log=None):
        # bindings: {"f8": callable, ...}
        self.bindings = {str(k).lower(): v for k, v in bindings.items()}
        self.log = log or _log_fallback
        self._last = {}
        self._thread = None
        self._runloop = None
        self._tap = None
        self._listener = None
        self.mode = None

    def _fire(self, name):
        cb = self.bindings.get(name)
        if cb is None:
            return
        now = time.monotonic()
        if now - self._last.get(name, 0.0) < self.DEBOUNCE:
            return
        self._last[name] = now
        try:
            cb()
        except Exception as e:
            self.log("Hotkey-Fehler (%s): %s" % (name, e))

    # ---- Quartz ----
    def _start_quartz(self):
        code_to_name = {}
        for name in self.bindings:
            code = MAC_KEYCODES.get(name)
            if code is not None:
                code_to_name[code] = name
        ready = threading.Event()
        result = {"ok": False}

        def callback(proxy, etype, event, refcon):
            try:
                if etype in (Quartz.kCGEventTapDisabledByTimeout,
                             Quartz.kCGEventTapDisabledByUserInput):
                    if self._tap is not None:
                        Quartz.CGEventTapEnable(self._tap, True)
                    return event
                if etype == Quartz.kCGEventKeyDown:
                    if Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGKeyboardEventAutorepeat):
                        return event
                    code = Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode)
                    name = code_to_name.get(int(code))
                    if name:
                        self._fire(name)
            except Exception as e:
                print("Hotkey-Callback-Fehler: %s" % e)
            return event

        def run():
            try:
                mask = 1 << Quartz.kCGEventKeyDown
                tap = Quartz.CGEventTapCreate(
                    Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                    Quartz.kCGEventTapOptionListenOnly, mask, callback, None)
                if tap is None:
                    ready.set()
                    return
                self._tap = tap
                src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
                self._runloop = Quartz.CFRunLoopGetCurrent()
                Quartz.CFRunLoopAddSource(self._runloop, src,
                                          Quartz.kCFRunLoopCommonModes)
                Quartz.CGEventTapEnable(tap, True)
                result["ok"] = True
                ready.set()
                Quartz.CFRunLoopRun()
            except Exception as e:
                print("Quartz-EventTap-Fehler: %s" % e)
                ready.set()

        self._thread = threading.Thread(target=run, name="HotkeyTap",
                                        daemon=True)
        self._thread.start()
        ready.wait(2.0)
        return result["ok"]

    # ---- pynput ----
    def _start_pynput(self):
        key_map = {}
        for name in self.bindings:
            k = getattr(pk_keyboard.Key, name, None)
            if k is not None:
                key_map[k] = name

        def on_press(key):
            name = key_map.get(key)
            if name:
                self._fire(name)

        self._listener = pk_keyboard.Listener(on_press=on_press)
        self._listener.daemon = True
        self._listener.start()
        return True

    def start(self):
        if HAVE_QUARTZ:
            try:
                if self._start_quartz():
                    self.mode = "quartz"
                    return True
                self.log("Quartz-EventTap nicht erlaubt (Eingabeüberwachung/"
                         "Bedienungshilfen fehlen?) – versuche pynput.")
            except Exception as e:
                self.log("Quartz-Hotkeys fehlgeschlagen: %s" % e)
        try:
            self._start_pynput()
            self.mode = "pynput"
            return True
        except Exception as e:
            self.log("pynput-Hotkeys fehlgeschlagen: %s" % e)
            self.mode = None
            return False

    def stop(self):
        try:
            if self._runloop is not None:
                Quartz.CFRunLoopStop(self._runloop)
        except Exception:
            pass
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _check_accessibility(log):
    """Hinweis, falls Bedienungshilfen-Recht fehlt (nur Info)."""
    try:
        import ApplicationServices  # type: ignore  (pyobjc)
        if not ApplicationServices.AXIsProcessTrusted():
            log("⚠ Bedienungshilfen-Recht fehlt! Systemeinstellungen → "
                "Datenschutz & Sicherheit → Bedienungshilfen → Terminal/PyCharm "
                "aktivieren und App neu starten. Sonst kommen Tasten/Klicks "
                "nicht in Roblox an.")
        else:
            log("Bedienungshilfen-Recht: OK")
    except Exception:
        pass


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

    inp = MacInput(log=log)
    screen = ScreenReader()
    engine = BotEngine(config, inp, screen=screen, log=log)

    # --- Safety: bei jedem Programmende Tasten loslassen
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
    log("Input: %s" % ("Quartz CGEvents" if inp.use_quartz else "pynput"))
    try:
        mon = screen.monitor()
        log("Monitor: %sx%s (logisch), Retina-Faktor %.2f"
            % (mon.get("width"), mon.get("height"), screen.scale()))
    except Exception as e:
        log("⚠ Bildschirm konnte nicht gelesen werden (%s). "
            "Bildschirmaufnahme-Recht erteilen!" % e)
    _check_accessibility(log)

    hk_cfg = config.get("hotkeys", default=None) or {}
    k_cal = str(hk_cfg.get("calibrate", "f8")).lower()
    k_tog = str(hk_cfg.get("toggle", "f9")).lower()
    k_emg = str(hk_cfg.get("emergency", "f10")).lower()
    hotkeys = MacHotkeys({k_cal: gui.calibrate_selected,
                          k_tog: gui.toggle_pause,
                          k_emg: gui.emergency}, log=log)
    hotkeys_holder["hk"] = hotkeys
    if hotkeys.start():
        log("Globale Hotkeys aktiv (%s): %s=Kalibrieren, %s=Start/Pause, "
            "%s=NOT-AUS" % (hotkeys.mode, k_cal.upper(), k_tog.upper(),
                             k_emg.upper()))
        log("Hinweis: Falls F-Tasten nicht reagieren → 'fn' gedrückt halten "
            "oder in Tastatur-Einstellungen F-Tasten als Standardtasten "
            "verwenden. Eingabeüberwachung für Terminal/PyCharm erlauben.")
    else:
        gui.bind_local_hotkeys(k_cal.upper(), k_tog.upper(), k_emg.upper())
        log("⚠ Keine globalen Hotkeys – F8/F9/F10 funktionieren nur, wenn das "
            "Bot-Fenster den Fokus hat. Eingabeüberwachung erlauben!")

    engine.start_thread()
    log("Engine läuft (PAUSED). Roblox fokussieren und F9 drücken.")

    try:
        gui.run()
    finally:
        on_close()
        safety_exit()


if __name__ == "__main__":
    main()
