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

Hotkeys:
--------
- [i] Aktuelle Mausposition als Schalt-Punkt speichern + RGB-Wert im Terminal ausgeben
      (WICHTIG: Der Kreis darf dabei noch GRAU sein! Es wird nur die Position gespeichert.)
- [o] Auto-Shifter starten / pausieren
- [p] Skript sauber beenden
"""


from __future__ import annotations

import os
import select
import subprocess
import sys
import termios
import threading
import time
import tty
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

try:
    import ApplicationServices
except ImportError:
    ApplicationServices = None  # type: ignore[assignment]


# ============================================================================
# KONFIGURATION & SCHWELLENWERTE (OPTIMIERT FÜR GANG 1 & 2)
# ============================================================================

# Relative Standard-Position aus der Screenshot-Analyse (Schalt-Indikator-Kreis)
DEFAULT_REL_X: float = 0.5000  # 1710 / 3420 (exakt mittig)
DEFAULT_REL_Y: float = 0.7787  # 1724 / 2214 (Schalt-Kreis im Tacho)

# Überwachungsbereich rund um die Zielkoordinate (3x3 Bereich)
SAMPLE_BOX_SIZE: int = 3

# Schwellenwert-Logik für Grün-Erkennung:
# Etwas sensibler eingestellt (G > 95 statt 140), damit bereits der allererste
# Übergangs-Frame von Grau RGB(50,50,50) zu Grün im 1. und 2. Gang sofort auslöst!
GREEN_MIN_VALUE: int = 95
GREEN_DOMINANCE_RATIO: float = 1.22  # G > R * 1.22 und G > B * 1.22

# Timing-Parameter (Ultra-Low-Latency für schnelle erste Gänge):
KEY_HOLD_DURATION: float = 0.012  # 12 ms Tastendruck (schnellstmögliche Registrierung in Roblox)
MIN_SHIFT_COOLDOWN: float = 0.085 # 85 ms Mindest-Pause, damit der 2. Gang nicht verschlafen wird
MAX_SHIFT_COOLDOWN: float = 0.22  # Max. Wartezeit, falls der Kreis länger grün bleibt
SCAN_INTERVAL: float = 0.0        # 0.0 s Zusatzpause -> volle 120 Hz Synchronisation mit macOS WindowServer

# macOS Virtual KeyCodes für i, o, p + e
MACOS_VK_MAP: dict[int, str] = {
    34: "i",  # Taste 'i'
    31: "o",  # Taste 'o'
    35: "p",  # Taste 'p'
}
MACOS_VK_E: int = 14  # Physischer Keycode für 'e' auf macOS


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


def check_and_request_macos_permissions() -> Tuple[bool, bool, bool]:
    """
    Prüft die macOS-Berechtigungen für:
      1. Bildschirmaufnahme (Screen Recording) -> für mss
      2. Eingabeüberwachung (Input Monitoring) -> für globale Hotkeys (i, o, p)
      3. Bedienungshilfen (Accessibility)      -> für automatisches Drücken von 'e'
    """
    screen_ok = True
    listen_ok = True
    post_ok = True

    if Quartz is not None:
        if hasattr(Quartz, "CGPreflightScreenCaptureAccess"):
            screen_ok = bool(Quartz.CGPreflightScreenCaptureAccess())
            if not screen_ok and hasattr(Quartz, "CGRequestScreenCaptureAccess"):
                Quartz.CGRequestScreenCaptureAccess()

        if hasattr(Quartz, "CGPreflightListenEventAccess"):
            listen_ok = bool(Quartz.CGPreflightListenEventAccess())
            if not listen_ok and hasattr(Quartz, "CGRequestListenEventAccess"):
                Quartz.CGRequestListenEventAccess()

        if hasattr(Quartz, "CGPreflightPostEventAccess"):
            post_ok = bool(Quartz.CGPreflightPostEventAccess())
            if not post_ok and hasattr(Quartz, "CGRequestPostEventAccess"):
                Quartz.CGRequestPostEventAccess()

    if ApplicationServices is not None and hasattr(ApplicationServices, "AXIsProcessTrusted"):
        ax_ok = bool(ApplicationServices.AXIsProcessTrusted())
        if not ax_ok and hasattr(ApplicationServices, "AXIsProcessTrustedWithOptions"):
            try:
                ApplicationServices.AXIsProcessTrustedWithOptions(
                    {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
                )
            except Exception:
                pass
        post_ok = post_ok and ax_ok

    return screen_ok, listen_ok, post_ok


class AutoShifter:
    """Performanter macOS Auto-Shifter für Taxi Boss (Roblox)."""

    def __init__(self) -> None:
        self.mouse_ctrl = mouse.Controller()
        self.kb_ctrl = keyboard.Controller()

        self._lock = threading.Lock()
        self.active: bool = False
        self.running: bool = True
        self.shift_count: int = 0
        self._last_cmd_time: dict[str, float] = {"i": 0.0, "o": 0.0, "p": 0.0}

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
        self.mss_coord_scale_x: float = (
            self.mss_width / self.logical_width if self.logical_width else 1.0
        )
        self.mss_coord_scale_y: float = (
            self.mss_height / self.logical_height if self.logical_height else 1.0
        )

        # Vorausgewählte logische Koordinate anhand der Vorab-Analyse setzen
        self.target_logical_x: int = int(round(self.logical_width * DEFAULT_REL_X))
        self.target_logical_y: int = int(round(self.logical_height * DEFAULT_REL_Y))
        self._cached_bbox: dict[str, int] = self._build_capture_bbox(
            self.target_logical_x, self.target_logical_y
        )

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

    def sample_region_fast(self, sct: Any, bbox: dict[str, int]) -> Tuple[Tuple[int, int, int], bool]:
        """
        Ultraschnelle Auswertung des 3x3-Bereichs direkt auf dem BGRA-Speicherpuffer.
        """
        sct_img = sct.grab(bbox)
        raw: bytes = sct_img.raw

        # Schnelle Prüfung über alle Pixel im 3x3-Überwachungsbereich
        for offset in range(0, len(raw), 4):
            g = raw[offset + 1]
            if g > GREEN_MIN_VALUE:
                b = raw[offset]
                r = raw[offset + 2]
                if g > (r * GREEN_DOMINANCE_RATIO) and g > (b * GREEN_DOMINANCE_RATIO):
                    return (r, g, b), True

        # Falls nicht grün: Zentrumspixel für Diagnose/Kalibrierung zurückgeben
        width: int = sct_img.width
        height: int = sct_img.height
        center_offset = ((height // 2) * width + (width // 2)) * 4
        return (raw[center_offset + 2], raw[center_offset + 1], raw[center_offset]), False

    def sample_region(
        self, sct: Any, logical_x: int, logical_y: int
    ) -> Tuple[Tuple[int, int, int], bool]:
        """Komfort-Wrapper für Kalibrierung und Diagnose."""
        bbox = self._build_capture_bbox(logical_x, logical_y)
        return self.sample_region_fast(sct, bbox)

    def trigger_shift(self) -> None:
        """
        Sendet den Tastendruck 'e' mit minimaler Latenz direkt über Quartz CGEvents
        (bzw. pynput als Fallback), damit keine Millisekunde im 1./2. Gang verloren geht.
        """
        if Quartz is not None:
            down_evt = Quartz.CGEventCreateKeyboardEvent(None, MACOS_VK_E, True)
            up_evt = Quartz.CGEventCreateKeyboardEvent(None, MACOS_VK_E, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, down_evt)
            time.sleep(KEY_HOLD_DURATION)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up_evt)
        else:
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
        new_bbox = self._build_capture_bbox(logical_x, logical_y)

        with self._lock:
            self.target_logical_x = logical_x
            self.target_logical_y = logical_y
            self._cached_bbox = new_bbox

        with create_mss_instance() as sct:
            rgb, detected_green = self.sample_region_fast(sct, new_bbox)

        phys_x = int(round(logical_x * self.retina_scale))
        phys_y = int(round(logical_y * self.retina_scale))
        r, g, b = rgb
        hex_color = f"#{r:02X}{g:02X}{b:02X}"
        green_status = (
            "GRÜN ERKANNT (Schaltpunkt!)"
            if detected_green
            else "Grau / Nicht grün (Position gespeichert – bereit für Grün!)"
        )

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
                f"Überwache Logisch ({tx}, {ty}) / Retina ({phys_tx}, {phys_ty}) mit 120 Hz ...",
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
        """
        Verarbeitet einen Hotkey-Befehl ('i', 'o', 'p') mit kurzer Entprellung (150 ms).
        """
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
        Hochgeschwindigkeits-Hauptschleife (~120 Hz, ohne künstliches Sleep-Delay):
        1. Sobald Grün erkannt wird, wird SOFORT (vor jeder Terminal-Ausgabe) 'e' gedrückt.
        2. Danach greift eine intelligente Entprellung: Nach nur 85 ms Mindestpause
           wird sofort geprüft, wann der Kreis wieder grau wird (statt starr 250 ms
           zu schlafen). Dadurch werden auch extrem schnelle Gangwechsel vom 1. in den
           2. und 3. Gang rechtzeitig erwischt.
        """
        with create_mss_instance() as sct:
            while self.running:
                if not self.active:
                    time.sleep(0.025)
                    continue

                bbox = self._cached_bbox
                rgb, is_shift_ready = self.sample_region_fast(sct, bbox)

                if is_shift_ready:
                    # 1. SOFORT schalten – keine Zeit durch print() vor dem Tastendruck verlieren!
                    shift_start = time.perf_counter()
                    self.trigger_shift()
                    self.shift_count += 1

                    # 2. Terminal-Ausgabe während ohnehin die Mindest-Entprellung läuft
                    r, g, b = rgb
                    tx, ty = self.target_logical_x, self.target_logical_y
                    timestamp = time.strftime("%H:%M:%S")
                    print(
                        f"[{timestamp}] SCHALTEN #{self.shift_count:02d} -> Taste 'e' gedrückt! "
                        f"(RGB({r}, {g}, {b}) bei ({tx}, {ty}))",
                        flush=True,
                    )

                    # 3. Kurze Mindest-Entprellung (85 ms ab Schaltbeginn), damit Roblox den Gang wechselt
                    elapsed = time.perf_counter() - shift_start
                    if elapsed < MIN_SHIFT_COOLDOWN:
                        time.sleep(MIN_SHIFT_COOLDOWN - elapsed)

                    # 4. Dynamisches Warten bis der Kreis nach dem Schalten wieder grau ist
                    #    (maximal bis MAX_SHIFT_COOLDOWN = 220 ms Gesamtdauer).
                    #    Sobald er grau ist, ist das Skript SOFORT bereit für den nächsten Gang!
                    while self.running and self.active:
                        if (time.perf_counter() - shift_start) >= MAX_SHIFT_COOLDOWN:
                            break
                        _, still_green = self.sample_region_fast(sct, self._cached_bbox)
                        if not still_green:
                            break
                elif SCAN_INTERVAL > 0.0:
                    time.sleep(SCAN_INTERVAL)

    def stdin_fallback_loop(self) -> None:
        """
        Fallback-Tastatur-Listener direkt im Terminal / in der PyCharm-Konsole.
        """
        if not sys.stdin or sys.stdin.closed:
            return

        fd = None
        old_settings = None
        is_tty = False
        try:
            fd = sys.stdin.fileno()
            is_tty = os.isatty(fd)
            if is_tty:
                old_settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
        except Exception:
            is_tty = False

        try:
            while self.running:
                if is_tty and fd is not None:
                    rlist, _, _ = select.select([fd], [], [], 0.1)
                    if rlist:
                        ch = os.read(fd, 1).decode("utf-8", errors="ignore").lower()
                        if ch in ("i", "o", "p"):
                            if self.handle_command(ch):
                                break
                else:
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if rlist:
                        line = sys.stdin.readline()
                        if not line:
                            break
                        cmd = line.strip().lower()
                        if cmd in ("i", "o", "p"):
                            if self.handle_command(cmd):
                                break
        except Exception:
            pass
        finally:
            if is_tty and fd is not None and old_settings is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass

    def print_banner(self, screen_ok: bool, listen_ok: bool, post_ok: bool) -> None:
        """Gibt beim Start alle System-, Berechtigungs- und Hotkey-Informationen im Terminal aus."""
        phys_w = int(round(self.logical_width * self.retina_scale))
        phys_h = int(round(self.logical_height * self.retina_scale))
        phys_tx = int(round(self.target_logical_x * self.retina_scale))
        phys_ty = int(round(self.target_logical_y * self.retina_scale))

        with create_mss_instance() as sct:
            init_rgb, _ = self.sample_region(sct, self.target_logical_x, self.target_logical_y)

        print("=" * 76)
        print("  TAXI BOSS (ROBLOX) – AUTO-SHIFTER FÜR macOS (TURBO-MODUS GANG 1 & 2)")
        print("=" * 76)
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
        print(
            f"  Dynamischer Cooldown:  {MIN_SHIFT_COOLDOWN*1000:.0f}–{MAX_SHIFT_COOLDOWN*1000:.0f} ms "
            f"(sofort bereit, sobald Kreis wieder grau)"
        )
        print("-" * 76)
        print("  Steuerung per Hotkeys:")
        print("    [i]   Mausposition als Schalt-Punkt speichern + RGB anzeigen")
        print("          (Der Kreis darf beim Drücken von [i] GRAU sein – es wird nur der Ort gespeichert!)")
        print("    [o]   Auto-Shifter starten / pausieren")
        print("    [p]   Skript sauber beenden")
        print("=" * 76, flush=True)

    def run(self) -> None:
        """Startet den Überwachungs-Thread, den Konsole-Fallback und den globalen Hotkey-Listener."""
        screen_ok, listen_ok, post_ok = check_and_request_macos_permissions()
        self.print_banner(screen_ok, listen_ok, post_ok)

        if not (listen_ok and post_ok):
            try:
                subprocess.run(
                    [
                        "open",
                        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                    ],
                    check=False,
                )
            except Exception:
                pass

        worker = threading.Thread(target=self.monitor_loop, name="ShiftMonitorThread", daemon=True)
        worker.start()

        stdin_worker = threading.Thread(
            target=self.stdin_fallback_loop, name="StdinHotkeyThread", daemon=True
        )
        stdin_worker.start()

        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> bool | None:
            try:
                cmd: str | None = None
                char = getattr(key, "char", None)
                if isinstance(char, str) and char.lower() in ("i", "o", "p"):
                    cmd = char.lower()
                else:
                    vk = getattr(key, "vk", None)
                    if isinstance(vk, int) and vk in MACOS_VK_MAP:
                        cmd = MACOS_VK_MAP[vk]

                if cmd is not None:
                    if self.handle_command(cmd):
                        return False  # Beendet den pynput-Listener
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

        worker.join(timeout=1.0)
        print("Skript sauber beendet.", flush=True)


if __name__ == "__main__":
    shifter = AutoShifter()
    shifter.run()
