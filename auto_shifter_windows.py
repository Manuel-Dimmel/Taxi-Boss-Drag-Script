#!/usr/bin/env python3
"""
auto_shifter_windows.py – Vollautomatischer Taxi Boss (Roblox) Renn-Bot für Windows
===================================================================================

Nutzt die geteilten Kernfunktionen aus `core_utils.py` sowie `config.json` und
stellt die Windows-spezifischen Treiber (Win32 `SendInput` mit Hardware-ScanCodes
für Roblox DirectInput, Per-Monitor-DPI-Awareness und 1-ms-Systemtimer) bereit.

Hotkeys (Windows):
------------------
- [o]       Bot starten / pausieren (mit automatischem Safety-Release für W & E)
- [p]       Skript sauber beenden (mit garantiertem Safety-Release für W & E)
- [m]       Modus wechseln: Vollautomatik-Bot (`FULL_AUTO`) <-> Nur Auto-Shifter (`SHIFT_ONLY`)
- [i] / [1] Kalibriere `shift_point` (kleiner Kreis im Tacho für E)
- [2]       Kalibriere `needle_target_point` (weiße Tachonadel zwischen 4 und 5)
- [3]       Kalibriere `traffic_light_point` (Ampel-Box oben in der Mitte)
- [4]       Kalibriere `weiter_button` (grüner WEITER-Button in Popup 1 / Rennstatistik)
- [5]       Kalibriere `next_stage_button` (grüner NEXT STAGE-Button in Popup 2 / Ladder)
- [6]       Kalibriere `rennen_button` (grüner RENNEN-Button in Popup 3 / VS-Screen)
"""

from __future__ import annotations

import atexit
import ctypes
import sys
import threading
import time
from typing import Dict, Tuple

# ============================================================================
# WINDOWS DPI-AWARENESS & 1-MS-TIMER (VOR DEM IMPORT VON PYNPUT / MSS!)
# ============================================================================
IS_WINDOWS: bool = sys.platform == "win32"

if IS_WINDOWS:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

from pynput import keyboard, mouse

from core_utils import (
    MENU_CLICK_COOLDOWN,
    TAP_COOLDOWN,
    TAP_DURATION,
    ConfigManager,
    DragRaceStateMachine,
    InputDriver,
    ScreenCaptureManager,
    create_mss_instance,
    is_continue_button_green,
    is_green_button,
    is_needle_white,
    is_shift_green,
    is_traffic_light_present,
)

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]


# ============================================================================
# WINDOWS VIRTUAL KEYCODES & HARDWARE SCANCODES (FÜR ROBLOX DIRECTINPUT)
# ============================================================================

WIN_VK_W: int = 0x57        # Virtual Key 'W'
WIN_SCANCODE_W: int = 0x11  # Hardware-ScanCode 'W'
WIN_VK_E: int = 0x45        # Virtual Key 'E'
WIN_SCANCODE_E: int = 0x12  # Hardware-ScanCode 'E'

KEYEVENTF_SCANCODE: int = 0x0008
KEYEVENTF_KEYUP: int = 0x0002

WIN_VK_HOTKEYS: Dict[int, str] = {
    0x49: "i",  # VK_I -> shift_point
    0x4F: "o",  # VK_O -> Start / Pause
    0x50: "p",  # VK_P -> Beenden
    0x4D: "m",  # VK_M -> Modus wechseln
    0x31: "1",  # VK_1 -> shift_point
    0x32: "2",  # VK_2 -> needle_target_point
    0x33: "3",  # VK_3 -> traffic_light_point
    0x34: "4",  # VK_4 -> weiter_button
    0x35: "5",  # VK_5 -> next_stage_button
    0x36: "6",  # VK_6 -> rennen_button
}

if IS_WINDOWS:
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


# ============================================================================
# WINDOWS INPUT-TREIBER MIT SAFETY-RELEASE
# ============================================================================

