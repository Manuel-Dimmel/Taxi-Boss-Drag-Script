#!/usr/bin/env python3
"""
Taxi Boss (Roblox) – Performanter Auto-Shifter für macOS (Retina-kompatibel)
============================================================================

Vorab-Analyse des Referenzbilds ("Bildschirmfoto 2026-10-23 um 09.32.46.png"):
------------------------------------------------------------------------------
- Physische Screenshot-Auflösung (Retina 2x): 3420 x 2214 Pixel
- Logische macOS-Bildschirmauflösung (Points): 1710 x 1107 Punkte (Skalierungsfaktor 2.0x)

Gefundene graue Kreise im Tacho (horizontal exakt mittig bei X = 50.00 %):
1. Schalt-Indikator-Kreis (Shift-Light im Tacho, mittig über Gang "1"):
   - Relativ:              X = 50.00 %, Y = 77.87 %
   - Logische Koordinate:  (855, 862)   [macOS Points / pynput]
   - Physische Koordinate: (1710, 1724) [Retina Pixel im Screenshot]
   - Ruhefarbe (Grau):     RGB(50, 50, 50) / #323232

2. Tacho-Zentrum / Nadel-Kreis (direkt über "0 km/h"):
   - Relativ:              X = 50.00 %, Y = 85.19 %
   - Logische Koordinate:  (855, 943)   [macOS Points / pynput]
   - Physische Koordinate: (1710, 1886) [Retina Pixel im Screenshot]
   - Ruhefarbe (Hellgrau): RGB(200, 200, 200) / #C8C8C8

Hinweis zu macOS Retina & mss:
------------------------------
Screenshots unter macOS werden in physischen Retina-Pixeln (Faktor 2x: 3420x2214)
gespeichert, während `pynput` Maus-Koordinaten in logischen Quartz-Punkten
(1710x1107) liefert. Dieses Skript ermittelt sowohl den Retina-Backing-Scale-Faktor
als auch einen etwaigen Koordinaten-Skalierungsfaktor zwischen `pynput` und `mss`
automatisch, sodass der abgefragte 3x3-Bereich exakt mit der Mausposition
übereinstimmt.

Hotkeys:
--------
- [i] Aktuelle Mausposition als Schalt-Punkt speichern + RGB-Wert im Terminal ausgeben
- [o] Auto-Shifter starten / pausieren
- [p] Skript sauber beenden
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Tuple

import mss
from pynput import keyboard, mouse

try:
    from AppKit import NSScreen
except ImportError:
    NSScreen = None  # type: ignore[assignment]

try:
    import Quartz
except ImportError:
    Quartz = None  # type: ignore[assignment]


# ============================================================================
# KONFIGURATION & SCHWELLENWERTE
# ============================================================================

# Relative Standard-Position aus der Screenshot-Analyse (Schalt-Indikator-Kreis)
DEFAULT_REL_X: float = 0.5000  # 1710 / 3420 (exakt mittig)
DEFAULT_REL_Y: float = 0.7787  # 1724 / 2214 (Schalt-Kreis im Tacho)

# Überwachungsbereich rund um die Zielkoordinate (3x3 Bereich)
SAMPLE_BOX_SIZE: int = 3

# Schwellenwert-Logik für Grün-Erkennung (mit Toleranz)
GREEN_MIN_VALUE: int = 140
GREEN_DOMINANCE_RATIO: float = 1.35  # G > R * 1.35 und G > B * 1.35

# Timing-Parameter
KEY_HOLD_DURATION: float = 0.035  # Kurze Haltedauer (35 ms), damit Roblox die Eingabe sicher erkennt
SHIFT_COOLDOWN: float = 0.25      # 0.25 s Entprellung nach jedem Schaltvorgang
SCAN_INTERVAL: float = 0.004      # ~250 Hz Scan-Schleife für minimale Latenz


def create_mss_instance() -> Any:
    """Erstellt eine MSS-Instanz kompatibel mit mss 9.x und mss 10.x+ (ohne DeprecationWarning)."""
    mss_cls = getattr(mss, "MSS", None) or getattr(mss, "mss")
    return mss_cls()


def detect_retina_scale() -> float:
    """Ermittelt den physischen Retina-Skalierungsfaktor unter macOS (z. B. 2.0 auf Retina-Displays)."""
    if NSScreen is not None:
        try:
            main_screen = NSScreen.mainScreen()
            if main_screen is not None:
                return float(main_screen.backingScaleFactor())
        except Exception:
            pass
    return 2.0


class AutoShifter:
    """Performanter macOS Auto-Shifter für Taxi Boss (Roblox)."""

    def __init__(self) -> None:
        self.mouse_ctrl = mouse.Controller()
        self.kb_ctrl = keyboard.Controller()

        self._lock = threading.Lock()
        self.active: bool = False
        self.running: bool = True
        self.shift_count: int = 0

        self.retina_scale: float = detect_retina_scale()

        # Bildschirm- und Koordinatenskalierung zwischen pynput (Logisch) und mss prüfen
        with create_mss_instance() as sct:
            primary_mon = sct.monitors[1]
            self.mss_left: int = int(primary_mon["left"])
            self.mss_top: int = int(primary_mon["top"])
            self.mss_width: int = int(primary_mon["width"])
            self.mss_height: int = int(primary_mon["height"])

        # Logische Quartz-Auflösung bestimmen (Koordinatensystem von pynput)
        if Quartz is not None:
            main_id = Quartz.CGMainDisplayID()
            self.logical_width: int = int(Quartz.CGDisplayPixelsWide(main_id))
            self.logical_height: int = int(Quartz.CGDisplayPixelsHigh(main_id))
        else:
            self.logical_width = self.mss_width
            self.logical_height = self.mss_height

        # Faktor zwischen logischen pynput-Punkten und mss-Koordinaten
        # (1.0 wenn mss logische Quartz-Koordinaten nutzt, 2.0 falls mss physische Pixel erwartet)
        self.mss_coord_scale_x: float = (
            self.mss_width / self.logical_width if self.logical_width else 1.0
        )
        self.mss_coord_scale_y: float = (
            self.mss_height / self.logical_height if self.logical_height else 1.0
        )

        # Vorausgewählte logische Koordinate anhand der Vorab-Analyse setzen
        self.target_logical_x: int = int(round(self.logical_width * DEFAULT_REL_X))
        self.target_logical_y: int = int(round(self.logical_height * DEFAULT_REL_Y))

    @staticmethod
    def is_green(r: int, g: int, b: int) -> bool:
        """
        Prüft anhand der Schwellenwert-Logik, ob ein RGB-Wert als 'Grün'
        (optimaler Schaltzeitpunkt) gilt:
        G > 140 sowie G > R * 1.35 und G > B * 1.35
        """
        return (
            g > GREEN_MIN_VALUE
            and g > (r * GREEN_DOMINANCE_RATIO)
            and g > (b * GREEN_DOMINANCE_RATIO)
        )

    def _build_capture_bbox(self, logical_x: int, logical_y: int) -> dict[str, int]:
        """
        Rechnet die logische Mauskoordinate (pynput) exakt in das mss-Koordinatensystem um
        und erstellt eine 3x3-Bounding-Box rund um den Zielpunkt.
        """
        mss_x = int(round(logical_x * self.mss_coord_scale_x))
        mss_y = int(round(logical_y * self.mss_coord_scale_y))
        half = SAMPLE_BOX_SIZE // 2
        return {
            "left": mss_x - half,
            "top": mss_y - half,
            "width": SAMPLE_BOX_SIZE,
            "height": SAMPLE_BOX_SIZE,
        }

    def sample_region(
        self, sct: Any, logical_x: int, logical_y: int
    ) -> Tuple[Tuple[int, int, int], bool]:
        """
        Liest den 3x3-Bereich um (logical_x, logical_y) direkt aus dem BGRA-Rohpuffer aus.
        Gibt zurück:
          - (R, G, B): Farbwert des erkannten grünen Pixels bzw. des Zentrumspixels
          - is_green:  True, sobald mindestens ein Pixel im 3x3-Bereich den Grün-Schwellenwert erfüllt
        """
        bbox = self._build_capture_bbox(logical_x, logical_y)
        sct_img = sct.grab(bbox)

        # Rohdaten liegen in mss als BGRA vor (4 Bytes pro Pixel)
        raw: bytes = sct_img.raw
        width: int = sct_img.width
        height: int = sct_img.height

        # Zentrumspixel für die Kontrollausgabe im Terminal bestimmen
        cx = width // 2
        cy = height // 2
        center_offset = (cy * width + cx) * 4
        center_b = raw[center_offset]
        center_g = raw[center_offset + 1]
        center_r = raw[center_offset + 2]
        center_rgb = (center_r, center_g, center_b)

        # Schnelle Iteration über alle Pixel im 3x3-Überwachungsbereich
        total_bytes = len(raw)
        for offset in range(0, total_bytes, 4):
            b = raw[offset]
            g = raw[offset + 1]
            r = raw[offset + 2]
            if (
                g > GREEN_MIN_VALUE
                and g > (r * GREEN_DOMINANCE_RATIO)
                and g > (b * GREEN_DOMINANCE_RATIO)
            ):
                return (r, g, b), True

        return center_rgb, False

    def trigger_shift(self) -> None:
        """Drückt die Taste 'e' kurz und lässt sie wieder los (macOS/Roblox-kompatibel)."""
        self.kb_ctrl.press("e")
        time.sleep(KEY_HOLD_DURATION)
        self.kb_ctrl.release("e")

    def calibrate_at_mouse(self) -> None:
        """
        [i] Speichert die aktuelle Mausposition als Schalt-Punkt und gibt sowohl
        logische als auch physische Retina-Koordinaten sowie den gelesenen RGB-Wert aus.
        """
        mx, my = self.mouse_ctrl.position
        logical_x = int(round(mx))
        logical_y = int(round(my))

        with self._lock:
            self.target_logical_x = logical_x
            self.target_logical_y = logical_y

        with create_mss_instance() as sct:
            rgb, detected_green = self.sample_region(sct, logical_x, logical_y)

        phys_x = int(round(logical_x * self.retina_scale))
        phys_y = int(round(logical_y * self.retina_scale))
        r, g, b = rgb
        hex_color = f"#{r:02X}{g:02X}{b:02X}"
        green_status = "GRÜN ERKANNT (Schaltpunkt!)" if detected_green else "Nicht grün (Ruhezustand)"

        print(
            f"\n[KALIBRIERUNG - i] Aktuelle Mausposition als Schalt-Punkt gespeichert:\n"
            f"  -> Logische Koordinate (Points):       ({logical_x}, {logical_y})\n"
            f"  -> Physische Retina-Pixel ({self.retina_scale:.1f}x):      ({phys_x}, {phys_y})\n"
            f"  -> Gelesener RGB-Wert (3x3-Zentrum):   RGB({r}, {g}, {b}) | {hex_color} | {green_status}",
            flush=True,
        )

    def toggle_active(self) -> None:
        """[o] Startet oder pausiert den Auto-Shifter."""
        with self._lock:
            self.active = not self.active
            state = self.active
            tx, ty = self.target_logical_x, self.target_logical_y

        phys_tx = int(round(tx * self.retina_scale))
        phys_ty = int(round(ty * self.retina_scale))

        if state:
            print(
                f"\n[STATUS - o] Auto-Shifter AKTIVIERT – "
                f"Überwache Logisch ({tx}, {ty}) / Retina ({phys_tx}, {phys_ty}) ...",
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

    def monitor_loop(self) -> None:
        """
        Hauptschleife für die Echtzeit-Farberkennung.
        Nutzt eine dedizierte `mss`-Instanz im Worker-Thread für maximale Performance.
        """
        with create_mss_instance() as sct:
            while self.running:
                with self._lock:
                    is_active = self.active
                    tx = self.target_logical_x
                    ty = self.target_logical_y

                if not is_active:
                    time.sleep(0.03)
                    continue

                rgb, is_shift_ready = self.sample_region(sct, tx, ty)
                if is_shift_ready:
                    self.trigger_shift()
                    self.shift_count += 1
                    r, g, b = rgb
                    timestamp = time.strftime("%H:%M:%S")
                    print(
                        f"[{timestamp}] SCHALTEN #{self.shift_count:02d} -> Taste 'e' gedrückt! "
                        f"(Erkanntes Grün: RGB({r}, {g}, {b}) bei ({tx}, {ty}))",
                        flush=True,
                    )
                    # Cooldown (Entprellung), damit keine Gänge übersprungen werden
                    time.sleep(SHIFT_COOLDOWN)
                else:
                    time.sleep(SCAN_INTERVAL)

    def print_banner(self) -> None:
        """Gibt beim Start alle System-, Analyse- und Hotkey-Informationen im Terminal aus."""
        phys_w = int(round(self.logical_width * self.retina_scale))
        phys_h = int(round(self.logical_height * self.retina_scale))
        phys_tx = int(round(self.target_logical_x * self.retina_scale))
        phys_ty = int(round(self.target_logical_y * self.retina_scale))

        with create_mss_instance() as sct:
            init_rgb, _ = self.sample_region(sct, self.target_logical_x, self.target_logical_y)

        print("=" * 74)
        print("  TAXI BOSS (ROBLOX) – AUTO-SHIFTER FÜR macOS")
        print("=" * 74)
        print(f"  Bildschirm (Logisch):  {self.logical_width} x {self.logical_height} Punkte (pynput)")
        print(f"  Bildschirm (Retina):   {phys_w} x {phys_h} Pixel (Skalierung {self.retina_scale:.1f}x)")
        print(
            f"  Vorab-Schaltpunkt:     Logisch ({self.target_logical_x}, {self.target_logical_y}) | "
            f"Retina ({phys_tx}, {phys_ty})"
        )
        print(f"  Aktuelles RGB dort:    RGB{init_rgb} (3x3-Überwachungsbereich)")
        print(
            f"  Grün-Schwellenwert:    G > {GREEN_MIN_VALUE}, "
            f"G > R * {GREEN_DOMINANCE_RATIO}, G > B * {GREEN_DOMINANCE_RATIO}"
        )
        print(f"  Schalt-Cooldown:       {SHIFT_COOLDOWN:.2f} Sekunden")
        print("-" * 74)
        print("  Steuerung per Hotkeys:")
        print("    [i]   Aktuelle Mausposition als Schalt-Punkt speichern + RGB anzeigen")
        print("    [o]   Auto-Shifter starten / pausieren")
        print("    [p]   Skript sauber beenden")
        print("=" * 74)
        print("  Bereit! Bewege die Maus über den grauen Kreis und drücke [i],")
        print("  oder drücke direkt [o], um den Auto-Shifter zu starten.\n", flush=True)

    def run(self) -> None:
        """Startet den Überwachungs-Thread und den globalen Hotkey-Listener."""
        self.print_banner()

        worker = threading.Thread(target=self.monitor_loop, name="ShiftMonitorThread", daemon=True)
        worker.start()

        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> bool | None:
            try:
                char = getattr(key, "char", None)
                if isinstance(char, str):
                    char_lower = char.lower()
                    if char_lower == "i":
                        self.calibrate_at_mouse()
                    elif char_lower == "o":
                        self.toggle_active()
                    elif char_lower == "p":
                        self.stop()
                        return False  # Beendet den pynput-Listener
            except Exception as exc:
                print(f"[FEHLER] Hotkey-Verarbeitung: {exc}", file=sys.stderr, flush=True)
            return None

        try:
            with keyboard.Listener(on_press=on_press) as listener:
                listener.join()
        except KeyboardInterrupt:
            self.stop()

        worker.join(timeout=1.0)
        print("Skript sauber beendet.", flush=True)


if __name__ == "__main__":
    shifter = AutoShifter()
    shifter.run()
