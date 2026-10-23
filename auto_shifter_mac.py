#!/usr/bin/env python3
"""
auto_shifter_mac.py – Vollautomatischer Taxi Boss (Roblox) Renn-Bot für macOS
=============================================================================

Nutzt die geteilten Kernfunktionen aus `core_utils.py` sowie `config.json` und
stellt die macOS-spezifischen Treiber (Quartz CoreGraphics / Retina-Skalierung /
pynput) bereit.

Ablauf der 3 Phasen im Vollautomatik-Modus (`FULL_AUTO`):
---------------------------------------------------------
1. Phase 1 (`WAITING_AT_LIGHT` – Referenz: Bildschirmfoto 13.53.03):
   - Erkennt die dunkle Ampel-Box oben in der Mitte (`Traffic_Light_Point`).
   - Regelt die Taste `w` per schnellem PWM/Feathering so, dass die weiße
     Tachonadel am `Needle_Target_Point` im blauen Bereich zwischen 4 und 5
     gehalten wird (sobald weiße Pixel erkannt werden -> sofort `release('w')`).
2. Phase 2 (`RACING` – Referenz: Bildschirmfoto 09.32.46):
   - Sobald die Ampel-Box verschwindet (`GO!`), wird `w` durchgehend gehalten.
   - Überwacht `Shift_Point` mit ~120 Hz und drückt sofort `e`, sobald der
     Schalt-Kreis grün wird (inkl. dynamischem 85–220 ms Cooldown für Gang 1 & 2).
3. Phase 3 (`POST_RACE_POPUP` – Referenz: Bildschirmfoto 13.58.00):
   - Sobald der grüne `WEITER`-Button (`Continue_Button_Point`) erscheint, wird
     `w` sofort losgelassen (`release('w')`).
   - Klickt automatisch auf `WEITER` und wechselt zurück in Phase 1, sobald die
     Ampel für das nächste Rennen erscheint.

Hotkeys (macOS):
----------------
- [o]       Bot starten / pausieren (mit automatischem Safety-Release für W & E)
- [p]       Skript sauber beenden (mit garantiertem Safety-Release für W & E)
- [m]       Modus wechseln: Vollautomatik-Bot (`FULL_AUTO`) <-> Nur Auto-Shifter (`SHIFT_ONLY`)
- [i] / [1] Kalibriere `Shift_Point` (kleiner Kreis im Tacho für E)
- [2]       Kalibriere `Needle_Target_Point` (weiße Tachonadel zwischen 4 und 5)
- [3]       Kalibriere `Traffic_Light_Point` (Ampel-Box oben in der Mitte)
- [4]       Kalibriere `Continue_Button_Point` (grüner WEITER-Button im End-Popup)
"""

from __future__ import annotations

import atexit
import os
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from typing import Dict, Tuple

from pynput import keyboard, mouse

from core_utils import (
    ConfigManager,
    DragRaceStateMachine,
    InputDriver,
    ScreenCaptureManager,
    create_mss_instance,
    is_continue_button_green,
    is_needle_white,
    is_shift_green,
    is_traffic_light_present,
)

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
# macOS VIRTUAL KEYCODES
# ============================================================================

MACOS_VK_W: int = 13  # Taste 'w'
MACOS_VK_E: int = 14  # Taste 'e'

MACOS_VK_HOTKEYS: Dict[int, str] = {
    34: "i",  # 'i' -> Shift_Point kalibrieren
    31: "o",  # 'o' -> Start / Pause
    35: "p",  # 'p' -> Beenden
    46: "m",  # 'm' -> Modus wechseln (Full-Auto <-> Shift-Only)
    18: "1",  # '1' -> Shift_Point kalibrieren
    19: "2",  # '2' -> Needle_Target_Point kalibrieren
    20: "3",  # '3' -> Traffic_Light_Point kalibrieren
    21: "4",  # '4' -> Continue_Button_Point kalibrieren
}