class WindowsInputDriver(InputDriver):
    """
    Performanter Windows-Treiber für Tastatur (`w`, `e` über Hardware-ScanCodes)
    und Maus-Klicks, inklusive garantiertem Safety-Release.
    """

    def __init__(self) -> None:
        self.kb_ctrl = keyboard.Controller()
        self.mouse_ctrl = mouse.Controller()
        self._lock = threading.Lock()
        self._w_pressed: bool = False
        self._last_w_refresh: float = 0.0

        atexit.register(self.release_all)

    def _send_scancode(self, vk: int, scancode: int, char: str, is_down: bool) -> None:
        """Sendet ein Tastatur-Event per Win32 SendInput (ScanCode) oder pynput-Fallback."""
        if IS_WINDOWS:
            try:
                flags = KEYEVENTF_SCANCODE | (0 if is_down else KEYEVENTF_KEYUP)
                inp = INPUT(
                    type=1,
                    union=_INPUT_UNION(
                        ki=KEYBDINPUT(
                            wVk=vk,
                            wScan=scancode,
                            dwFlags=flags,
                            time=0,
                            dwExtraInfo=0,
                        )
                    ),
                )
                ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
                return
            except Exception:
                pass

        if is_down:
            self.kb_ctrl.press(char)
        else:
            self.kb_ctrl.release(char)

    def press_w(self) -> None:
        """Drückt und hält 'w' (mit periodischem Refresh für Roblox DirectInput)."""
        now = time.monotonic()
        with self._lock:
            if not self._w_pressed or (now - self._last_w_refresh) >= 0.18:
                self._send_scancode(WIN_VK_W, WIN_SCANCODE_W, "w", True)
                self._w_pressed = True
                self._last_w_refresh = now

    def release_w(self) -> None:
        """Lässt 'w' sofort los."""
        with self._lock:
            if self._w_pressed:
                self._send_scancode(WIN_VK_W, WIN_SCANCODE_W, "w", False)
                self._w_pressed = False

    def tap_e(self, hold_duration: float = 0.012) -> None:
        """Drückt 'e' kurz (Up-Shift) und lässt es wieder los."""
        self._send_scancode(WIN_VK_E, WIN_SCANCODE_E, "e", True)
        time.sleep(hold_duration)
        self._send_scancode(WIN_VK_E, WIN_SCANCODE_E, "e", False)
        with self._lock:
            if self._w_pressed:
                self._send_scancode(WIN_VK_W, WIN_SCANCODE_W, "w", True)
                self._last_w_refresh = time.monotonic()

    def click_at(self, logical_x: int, logical_y: int) -> None:
        """Bewegt den Mauszeiger zum Zielpunkt (inkl. Hover-Wiggle) und klickt links."""
        self.mouse_ctrl.position = (logical_x + 1, logical_y)
        time.sleep(0.010)
        self.mouse_ctrl.position = (logical_x, logical_y)
        time.sleep(0.015)
        self.mouse_ctrl.press(mouse.Button.left)
        time.sleep(0.025)
        self.mouse_ctrl.release(mouse.Button.left)

    def release_all(self) -> None:
        """Safety-Release: Lässt garantiert 'w' und 'e' sauber los."""
        with self._lock:
            self._w_pressed = False
        try:
            self._send_scancode(WIN_VK_W, WIN_SCANCODE_W, "w", False)
            self._send_scancode(WIN_VK_E, WIN_SCANCODE_E, "e", False)
        except Exception:
            pass
        try:
            self.kb_ctrl.release("w")
            self.kb_ctrl.release("e")
        except Exception:
            pass


# ============================================================================
# WINDOWS HAUPTSTEUERUNG & KALIBRIERUNG
# ============================================================================

