#!/usr/bin/env python3
"""
Taxi Boss (Roblox) – Performanter Auto-Shifter für Windows (10 / 11)
====================================================================

Optimierte Windows-Version (inkl. Turbo-Modus für Gang 1 & 2):
--------------------------------------------------------------
- Automatische Windows-DPI-Awareness (100 %, 125 %, 150 %, 200 % Skalierung),
  damit Maus-Koordinaten (`pynput`) und Bildschirm-Pixel (`mss`) 1:1 übereinstimmen.
- 1-ms-Systemtimer (`winmm.timeBeginPeriod(1)`), damit Windows bei kurzen Pausen
  nicht auf das 15,6-ms-Standardraster rundet.
- Direkter Hardware-ScanCode-Tastendruck (`SendInput` / ScanCode 0x12 für 'E'),
  damit Roblox unter Windows (DirectInput / RawInput) das Schalten verzögerungsfrei erkennt.
- Intelligenter Cooldown (85–220 ms mit Graustufen-Reset) und früher Grün-Schwellenwert
  speziell für die schnellen ersten beiden Gänge.

Hotkeys:
--------
- [i] Aktuelle Mausposition als Schalt-Punkt speichern + RGB-Wert im Terminal ausgeben
      (WICHTIG: Der Kreis darf dabei noch GRAU sein! Es wird nur die Position gespeichert.)
- [o] Auto-Shifter starten / pausieren
- [p] Skript sauber beenden
"""


from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Any, Tuple

# ============================================================================
# WINDOWS DPI-AWARENESS & 1-MS-TIMER (VOR DEM IMPORT VON PYNPUT / MSS!)
# ============================================================================
IS_WINDOWS: bool = sys.platform == "win32"

if IS_WINDOWS:
    # 1. Per-Monitor DPI-Awareness aktivieren, damit pynput und mss identische
    #    physische Pixel-Koordinaten verwenden (auch bei 125 % / 150 % Skalierung).
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # 2. Windows-Timer-Auflösung von 15.6 ms auf 1.0 ms setzen (extrem wichtig für Gang 1 & 2!)
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

import mss
from pynput import keyboard, mouse

try:
    import msvcrt  # Windows-Konsole Tastatureingabe
except ImportError:
    msvcrt = None  # type: ignore[assignment]


# ============================================================================
# KONFIGURATION & SCHWELLENWERTE (OPTIMIERT FÜR GANG 1 & 2)
# ============================================================================

# Relative Standard-Position aus der Tacho-Analyse (Schalt-Indikator-Kreis)
DEFAULT_REL_X: float = 0.5000  # Exakt mittig
DEFAULT_REL_Y: float = 0.7787  # Schalt-Kreis im Tacho über der Ganganzeige

# Überwachungsbereich rund um die Zielkoordinate (3x3 Pixel)
SAMPLE_BOX_SIZE: int = 3

# Schwellenwert-Logik für Grün-Erkennung:
# Sensibel eingestellt (G > 95), damit bereits der allererste Übergangs-Frame
# von Grau RGB(50,50,50) zu Grün im 1. und 2. Gang sofort auslöst!
GREEN_MIN_VALUE: int = 95
GREEN_DOMINANCE_RATIO: float = 1.22  # G > R * 1.22 und G > B * 1.22

# Timing-Parameter (Ultra-Low-Latency für schnelle erste Gänge):
KEY_HOLD_DURATION: float = 0.012  # 12 ms Tastendruck
MIN_SHIFT_COOLDOWN: float = 0.085 # 85 ms Mindest-Pause, damit der 2. Gang nicht verschlafen wird
MAX_SHIFT_COOLDOWN: float = 0.22  # Max. Wartezeit, falls der Kreis länger grün bleibt
SCAN_INTERVAL: float = 0.0        # Maximale Abtastrate ohne künstliches Sleep-Delay

# Windows Virtual-Key-Codes für i, o, p sowie ScanCode für 'E'
WIN_VK_MAP: dict[int, str] = {
    0x49: "i",  # VK_I (73)
    0x4F: "o",  # VK_O (79)
    0x50: "p",  # VK_P (80)
}
WIN_VK_E: int = 0x45        # Virtual Key 'E'
WIN_SCANCODE_E: int = 0x12  # Hardware-ScanCode für 'E' (wichtig für Roblox unter Windows)
KEYEVENTF_SCANCODE: int = 0x0008
KEYEVENTF_KEYUP: int = 0x0002