def detect_retina_scale() -> float:
    """Ermittelt den physischen Retina-Skalierungsfaktor unter macOS (z. B. 2.0x)."""
    if NSScreen is not None:
        try:
            main_screen = NSScreen.mainScreen()
            if main_screen is not None:
                return float(main_screen.backingScaleFactor())
        except Exception:
            pass
    return 2.0


def detect_logical_screen_size() -> Tuple[int | None, int | None]:
    """Liest die logische Quartz-Bildschirmauflösung (Points) unter macOS aus."""
    if Quartz is not None:
        try:
            main_id = Quartz.CGMainDisplayID()
            return (
                int(Quartz.CGDisplayPixelsWide(main_id)),
                int(Quartz.CGDisplayPixelsHigh(main_id)),
            )
        except Exception:
            pass
    return None, None


def check_and_request_macos_permissions() -> Tuple[bool, bool, bool]:
    """Prüft Bildschirmaufnahme-, Eingabeüberwachungs- und Bedienungshilfen-Rechte unter macOS."""
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


# ============================================================================
# macOS INPUT-TREIBER MIT SAFETY-RELEASE
# ============================================================================

class MacInputDriver(InputDriver):
    """
    Nativ-performanter macOS-Treiber für Tastatur (`w`, `e`) und Maus-Klicks.
    Nutzt Quartz CoreGraphics CGEvents (mit `pynput` als Fallback) und garantiert
    über `release_all()`, dass niemals eine Taste hängen bleibt.
    """

    def __init__(self) -> None:
        self.kb_ctrl = keyboard.Controller()
        self.mouse_ctrl = mouse.Controller()
        self._lock = threading.Lock()
        self._w_pressed: bool = False
        self._last_w_refresh: float = 0.0

        # Safety-Hook beim Beenden des Python-Prozesses registrieren
        atexit.register(self.release_all)

    def _post_key(self, vk: int, char: str, is_down: bool) -> None:
        """Sendet ein einzelnes Tastatur-Event über Quartz (oder pynput als Fallback)."""
        if Quartz is not None:
            try:
                evt = Quartz.CGEventCreateKeyboardEvent(None, vk, is_down)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, evt)
                return
            except Exception:
                pass
        if is_down:
            self.kb_ctrl.press(char)
        else:
            self.kb_ctrl.release(char)

    def press_w(self) -> None:
        """Drückt und hält 'w'. Erneuert das KeyDown-Signal periodisch für Roblox."""
        now = time.monotonic()
        with self._lock:
            if not self._w_pressed or (now - self._last_w_refresh) >= 0.18:
                self._post_key(MACOS_VK_W, "w", True)
                self._w_pressed = True
                self._last_w_refresh = now

    def release_w(self) -> None:
        """Lässt 'w' sofort los."""
        with self._lock:
            if self._w_pressed:
                self._post_key(MACOS_VK_W, "w", False)
                self._w_pressed = False

    def tap_e(self, hold_duration: float = 0.012) -> None:
        """Drückt 'e' kurz (Up-Shift) und lässt es sofort wieder los."""
        self._post_key(MACOS_VK_E, "e", True)
        time.sleep(hold_duration)
        self._post_key(MACOS_VK_E, "e", False)
        # Falls W gehalten wird, KeyDown für W direkt auffrischen, damit Roblox Vollgas beibehält
        with self._lock:
            if self._w_pressed:
                self._post_key(MACOS_VK_W, "w", True)
                self._last_w_refresh = time.monotonic()

    def click_at(self, logical_x: int, logical_y: int) -> None:
        """
        Bewegt den Mauszeiger zum Zielpunkt (inkl. minimalem Hover-Impuls für Roblox UI)
        und führt einen zuverlässigen Linksklick aus.
        """
        if Quartz is not None:
            try:
                pt_hover = Quartz.CGPoint(float(logical_x + 1), float(logical_y))
                pt = Quartz.CGPoint(float(logical_x), float(logical_y))

                move1 = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved, pt_hover, Quartz.kCGMouseButtonLeft
                )
                move2 = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved, pt, Quartz.kCGMouseButtonLeft
                )
                down = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventLeftMouseDown, pt, Quartz.kCGMouseButtonLeft
                )
                up = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventLeftMouseUp, pt, Quartz.kCGMouseButtonLeft
                )

                Quartz.CGEventPost(Quartz.kCGHIDEventTap, move1)
                time.sleep(0.010)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, move2)
                time.sleep(0.015)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
                time.sleep(0.025)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
                return
            except Exception:
                pass

        self.mouse_ctrl.position = (logical_x, logical_y)
        time.sleep(0.015)
        self.mouse_ctrl.press(mouse.Button.left)
        time.sleep(0.025)
        self.mouse_ctrl.release(mouse.Button.left)

    def release_all(self) -> None:
        """
        Safety-Release: Lässt garantiert 'w' und 'e' sowohl über Quartz als auch über
        pynput los, damit auf macOS niemals eine Taste hängen bleibt.
        """
        with self._lock:
            self._w_pressed = False
        try:
            self._post_key(MACOS_VK_W, "w", False)
            self._post_key(MACOS_VK_E, "e", False)
        except Exception:
            pass
        try:
            self.kb_ctrl.release("w")
            self.kb_ctrl.release("e")
        except Exception:
            pass