class WindowsDragBotApp:
    """Verbindet `DragRaceStateMachine`, `ConfigManager` und Windows-Hotkeys."""

    CALIBRATION_MAP: Dict[str, Tuple[str, str]] = {
        "1": ("shift_point", "shift_point (Kleiner Kreis im Tacho für E)"),
        "i": ("shift_point", "shift_point (Kleiner Kreis im Tacho für E)"),
        "2": ("needle_target_point", "needle_target_point (Weiße Tachonadel zwischen 4 und 5)"),
        "3": ("traffic_light_point", "traffic_light_point (Ampel-Box oben in der Mitte)"),
        "4": ("weiter_button", "weiter_button (Grüner WEITER-Button in Popup 1 / Rennstatistik)"),
        "5": ("next_stage_button", "next_stage_button (Grüner NEXT STAGE-Button unten rechts in Popup 2 / Ladder)"),
        "6": ("rennen_button", "rennen_button (Grüner RENNEN-Button unten rechts neben CASH OUT in Popup 3)"),
    }

    def __init__(self) -> None:
        self.config = ConfigManager()
        self.capture = ScreenCaptureManager(retina_scale=1.0)
        self.driver = WindowsInputDriver()
        self.bot = DragRaceStateMachine(
            config=self.config,
            capture=self.capture,
            driver=self.driver,
        )
        self.mouse_ctrl = mouse.Controller()
        self._cmd_lock = threading.Lock()
        self._last_cmd_time: Dict[str, float] = {}

    def calibrate_point(self, key_cmd: str) -> None:
        """Speichert die aktuelle Mausposition für den gewählten Punkt in `config.json`."""
        if key_cmd not in self.CALIBRATION_MAP:
            return

        point_name, label = self.CALIBRATION_MAP[key_cmd]
        mx, my = self.mouse_ctrl.position
        px, py = int(round(mx)), int(round(my))

        bbox = self.capture.build_bbox(px, py, box_size=3)
        with create_mss_instance() as sct:
            rgb, _ = self.capture.sample_bbox_fast(sct, bbox)

        r, g, b = rgb
        hex_color = f"#{r:02X}{g:02X}{b:02X}"

        ref_rgb = rgb if point_name == "traffic_light_point" else None
        self.config.update_point_from_logical(
            point_name=point_name,
            logical_x=px,
            logical_y=py,
            logical_width=self.capture.logical_width,
            logical_height=self.capture.logical_height,
            ref_rgb=ref_rgb,
        )
        self.bot.refresh_bboxes()

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
                f"GRÜNER BUTTON erkannt! ({point_name})"
                if is_green_button(r, g, b)
                else f"Position gespeichert (Bereit für grünen Button: {point_name})"
            )

        print(
            f"\n[KALIBRIERUNG - Taste '{key_cmd}'] {label} gespeichert in config.json:\n"
            f"  -> Bildschirm-Koordinate (Pixel):      ({px}, {py})\n"
            f"  -> Gelesener RGB-Wert (3x3-Zentrum):   RGB({r}, {g}, {b}) | {hex_color} | {status}",
            flush=True,
        )

    def handle_command(self, cmd: str) -> bool:
        """Verarbeitet Hotkey-Befehle mit 180 ms Entprellung."""
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

    def console_fallback_loop(self) -> None:
        """Optionaler Konsolen-Fallback unter Windows (msvcrt)."""
        if msvcrt is None:
            return
        valid_cmds = ("o", "p", "m", "i", "1", "2", "3", "4", "5", "6")
        try:
            while self.bot.running:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch().lower()
                    if ch in valid_cmds:
                        if self.handle_command(ch):
                            break
                time.sleep(0.05)
        except Exception:
            pass

    def print_banner(self) -> None:
        """Gibt alle System-, Punkt- und Hotkey-Informationen im Terminal aus."""
        w, h = self.capture.logical_width, self.capture.logical_height

        print("=" * 80)
        print("  TAXI BOSS (ROBLOX) – VOLLAUTOMATISCHER RENN-BOT FÜR WINDOWS")
        print("=" * 80)
        print(f"  Bildschirm-Auflösung:     {w} x {h} Pixel (DPI-Aware 1:1)")
        print(f"  Konfigurationsdatei:      {self.config.config_path}")
        print(f"  Aktiver Modus:            {self.bot.mode.value} (Vollautomatik: Ampel -> Rennen -> 3-Stufen-Menü)")
        print(
            f"  Micro-Tap-Timings (W):    TAP_DURATION={TAP_DURATION * 1000:.0f} ms | "
            f"TAP_COOLDOWN={TAP_COOLDOWN * 1000:.0f} ms | MENU_CLICK_COOLDOWN={MENU_CLICK_COOLDOWN:.2f} s"
        )
        print("-" * 80)
        print("  Aktuelle Koordinaten (Pixel):")
        print(f"    [1 / i] shift_point (Kreis für E):         {self.bot.shift_pt}")
        print(f"    [2]     needle_target_point (Nadel 4–5):   {self.bot.needle_pt}")
        print(f"    [3]     traffic_light_point (Ampel oben):  {self.bot.light_pt}")
        print(f"    [4]     weiter_button (Popup 1 WEITER):    {self.bot.weiter_pt}")
        print(f"    [5]     next_stage_button (Popup 2 NEXT):  {self.bot.next_stage_pt}")
        print(f"    [6]     rennen_button (Popup 3 RENNEN):    {self.bot.rennen_pt}")
        print("-" * 80)
        print("  Steuerung per Hotkeys:")
        print("    [o]       Bot STARTEN / PAUSIEREN (inkl. Safety-Release für W & E)")
        print("    [p]       Skript SAUBER BEENDEN   (inkl. Safety-Release für W & E)")
        print("    [m]       Modus wechseln: FULL_AUTO (Renn-Bot) <-> SHIFT_ONLY (Nur Schalten)")
        print("    [1] / [i] Mausposition als shift_point (Kreis im Tacho für E) speichern")
        print("    [2]       Mausposition als needle_target_point (Tachonadel zwischen 4 & 5) speichern")
        print("    [3]       Mausposition als traffic_light_point (Ampel-Box oben) speichern")
        print("    [4]       Mausposition als weiter_button (Grüner WEITER-Button in Popup 1) speichern")
        print("    [5]       Mausposition als next_stage_button (Grüner NEXT STAGE-Button in Popup 2) speichern")
        print("    [6]       Mausposition als rennen_button (Grüner RENNEN-Button in Popup 3) speichern")
        print("=" * 80)
        print("  Bereit! Drücke [o], um den vollautomatischen Renn-Bot zu starten.\n", flush=True)

    def run(self) -> None:
        """Startet den Überwachungs-Thread und den globalen Hotkey-Listener."""
        self.print_banner()

        worker = threading.Thread(target=self.bot.run_loop, name="DragBotStateMachine", daemon=True)
        worker.start()

        if msvcrt is not None:
            console_worker = threading.Thread(
                target=self.console_fallback_loop, name="ConsoleHotkeyThread", daemon=True
            )
            console_worker.start()

        valid_chars = {"o", "p", "m", "i", "1", "2", "3", "4", "5", "6"}

        def on_press(key: keyboard.Key | keyboard.KeyCode | None) -> bool | None:
            try:
                cmd: str | None = None
                char = getattr(key, "char", None)
                if isinstance(char, str) and char.lower() in valid_chars:
                    cmd = char.lower()
                else:
                    vk = getattr(key, "vk", None)
                    if isinstance(vk, int) and vk in WIN_VK_HOTKEYS:
                        cmd = WIN_VK_HOTKEYS[vk]

                if cmd is not None:
                    if cmd in ("1", "2", "3", "4", "5", "6") and self.bot.active:
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
            if IS_WINDOWS:
                try:
                    ctypes.windll.winmm.timeEndPeriod(1)
                except Exception:
                    pass

        worker.join(timeout=1.0)
        self.driver.release_all()
        print("Skript sauber beendet (alle Tasten freigegeben).", flush=True)


if __name__ == "__main__":
    app = WindowsDragBotApp()
    app.run()