# ============================================================================
# WINDOWS SENDINPUT STRUKTUREN FÜR ROBLOX (DIRECTINPUT / SCANCODE)
# ============================================================================
if IS_WINDOWS:
    ULONG_PTR = ctypes.WINFUNCTYPE(ctypes.c_void_p) if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_short),
            ("wParamH", ctypes.c_ushort),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("ki", KEYBDINPUT),
            ("mi", MOUSEINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ulong),
            ("union", _INPUT_UNION),
        ]


def create_mss_instance() -> Any:
    """Erstellt eine MSS-Instanz kompatibel mit mss 9.x und mss 10.x+ (ohne DeprecationWarning)."""
    mss_cls = getattr(mss, "MSS", None) or getattr(mss, "mss")
    return mss_cls()


class AutoShifterWindows:
    """Performanter Windows Auto-Shifter für Taxi Boss (Roblox)."""

    def __init__(self) -> None:
        self.mouse_ctrl = mouse.Controller()
        self.kb_ctrl = keyboard.Controller()

        self._lock = threading.Lock()
        self.active: bool = False
        self.running: bool = True
        self.shift_count: int = 0
        self._last_cmd_time: dict[str, float] = {"i": 0.0, "o": 0.0, "p": 0.0}

        # Hauptmonitor-Auflösung über mss ermitteln
        with create_mss_instance() as sct:
            primary_mon = sct.monitors[1]
            self.screen_left: int = int(primary_mon["left"])
            self.screen_top: int = int(primary_mon["top"])
            self.screen_width: int = int(primary_mon["width"])
            self.screen_height: int = int(primary_mon["height"])

        # Vorausgewählte Koordinate anhand der relativen Tacho-Position setzen
        self.target_x: int = self.screen_left + int(round(self.screen_width * DEFAULT_REL_X))
        self.target_y: int = self.screen_top + int(round(self.screen_height * DEFAULT_REL_Y))
        self._cached_bbox: dict[str, int] = self._build_capture_bbox(self.target_x, self.target_y)

    @staticmethod
    def is_green(r: int, g: int, b: int) -> bool:
        """
        Prüft anhand der Schwellenwert-Logik, ob ein RGB-Wert als 'Grün'
        (optimaler Schaltzeitpunkt) gilt:
        G > 95 sowie G > R * 1.22 und G > B * 1.22
        """
        return (
            g > GREEN_MIN_VALUE
            and g > (r * GREEN_DOMINANCE_RATIO)
            and g > (b * GREEN_DOMINANCE_RATIO)
        )

    @staticmethod
    def _build_capture_bbox(x: int, y: int) -> dict[str, int]:
        """Erstellt eine 3x3-Bounding-Box rund um den Zielpunkt (x, y)."""
        half = SAMPLE_BOX_SIZE // 2
        return {
            "left": int(x - half),
            "top": int(y - half),
            "width": SAMPLE_BOX_SIZE,
            "height": SAMPLE_BOX_SIZE,
        }

    def sample_region_fast(self, sct: Any, bbox: dict[str, int]) -> Tuple[Tuple[int, int, int], bool]:
        """
        Ultraschnelle Auswertung des 3x3-Bereichs direkt auf dem BGRA-Speicherpuffer.
        """
        sct_img = sct.grab(bbox)
        raw: bytes = sct_img.raw

        for offset in range(0, len(raw), 4):
            g = raw[offset + 1]
            if g > GREEN_MIN_VALUE:
                b = raw[offset]
                r = raw[offset + 2]
                if g > (r * GREEN_DOMINANCE_RATIO) and g > (b * GREEN_DOMINANCE_RATIO):
                    return (r, g, b), True

        width: int = sct_img.width
        height: int = sct_img.height
        center_offset = ((height // 2) * width + (width // 2)) * 4
        return (raw[center_offset + 2], raw[center_offset + 1], raw[center_offset]), False

    def trigger_shift(self) -> None:
        """
        Sendet die Taste 'e' unter Windows direkt als Hardware-ScanCode (0x12) via SendInput,
        damit Roblox (DirectInput) den Tastendruck sofort und zuverlässig registriert.
        """
        if IS_WINDOWS:
            try:
                down = INPUT(
                    type=1,
                    union=_INPUT_UNION(
                        ki=KEYBDINPUT(
                            wVk=WIN_VK_E,
                            wScan=WIN_SCANCODE_E,
                            dwFlags=KEYEVENTF_SCANCODE,
                            time=0,
                            dwExtraInfo=0,
                        )
                    ),
                )
                up = INPUT(
                    type=1,
                    union=_INPUT_UNION(
                        ki=KEYBDINPUT(
                            wVk=WIN_VK_E,
                            wScan=WIN_SCANCODE_E,
                            dwFlags=KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP,
                            time=0,
                            dwExtraInfo=0,
                        )
                    ),
                )
                ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
                time.sleep(KEY_HOLD_DURATION)
                ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
                return
            except Exception:
                pass

        # Fallback über pynput
        self.kb_ctrl.press("e")
        time.sleep(KEY_HOLD_DURATION)
        self.kb_ctrl.release("e")

    def calibrate_at_mouse(self) -> None:
        """
        [i] Speichert die aktuelle Mausposition als Schalt-Punkt und gibt die
        Pixel-Koordinate sowie den gelesenen RGB-Wert im Terminal aus.
        """
        mx, my = self.mouse_ctrl.position
        px = int(round(mx))
        py = int(round(my))
        new_bbox = self._build_capture_bbox(px, py)

        with self._lock:
            self.target_x = px
            self.target_y = py
            self._cached_bbox = new_bbox

        with create_mss_instance() as sct:
            rgb, detected_green = self.sample_region_fast(sct, new_bbox)

        r, g, b = rgb
        hex_color = f"#{r:02X}{g:02X}{b:02X}"
        green_status = (
            "GRÜN ERKANNT (Schaltpunkt!)"
            if detected_green
            else "Grau / Nicht grün (Position gespeichert – bereit für Grün!)"
        )

        print(
            f"\n[KALIBRIERUNG - i] Aktuelle Mausposition als Schalt-Punkt gespeichert:\n"
            f"  -> Bildschirm-Koordinate (Pixel):      ({px}, {py})\n"
            f"  -> Gelesener RGB-Wert (3x3-Zentrum):   RGB({r}, {g}, {b}) | {hex_color} | {green_status}",
            flush=True,
        )

    def toggle_active(self) -> None:
        """[o] Startet oder pausiert den Auto-Shifter."""
        with self._lock:
            self.active = not self.active
            state = self.active
            tx, ty = self.target_x, self.target_y

        if state:
            print(
                f"\n[STATUS - o] Auto-Shifter AKTIVIERT – Überwache Pixel ({tx}, {ty}) ...",
                flush=True,
            )
        else:
            print("\n[STATUS - o] Auto-Shifter PAUSIERT.", flush=True)

    def stop(self) -> None:
        """[p] Beendet das Skript sauber."""
        with self._lock:
            if not self.running:
                return
            self.running = False
            self.active = False
        print("\n[BEENDEN - p] Auto-Shifter wird sauber beendet ...", flush=True)

    def handle_command(self, cmd: str) -> bool:
        """Verarbeitet einen Hotkey-Befehl ('i', 'o', 'p') mit 150 ms Entprellung."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_cmd_time.get(cmd, 0.0) < 0.15:
                return not self.running
            self._last_cmd_time[cmd] = now

        if cmd == "i":
            self.calibrate_at_mouse()
        elif cmd == "o":
            self.toggle_active()
        elif cmd == "p":
            self.stop()
            return True
        return False

    def monitor_loop(self) -> None:
        """
        Hochgeschwindigkeits-Hauptschleife für Windows:
        1. Schaltet SOFORT (via ScanCode SendInput), sobald Grün erkannt wird.
        2. Nutzt dynamische Entprellung (85–220 ms), damit der schnelle 2. Gang
           sofort nach dem Erlöschen des grünen Kreises wieder überwacht wird.
        """
        with create_mss_instance() as sct:
            while self.running:
                if not self.active:
                    time.sleep(0.025)
                    continue

                bbox = self._cached_bbox
                rgb, is_shift_ready = self.sample_region_fast(sct, bbox)

                if is_shift_ready:
                    shift_start = time.perf_counter()
                    self.trigger_shift()
                    self.shift_count += 1

                    r, g, b = rgb
                    tx, ty = self.target_x, self.target_y
                    timestamp = time.strftime("%H:%M:%S")
                    print(
                        f"[{timestamp}] SCHALTEN #{self.shift_count:02d} -> Taste 'e' gedrückt! "
                        f"(RGB({r}, {g}, {b}) bei ({tx}, {ty}))",
                        flush=True,
                    )

                    elapsed = time.perf_counter() - shift_start
                    if elapsed < MIN_SHIFT_COOLDOWN:
                        time.sleep(MIN_SHIFT_COOLDOWN - elapsed)

                    while self.running and self.active:
                        if (time.perf_counter() - shift_start) >= MAX_SHIFT_COOLDOWN:
                            break
                        _, still_green = self.sample_region_fast(sct, self._cached_bbox)
                        if not still_green:
                            break
                elif SCAN_INTERVAL > 0.0:
                    time.sleep(SCAN_INTERVAL)

    def console_fallback_loop(self) -> None:
        """Optionaler Konsolen-Fallback unter Windows (msvcrt)."""
        if msvcrt is None:
            return
        try:
            while self.running:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch().lower()
                    if ch in ("i", "o", "p"):
                        if self.handle_command(ch):
                            break
                time.sleep(0.05)
        except Exception:
            pass

    def print_banner(self) -> None:
        """Gibt beim Start alle System- und Hotkey-Informationen im Terminal aus."""
        with create_mss_instance() as sct:
            init_rgb, _ = self.sample_region_fast(sct, self._cached_bbox)

        print("=" * 76)
        print("  TAXI BOSS (ROBLOX) – AUTO-SHIFTER FÜR WINDOWS (TURBO-MODUS GANG 1 & 2)")
        print("=" * 76)
        print(f"  Bildschirm-Auflösung:  {self.screen_width} x {self.screen_height} Pixel (DPI-Aware)")
        print(f"  Vorab-Schaltpunkt:     ({self.target_x}, {self.target_y}) [X=50.0%, Y=77.9%]")
        print(f"  Aktuelles RGB dort:    RGB{init_rgb} (3x3-Überwachungsbereich)")
        print(
            f"  Grün-Schwellenwert:    G > {GREEN_MIN_VALUE}, "
            f"G > R * {GREEN_DOMINANCE_RATIO}, G > B * {GREEN_DOMINANCE_RATIO}"
        )
        print(
            f"  Dynamischer Cooldown:  {MIN_SHIFT_COOLDOWN*1000:.0f}–{MAX_SHIFT_COOLDOWN*1000:.0f} ms "
            f"(sofort bereit, sobald Kreis wieder grau)"
        )
        print("-" * 76)
        print("  Steuerung per Hotkeys:")
        print("    [i]   Aktuelle Mausposition als Schalt-Punkt speichern + RGB anzeigen")
        print("          (Der Kreis darf beim Drücken von [i] GRAU sein – es wird nur der Ort gespeichert!)")
        print("    [o]   Auto-Shifter starten / pausieren")
        print("    [p]   Skript sauber beenden")
        print("-" * 76)
        print("  Tipp für Windows: Starte das Terminal / PyCharm ggf. als Administrator,")
        print("  falls Roblox im Vollbildmodus Tastendrücke von normalen Prozessen blockiert.")
        print("=" * 76, flush=True)

    def run(self) -> None:
        """Startet den Überwachungs-Thread und den globalen Hotkey-Listener."""
        self.print_banner()

        worker = threading.Thread(target=self.monitor_loop, name="ShiftMonitorThread", daemon=True)
        worker.start()

        if msvcrt is not None:
            console_worker = threading.Thread(
                target=self.console_fallback_loop, name="ConsoleHotkeyThread", daemon=True
            )
            console_worker.start()

        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> bool | None:
            try:
                cmd: str | None = None
                char = getattr(key, "char", None)
                if isinstance(char, str) and char.lower() in ("i", "o", "p"):
                    cmd = char.lower()
                else:
                    vk = getattr(key, "vk", None)
                    if isinstance(vk, int) and vk in WIN_VK_MAP:
                        cmd = WIN_VK_MAP[vk]

                if cmd is not None:
                    if self.handle_command(cmd):
                        return False
            except Exception as exc:
                print(f"[FEHLER] Hotkey-Verarbeitung: {exc}", file=sys.stderr, flush=True)
            return None

        try:
            with keyboard.Listener(on_press=on_press) as listener:
                while self.running and listener.is_alive():
                    time.sleep(0.1)
                listener.stop()
        except KeyboardInterrupt:
            self.stop()
        finally:
            if IS_WINDOWS:
                try:
                    ctypes.windll.winmm.timeEndPeriod(1)
                except Exception:
                    pass

        worker.join(timeout=1.0)
        print("Skript sauber beendet.", flush=True)


if __name__ == "__main__":
    shifter = AutoShifterWindows()
    shifter.run()