# ============================================================================
# macOS HAUPTSTEUERUNG & KALIBRIERUNG
# ============================================================================

class MacDragBotApp:
    """Verbindet `DragRaceStateMachine`, `ConfigManager` und macOS-Hotkeys."""

    CALIBRATION_MAP: Dict[str, Tuple[str, str]] = {
        "1": ("shift_point", "Shift_Point (Kleiner Kreis im Tacho für E)"),
        "i": ("shift_point", "Shift_Point (Kleiner Kreis im Tacho für E)"),
        "2": ("needle_target_point", "Needle_Target_Point (Weiße Tachonadel zwischen 4 und 5)"),
        "3": ("traffic_light_point", "Traffic_Light_Point (Ampel-Box oben in der Mitte)"),
        "4": ("continue_button_point", "Continue_Button_Point (Grüner WEITER-Button im Popup)"),
    }

    def __init__(self) -> None:
        self.config = ConfigManager()
        retina_scale = detect_retina_scale()
        log_w, log_h = detect_logical_screen_size()

        self.capture = ScreenCaptureManager(
            logical_width=log_w,
            logical_height=log_h,
            retina_scale=retina_scale,
        )
        self.driver = MacInputDriver()
        self.bot = DragRaceStateMachine(
            config=self.config,
            capture=self.capture,
            driver=self.driver,
        )
        self.mouse_ctrl = mouse.Controller()
        self._cmd_lock = threading.Lock()
        self._last_cmd_time: Dict[str, float] = {}

    def calibrate_point(self, key_cmd: str) -> None:
        """
        Speichert die aktuelle Mausposition für den gewählten Punkt in `config.json`
        und gibt logische/physische Koordinaten sowie den gelesenen RGB-Wert aus.
        """
        if key_cmd not in self.CALIBRATION_MAP:
            return

        point_name, label = self.CALIBRATION_MAP[key_cmd]
        mx, my = self.mouse_ctrl.position
        lx, ly = int(round(mx)), int(round(my))
        px, py = self.capture.to_physical_coords(lx, ly)

        bbox = self.capture.build_bbox(lx, ly, box_size=3)
        with create_mss_instance() as sct:
            rgb, _ = self.capture.sample_bbox_fast(sct, bbox)

        r, g, b = rgb
        hex_color = f"#{r:02X}{g:02X}{b:02X}"

        # Für die Ampel speichern wir zusätzlich den aktuellen RGB-Wert als Referenz
        ref_rgb = rgb if point_name == "traffic_light_point" else None
        self.config.update_point_from_logical(
            point_name=point_name,
            logical_x=lx,
            logical_y=ly,
            logical_width=self.capture.logical_width,
            logical_height=self.capture.logical_height,
            ref_rgb=ref_rgb,
        )
        self.bot.refresh_bboxes()

        # Passende Farbdiagnose ausgeben
        if point_name == "shift_point":
            status = "GRÜN (Schaltpunkt!)" if is_shift_green(r, g, b) else "Grau/Ruhezustand (Bereit für Grün)"
        elif point_name == "needle_target_point":
            status = "WEISS (Nadel erkannt!)" if is_needle_white(r, g, b) else "Dunkel (Bereit für weiße Nadel)"
        elif point_name == "traffic_light_point":
            status = (
                "Ampel-Box erkannt (Referenz gespeichert)"
                if is_traffic_light_present(r, g, b, rgb)
                else "Referenzfarbe für Ampel gespeichert"
            )
        else:
            status = (
                "WEITER-Grün erkannt!"
                if is_continue_button_green(r, g, b)
                else "Position gespeichert (Bereit für grünen WEITER-Button)"
            )

        print(
            f"\n[KALIBRIERUNG - Taste '{key_cmd}'] {label} gespeichert in config.json:\n"
            f"  -> Logisch (Points):                   ({lx}, {ly})\n"
            f"  -> Physisch Retina ({self.capture.retina_scale:.1f}x):             ({px}, {py})\n"
            f"  -> Gelesener RGB-Wert (3x3-Zentrum):   RGB({r}, {g}, {b}) | {hex_color} | {status}",
            flush=True,
        )

    def handle_command(self, cmd: str) -> bool:
        """Verarbeitet Hotkey-Befehle mit 180 ms Entprellung. Gibt True bei Beenden ('p') zurück."""
        now = time.monotonic()
        with self._cmd_lock:
            if now - self._last_cmd_time.get(cmd, 0.0) < 0.18:
                return not self.bot.running
            self._last_cmd_time[cmd] = now

        if cmd in self.CALIBRATION_MAP:
            self.calibrate_point(cmd)
        elif cmd == "o":
            self.bot.toggle_active()
        elif cmd == "m":
            self.bot.toggle_mode()
        elif cmd == "p":
            print("\n[BEENDEN - p] Bot wird sauber beendet (Safety-Release für W & E) ...", flush=True)
            self.bot.stop()
            return True
        return False

    def stdin_fallback_loop(self) -> None:
        """Erlaubt die Steuerung auch direkt durch Tastendruck im Terminal / in PyCharm."""
        if not sys.stdin or sys.stdin.closed:
            return

        fd = None
        old_settings = None
        is_tty = False
        valid_cmds = ("o", "p", "m", "i", "1", "2", "3", "4")

        try:
            fd = sys.stdin.fileno()
            is_tty = os.isatty(fd)
            if is_tty:
                old_settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
        except Exception:
            is_tty = False

        try:
            while self.bot.running:
                if is_tty and fd is not None:
                    rlist, _, _ = select.select([fd], [], [], 0.1)
                    if rlist:
                        ch = os.read(fd, 1).decode("utf-8", errors="ignore").lower()
                        if ch in valid_cmds:
                            if self.handle_command(ch):
                                break
                else:
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if rlist:
                        line = sys.stdin.readline()
                        if not line:
                            break
                        cmd = line.strip().lower()
                        if cmd in valid_cmds:
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
        """Gibt alle System-, Punkt- und Hotkey-Informationen übersichtlich im Terminal aus."""
        lw, lh = self.capture.logical_width, self.capture.logical_height
        pw, ph = self.capture.to_physical_coords(lw, lh)

        s_pt = self.bot.shift_pt
        n_pt = self.bot.needle_pt
        l_pt = self.bot.light_pt
        c_pt = self.bot.continue_pt

        print("=" * 80)
        print("  TAXI BOSS (ROBLOX) – VOLLAUTOMATISCHER RENN-BOT FÜR macOS")
        print("=" * 80)
        print(f"  Bildschirm (Logisch):     {lw} x {lh} Punkte (pynput)")
        print(f"  Bildschirm (Retina):      {pw} x {ph} Pixel (Skalierung {self.capture.retina_scale:.1f}x)")
        print(f"  Konfigurationsdatei:      {self.config.config_path}")
        print(f"  Aktiver Modus:            {self.bot.mode.value} (Vollautomatik: Ampel -> Rennen -> Popup)")
        print("-" * 80)
        print("  Aktuelle Koordinaten (Logisch | Retina):")
        print(f"    [1 / i] Shift_Point (Kreis für E):         {s_pt} | {self.capture.to_physical_coords(*s_pt)}")
        print(f"    [2]     Needle_Target_Point (Nadel 4–5):   {n_pt} | {self.capture.to_physical_coords(*n_pt)}")
        print(f"    [3]     Traffic_Light_Point (Ampel oben):  {l_pt} | {self.capture.to_physical_coords(*l_pt)}")
        print(f"    [4]     Continue_Button_Point (WEITER):    {c_pt} | {self.capture.to_physical_coords(*c_pt)}")
        print("-" * 80)
        print("  macOS-Berechtigungsstatus:")
        print(f"    - Bildschirmaufnahme (mss):           {'OK' if screen_ok else 'FEHLT!'}")
        print(f"    - Eingabeüberwachung (Hotkeys):       {'OK' if listen_ok else 'FEHLT!'}")
        print(f"    - Bedienungshilfen (Tasten/Maus):     {'OK' if post_ok else 'FEHLT!'}")
        print("-" * 80)
        print("  Steuerung per Hotkeys:")
        print("    [o]       Bot STARTEN / PAUSIEREN (inkl. Safety-Release für W & E)")
        print("    [p]       Skript SAUBER BEENDEN   (inkl. Safety-Release für W & E)")
        print("    [m]       Modus wechseln: FULL_AUTO (Renn-Bot) <-> SHIFT_ONLY (Nur Schalten)")
        print("    [1] / [i] Mausposition als Shift_Point speichern")
        print("    [2]       Mausposition als Needle_Target_Point (zwischen 4 & 5) speichern")
        print("    [3]       Mausposition als Traffic_Light_Point (Ampel oben) speichern")
        print("    [4]       Mausposition als Continue_Button_Point (WEITER-Button) speichern")
        print("=" * 80)
        print("  Bereit! Drücke [o], um den vollautomatischen Renn-Bot zu starten.\n", flush=True)

    def run(self) -> None:
        """Startet State-Machine-Thread, Terminal-Fallback und globalen Hotkey-Listener."""
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

        worker = threading.Thread(target=self.bot.run_loop, name="DragBotStateMachine", daemon=True)
        worker.start()

        stdin_worker = threading.Thread(
            target=self.stdin_fallback_loop, name="StdinHotkeyThread", daemon=True
        )
        stdin_worker.start()

        valid_chars = {"o", "p", "m", "i", "1", "2", "3", "4"}

        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> bool | None:
            try:
                cmd: str | None = None
                char = getattr(key, "char", None)
                if isinstance(char, str) and char.lower() in valid_chars:
                    cmd = char.lower()
                else:
                    vk = getattr(key, "vk", None)
                    if isinstance(vk, int) and vk in MACOS_VK_HOTKEYS:
                        cmd = MACOS_VK_HOTKEYS[vk]

                if cmd is not None:
                    # Kalibrierungstasten 1-4 während aktiver Fahrt ignorieren, falls versehentlich gedrückt,
                    # außer der Bot ist pausiert oder 'i' wird gedrückt.
                    if cmd in ("1", "2", "3", "4") and self.bot.active:
                        return None
                    if self.handle_command(cmd):
                        return False
            except Exception as exc:
                print(f"[FEHLER] Hotkey-Verarbeitung: {exc}", file=sys.stderr, flush=True)
            return None

        try:
            with keyboard.Listener(on_press=on_press) as listener:
                while self.bot.running and listener.is_alive():
                    time.sleep(0.1)
                listener.stop()
        except KeyboardInterrupt:
            self.bot.stop()
        finally:
            self.driver.release_all()

        worker.join(timeout=1.0)
        self.driver.release_all()
        print("Skript sauber beendet (alle Tasten freigegeben).", flush=True)


if __name__ == "__main__":
    app = MacDragBotApp()
    app.run()
