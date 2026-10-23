#!/usr/bin/env python3
"""
core_utils.py – Plattformübergreifende Kernfunktionen (macOS & Windows)
=======================================================================

Dieses Modul wird sowohl von `auto_shifter_mac.py` als auch von
`auto_shifter_windows.py` importiert und bündelt:
1. Farb- & Pixel-Analyse (Shift-Grün, WEITER-Button-Grün, Tachonadel-Weiß, Ampel-Box)
2. Screen-Capture-Logik mit `mss` (inkl. macOS Retina 2x vs. Windows 1x Skalierung)
3. Konfigurations-Management (`config.json` laden/speichern + Screenshot-Standardwerte)
4. State-Machine-Grundlogik (`WAITING_AT_LIGHT`, `RACING`, `POST_RACE_POPUP`)

Referenz-Analyse der drei Screenshots (3420 x 2214 Retina / 1710 x 1107 Logisch):
---------------------------------------------------------------------------------
- Shift_Point (Schalt-Kreis im Tacho):
    Relativ: (0.5000, 0.7787) | Logisch: (855, 862) | Retina: (1710, 1724)
- Needle_Target_Point (Weiße Tachonadel / Segment zwischen 4 und 5 im blauen Bereich):
    Relativ: (0.5120, 0.7642) | Logisch: (876, 846) | Retina: (1751, 1692)
- Traffic_Light_Point (Dunkle Ampel-Box oben in der Mitte):
    Relativ: (0.5000, 0.2078) | Logisch: (855, 230) | Retina: (1710, 460)
- Continue_Button_Point (Grüner "WEITER"-Button im Statistik-Popup):
    Relativ: (0.4971, 0.7480) | Logisch: (850, 828) | Retina: (1700, 1656)
"""

from __future__ import annotations

import enum
import json
import os
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import mss


# ============================================================================
# PFADE & STANDARD-KONFIGURATION
# ============================================================================

PROJECT_DIR: Path = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH: Path = PROJECT_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "points": {
        "shift_point": {
            "rel_x": 0.5000,
            "rel_y": 0.7787,
            "description": "Kleiner Kreis im Tacho für Up-Shift [E]",
        },
        "needle_target_point": {
            "rel_x": 0.5120,
            "rel_y": 0.7642,
            "description": "Zielpunkt für weiße Tachonadel zwischen 4 und 5 (Blaue Zone)",
        },
        "traffic_light_point": {
            "rel_x": 0.5000,
            "rel_y": 0.2078,
            "ref_rgb": [54, 54, 58],
            "description": "Punkt auf der dunklen Ampel-Box oben in der Mitte",
        },
        "continue_button_point": {
            "rel_x": 0.4971,
            "rel_y": 0.7480,
            "description": "Punkt auf dem grünen WEITER-Button im End-Popup",
        },
    },
    "thresholds": {
        "shift_green_min": 95,
        "shift_green_ratio": 1.22,
        "continue_green_min": 115,
        "continue_green_ratio_r": 1.30,
        "continue_green_ratio_b": 1.60,
        "needle_white_min": 195,
        "needle_white_max_diff": 35,
        "traffic_light_tolerance": 24,
    },
    "timings": {
        "key_hold_duration": 0.012,
        "min_shift_cooldown": 0.085,
        "max_shift_cooldown": 0.220,
        "needle_pulse_hold": 0.022,
        "needle_release_pause": 0.012,
        "popup_click_interval": 0.550,
        "scan_interval": 0.0,
    },
}


# ============================================================================
# 1. FARB- & PIXEL-ANALYSE MIT TOLERANZ / SCHWELLENWERTEN
# ============================================================================

RGB = Tuple[int, int, int]


def is_shift_green(
    r: int,
    g: int,
    b: int,
    min_g: int = 95,
    dominance_ratio: float = 1.22,
) -> bool:
    """
    Prüft, ob der Schalt-Indikator-Kreis im Tacho grün leuchtet.
    Sensibler Schwellenwert (G > 95, G > R*1.22, G > B*1.22), damit bereits der
    allererste Übergangs-Frame im 1. und 2. Gang sofort erkannt wird.
    """
    return (
        g > min_g
        and g > (r * dominance_ratio)
        and g > (b * dominance_ratio)
    )


def is_continue_button_green(
    r: int,
    g: int,
    b: int,
    min_g: int = 115,
    ratio_r: float = 1.30,
    ratio_b: float = 1.60,
) -> bool:
    """
    Prüft, ob ein Pixel die grüne Farbe des 'WEITER'-Buttons im Statistik-Popup hat.
    Im Referenzbild (13.58.00) hat der Button RGB(82, 146, 50).
    """
    return (
        g >= min_g
        and r >= 45
        and b <= 115
        and g > (r * ratio_r)
        and g > (b * ratio_b)
    )


def is_needle_white(
    r: int,
    g: int,
    b: int,
    min_val: int = 195,
    max_channel_diff: int = 35,
) -> bool:
    """
    Prüft, ob die weiße Tachonadel (bzw. der weiße Drehzahl-Balken) am Zielpunkt
    zwischen 4 und 5 angekommen ist.
    Im Referenzbild hat die Nadel / der Balken RGB(255, 255, 255), während der
    Hintergrund dunkelgrau/dunkelblau RGB(50, 52, 63) bzw. RGB(27, 33, 69) ist.
    """
    if r < min_val or g < min_val or b < min_val:
        return False
    return (max(r, g, b) - min(r, g, b)) <= max_channel_diff


def is_traffic_light_present(
    r: int,
    g: int,
    b: int,
    ref_rgb: Optional[RGB] = None,
    tolerance: int = 24,
) -> bool:
    """
    Prüft, ob die dunkle Ampel-Box oben in der Mitte sichtbar ist (Phase 1: WAITING_AT_LIGHT).
    Berücksichtigt:
      1. Das dunkle Anthrazit-Gehäuse / unbeleuchtete Lampen der Ampel-Box:
         im Screenshot 13.53.03 ca. RGB(43..80, 52..62, 55..73).
      2. Eine aktiv leuchtende rote/gelbe Ampel-Lampe: RGB(197, 66, 61).
      3. Einen vom Nutzer per Hotkey kalibrierten Referenz-Farbwert (`ref_rgb`).
    Sobald die Ampel verschwindet (Screenshot 09.32.46), erscheint dort entweder
    der helle Horizont/Himmel RGB(168, 158, 165) oder das weiße 'GO!' RGB(235, 235, 235).
    """
    # Heller Himmel oder weißes "GO!" -> Ampel ist definitiv NICHT mehr da
    if (r > 130 and g > 110 and b > 110) or (g > 95 and b > 95):
        return False

    # Aktive rote Ampel-Lampe (z. B. RGB(197, 66, 61))
    if r > 150 and g < 95 and b < 90:
        return True

    # Dunkles Ampel-Gehäuse bzw. unbeleuchtete Lampe (Screenshot 13.53.03)
    if 38 <= r <= 95 and 46 <= g <= 72 and 50 <= b <= 82:
        if (max(r, g, b) - min(r, g, b)) <= 30:
            return True

    # Vergleich mit kalibriertem Referenzwert (falls vorhanden)
    if ref_rgb is not None:
        rr, rg, rb = ref_rgb
        if (
            abs(r - rr) <= tolerance
            and abs(g - rg) <= tolerance
            and abs(b - rb) <= tolerance
        ):
            return True

    return False


# ============================================================================
# 2. KONFIGURATIONS-MANAGEMENT (config.json)
# ============================================================================

class ConfigManager:
    """Lädt, verwaltet und speichert Koordinaten & Timings in `config.json`."""

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.config_path: Path = config_path
        self.data: Dict[str, Any] = self._load_or_create()

    def _load_or_create(self) -> Dict[str, Any]:
        """Lädt die `config.json` oder legt sie mit den Screenshot-Standardwerten an."""
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for section in ("points", "thresholds", "timings"):
                    if isinstance(loaded.get(section), dict):
                        if section == "points":
                            for pt_name, pt_val in loaded["points"].items():
                                if pt_name in merged["points"] and isinstance(pt_val, dict):
                                    merged["points"][pt_name].update(pt_val)
                                else:
                                    merged["points"][pt_name] = pt_val
                        else:
                            merged[section].update(loaded[section])
            except Exception as exc:
                print(f"[WARNUNG] Konnte {self.config_path.name} nicht lesen ({exc}), nutze Standardwerte.")
        else:
            self.save(merged)
        return merged

    def save(self, data: Optional[Dict[str, Any]] = None) -> None:
        """Speichert die aktuelle Konfiguration formatiert in `config.json`."""
        if data is not None:
            self.data = data
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            print(f"[WARNUNG] Konnte {self.config_path.name} nicht speichern: {exc}")

    def get_point_logical(
        self, point_name: str, logical_width: int, logical_height: int
    ) -> Tuple[int, int]:
        """
        Gibt die logische (X, Y)-Koordinate für einen benannten Punkt zurück.
        Nutzt die gespeicherten relativen Koordinaten (`rel_x`, `rel_y`), damit
        die Position auf jeder Bildschirmauflösung passt.
        """
        pt = self.data["points"].get(point_name, DEFAULT_CONFIG["points"][point_name])
        rel_x = float(pt.get("rel_x", 0.5))
        rel_y = float(pt.get("rel_y", 0.5))
        return (
            int(round(logical_width * rel_x)),
            int(round(logical_height * rel_y)),
        )

    def update_point_from_logical(
        self,
        point_name: str,
        logical_x: int,
        logical_y: int,
        logical_width: int,
        logical_height: int,
        ref_rgb: Optional[RGB] = None,
    ) -> None:
        """Aktualisiert einen Kalibrierungspunkt und speichert ihn sofort in `config.json`."""
        rel_x = round(logical_x / max(1, logical_width), 6)
        rel_y = round(logical_y / max(1, logical_height), 6)
        if point_name not in self.data["points"]:
            self.data["points"][point_name] = {}
        self.data["points"][point_name]["rel_x"] = rel_x
        self.data["points"][point_name]["rel_y"] = rel_y
        self.data["points"][point_name]["last_logical"] = [logical_x, logical_y]
        if ref_rgb is not None:
            self.data["points"][point_name]["ref_rgb"] = list(ref_rgb)
        self.save()

    def get_traffic_light_ref_rgb(self) -> Optional[RGB]:
        """Gibt den gespeicherten Referenz-RGB-Wert der Ampel zurück."""
        pt = self.data["points"].get("traffic_light_point", {})
        ref = pt.get("ref_rgb")
        if isinstance(ref, (list, tuple)) and len(ref) == 3:
            return (int(ref[0]), int(ref[1]), int(ref[2]))
        return (54, 54, 58)


# ============================================================================
# 3. SCREEN-CAPTURE-LOGIK (mss + Retina/DPI-Skalierung)
# ============================================================================

def create_mss_instance() -> Any:
    """Erstellt eine MSS-Instanz kompatibel mit mss 9.x und mss 10.x+ (ohne DeprecationWarning)."""
    mss_cls = getattr(mss, "MSS", None) or getattr(mss, "mss")
    return mss_cls()


class ScreenCaptureManager:
    """
    Kapselt `mss`-Zugriffe und rechnet logische Maus-Koordinaten (`pynput`)
    sauber in `mss`-Koordinaten sowie physische Retina-Pixel (2x auf macOS, 1x auf Windows) um.
    """

    def __init__(
        self,
        logical_width: Optional[int] = None,
        logical_height: Optional[int] = None,
        retina_scale: float = 1.0,
    ) -> None:
        with create_mss_instance() as sct:
            primary_mon = sct.monitors[1]
            self.mss_left: int = int(primary_mon["left"])
            self.mss_top: int = int(primary_mon["top"])
            self.mss_width: int = int(primary_mon["width"])
            self.mss_height: int = int(primary_mon["height"])

        self.logical_width: int = logical_width or self.mss_width
        self.logical_height: int = logical_height or self.mss_height
        self.retina_scale: float = retina_scale

        # Faktor zwischen logischen pynput-Koordinaten und mss-Koordinaten
        self.mss_coord_scale_x: float = (
            self.mss_width / self.logical_width if self.logical_width else 1.0
        )
        self.mss_coord_scale_y: float = (
            self.mss_height / self.logical_height if self.logical_height else 1.0
        )

    def build_bbox(self, logical_x: int, logical_y: int, box_size: int = 3) -> Dict[str, int]:
        """Erstellt eine `mss`-Bounding-Box der Größe `box_size x box_size` um `(logical_x, logical_y)`."""
        mss_x = self.mss_left + int(round(logical_x * self.mss_coord_scale_x))
        mss_y = self.mss_top + int(round(logical_y * self.mss_coord_scale_y))
        half = box_size // 2
        return {
            "left": mss_x - half,
            "top": mss_y - half,
            "width": box_size,
            "height": box_size,
        }

    def to_physical_coords(self, logical_x: int, logical_y: int) -> Tuple[int, int]:
        """Rechnet logische Koordinaten in physische (Retina-)Pixel um."""
        return (
            int(round(logical_x * self.retina_scale)),
            int(round(logical_y * self.retina_scale)),
        )

    @staticmethod
    def sample_bbox_fast(
        sct: Any,
        bbox: Dict[str, int],
        predicate: Optional[Callable[[int, int, int], bool]] = None,
    ) -> Tuple[RGB, bool]:
        """
        Liest einen kleinen Pixelbereich (`bbox`) direkt aus dem BGRA-Rohpuffer aus.
        Gibt zurück:
          - `rgb`: Den (R, G, B)-Wert des ersten treffenden Pixels bzw. des Zentrumspixels
          - `matched`: True, falls mindestens ein Pixel im Bereich `predicate(r, g, b)` erfüllt
        """
        sct_img = sct.grab(bbox)
        raw: bytes = sct_img.raw

        if predicate is not None:
            for offset in range(0, len(raw), 4):
                b = raw[offset]
                g = raw[offset + 1]
                r = raw[offset + 2]
                if predicate(r, g, b):
                    return (r, g, b), True

        width: int = sct_img.width
        height: int = sct_img.height
        center_offset = ((height // 2) * width + (width // 2)) * 4
        center_rgb: RGB = (
            raw[center_offset + 2],
            raw[center_offset + 1],
            raw[center_offset],
        )
        return center_rgb, False


# ============================================================================
# 4. STATE-MACHINE-GRUNDLOGIK & TREIBER-INTERFACE
# ============================================================================

class BotState(enum.Enum):
    """Zustände des vollautomatischen Drag-Renn-Bots."""
    WAITING_AT_LIGHT = "WAITING_AT_LIGHT"  # Phase 1: An der Ampel Drehzahl im blauen Bereich halten
    RACING = "RACING"                      # Phase 2: Vollgas (W halten) + Auto-Shift (E bei Grün)
    POST_RACE_POPUP = "POST_RACE_POPUP"    # Phase 3: Rennende, W loslassen & auf WEITER klicken


class BotMode(enum.Enum):
    """Betriebsmodus des Bots."""
    FULL_AUTO = "FULL_AUTO"    # Vollautomatischer Renn-Bot (Phase 1 -> Phase 2 -> Phase 3)
    SHIFT_ONLY = "SHIFT_ONLY"  # Klassischer Auto-Shifter (nur E drücken bei grünem Schaltpunkt)


class InputDriver(ABC):
    """
    Abstrakte Schnittstelle für plattformspezifische Tastatur- und Maus-Eingaben.
    Wird in `auto_shifter_mac.py` (Quartz/pynput) und `auto_shifter_windows.py`
    (Win32 SendInput/pynput) implementiert.
    """

    @abstractmethod
    def press_w(self) -> None:
        """Drückt und hält die Taste 'w' (Gas geben), falls sie noch nicht gedrückt ist."""

    @abstractmethod
    def release_w(self) -> None:
        """Lässt die Taste 'w' sofort los."""

    @abstractmethod
    def tap_e(self, hold_duration: float = 0.012) -> None:
        """Drückt die Taste 'e' kurz (Up-Shift) und lässt sie wieder los."""

    @abstractmethod
    def click_at(self, logical_x: int, logical_y: int) -> None:
        """Bewegt die Maus zu `(logical_x, logical_y)` und führt einen Linksklick aus."""

    @abstractmethod
    def release_all(self) -> None:
        """Safety-Release: Lässt garantiert 'w' und 'e' sauber los."""


class DragRaceStateMachine:
    """
    Plattformunabhängige State-Machine für den Taxi Boss Drag-Race-Bot:
      - Phase 1 (`WAITING_AT_LIGHT`):
        Hält den weißen Tachozeiger durch schnelle PWM-/Feathering-Regelung von `w`
        am `Needle_Target_Point` im blauen Bereich zwischen 4 und 5.
        Wechselt sofort zu `RACING`, sobald die Ampel an `Traffic_Light_Point` verschwindet.
      - Phase 2 (`RACING`):
        Hält `w` durchgehend gedrückt und schaltet mit `e` bei grünem `Shift_Point`
        (inkl. dynamischer 85–220 ms Entprellung für die schnellen ersten Gänge).
        Überwacht parallel den `Continue_Button_Point` für das Rennende.
      - Phase 3 (`POST_RACE_POPUP`):
        Lässt `w` sofort los, klickt auf den grünen `WEITER`-Button und wechselt
        zurück zu `WAITING_AT_LIGHT`, sobald die Ampel für das nächste Rennen erscheint.
    """

    def __init__(
        self,
        config: ConfigManager,
        capture: ScreenCaptureManager,
        driver: InputDriver,
    ) -> None:
        self.config = config
        self.capture = capture
        self.driver = driver

        self._lock = threading.Lock()
        self.active: bool = False
        self.running: bool = True
        self.mode: BotMode = BotMode.FULL_AUTO
        self.state: BotState = BotState.WAITING_AT_LIGHT

        self.shift_count: int = 0
        self.race_count: int = 0

        # Interne Statuszähler zur Entprellung von Phasenübergängen
        self._light_seen_in_waiting: bool = False
        self._light_gone_streak: int = 0
        self._light_present_streak: int = 0
        self._popup_green_streak: int = 0
        self._race_frame_counter: int = 0
        self._last_popup_click_time: float = 0.0
        self._last_waiting_pulse_time: float = 0.0

        self.refresh_bboxes()

    def refresh_bboxes(self) -> None:
        """Berechnet alle Überwachungs-Bounding-Boxen anhand der aktuellen Konfiguration neu."""
        lw = self.capture.logical_width
        lh = self.capture.logical_height

        with self._lock:
            self.shift_pt = self.config.get_point_logical("shift_point", lw, lh)
            self.needle_pt = self.config.get_point_logical("needle_target_point", lw, lh)
            self.light_pt = self.config.get_point_logical("traffic_light_point", lw, lh)
            self.continue_pt = self.config.get_point_logical("continue_button_point", lw, lh)
            self.light_ref_rgb = self.config.get_traffic_light_ref_rgb()

            self.shift_bbox = self.capture.build_bbox(*self.shift_pt, box_size=3)
            # 5x5 Box für die Tachonadel, damit der weiße Zeiger / Balken sicher erfasst wird
            self.needle_bbox = self.capture.build_bbox(*self.needle_pt, box_size=5)
            self.light_bbox = self.capture.build_bbox(*self.light_pt, box_size=5)
            self.continue_bbox = self.capture.build_bbox(*self.continue_pt, box_size=5)

    def transition_to(self, new_state: BotState, reason: str = "") -> None:
        """Führt einen sauberen Zustandswechsel mit Terminal-Log aus."""
        self.state = new_state
        self._light_gone_streak = 0
        self._light_present_streak = 0
        self._popup_green_streak = 0
        self._race_frame_counter = 0

        timestamp = time.strftime("%H:%M:%S")
        suffix = f" ({reason})" if reason else ""

        if new_state == BotState.WAITING_AT_LIGHT:
            self.driver.release_w()
            self._light_seen_in_waiting = False
            print(
                f"\n[{timestamp}] [PHASE 1 -> WAITING_AT_LIGHT]{suffix}\n"
                f"  -> Halte Drehzahl im blauen Bereich (4–5) bei {self.needle_pt} & warte auf Ampel-Start ...",
                flush=True,
            )
        elif new_state == BotState.RACING:
            self.shift_count = 0
            self.driver.press_w()
            print(
                f"\n[{timestamp}] [PHASE 2 -> RACING]{suffix}\n"
                f"  -> GO! Vollgas [W] gehalten + Auto-Shift [E] aktiv bei {self.shift_pt}!",
                flush=True,
            )
        elif new_state == BotState.POST_RACE_POPUP:
            self.driver.release_all()
            self.race_count += 1
            print(
                f"\n[{timestamp}] [PHASE 3 -> POST_RACE_POPUP]{suffix}\n"
                f"  -> Rennen #{self.race_count} beendet! [W] losgelassen -> Klicke auf WEITER bei {self.continue_pt} ...",
                flush=True,
            )

    def detect_initial_state(self, sct: Any) -> BotState:
        """Erkennt beim Aktivieren des Bots automatisch die aktuelle Spielphase."""
        _, popup_green = self.capture.sample_bbox_fast(
            sct, self.continue_bbox, is_continue_button_green
        )
        if popup_green:
            return BotState.POST_RACE_POPUP

        _, light_present = self.capture.sample_bbox_fast(
            sct,
            self.light_bbox,
            lambda r, g, b: is_traffic_light_present(r, g, b, self.light_ref_rgb),
        )
        if light_present:
            return BotState.WAITING_AT_LIGHT

        # Standardmäßig in Phase 1 starten (wartet auf Ampel bzw. hält Drehzahl)
        return BotState.WAITING_AT_LIGHT

    def set_active(self, active: bool) -> None:
        """Startet oder pausiert den Bot inkl. Safety-Release für alle Tasten."""
        with self._lock:
            self.active = active
        if not active:
            self.driver.release_all()
            print("\n[STATUS - o] Bot PAUSIERT (Safety-Release: 'w' und 'e' losgelassen).", flush=True)
        else:
            with create_mss_instance() as sct:
                initial_state = self.detect_initial_state(sct)
            print(
                f"\n[STATUS - o] Bot AKTIVIERT (Modus: {self.mode.value})!",
                flush=True,
            )
            if self.mode == BotMode.FULL_AUTO:
                self.transition_to(initial_state, reason="Bot gestartet")
                if initial_state == BotState.WAITING_AT_LIGHT:
                    # Prüfen, ob Ampel bereits sichtbar ist
                    with create_mss_instance() as sct:
                        _, is_lit = self.capture.sample_bbox_fast(
                            sct,
                            self.light_bbox,
                            lambda r, g, b: is_traffic_light_present(r, g, b, self.light_ref_rgb),
                        )
                        self._light_seen_in_waiting = is_lit

    def toggle_active(self) -> None:
        """Schaltet zwischen Aktiv und Pausiert um."""
        self.set_active(not self.active)

    def toggle_mode(self) -> None:
        """Schaltet zwischen Vollautomatik-Bot (`FULL_AUTO`) und Nur-Shifter (`SHIFT_ONLY`) um."""
        self.driver.release_all()
        with self._lock:
            self.mode = (
                BotMode.SHIFT_ONLY if self.mode == BotMode.FULL_AUTO else BotMode.FULL_AUTO
            )
            new_mode = self.mode
        print(
            f"\n[MODUS - m] Gewechselt auf: {new_mode.value} "
            f"({'Vollautomatischer Renn-Bot (Ampel + W + E + Weiter)' if new_mode == BotMode.FULL_AUTO else 'Nur Auto-Shifter (E bei Grün)'})",
            flush=True,
        )

    def stop(self) -> None:
        """Beendet die State-Machine und führt sofort einen Safety-Release für W und E aus."""
        with self._lock:
            self.running = False
            self.active = False
        self.driver.release_all()

    def _step_waiting_at_light(self, sct: Any) -> None:
        """
        Phase 1 (`WAITING_AT_LIGHT`):
        1. Prüft die Ampel oben in der Mitte (`Traffic_Light_Point`).
           Sobald die Ampel sichtbar war und dann verschwindet (GO!), sofort Wechsel in `RACING`.
        2. Regelt die Drehzahl (`w`) anhand von `Needle_Target_Point` zwischen 4 und 5:
           - Ist der Punkt weiß (`is_needle_white` == True): Nadel hat den Zielpunkt
             zwischen 4 und 5 erreicht -> SOFORT `release_w()`, damit sie nicht überschießt.
           - Ist der Punkt nicht weiß (`is_needle_white` == False): Nadel ist unterhalb
             des Zielpunkts -> `w` kurz antippen/halten (Feathering/PWM), um sie im
             blauen Bereich zu halten.
        """
        # 1. Ampel-Status prüfen
        _, light_present = self.capture.sample_bbox_fast(
            sct,
            self.light_bbox,
            lambda r, g, b: is_traffic_light_present(r, g, b, self.light_ref_rgb),
        )

        if light_present:
            if not self._light_seen_in_waiting:
                self._light_seen_in_waiting = True
                print("  [Ampel erkannt] Starte Drehzahl-Regelung für blauen Bereich (4–5) ...", flush=True)
            self._light_gone_streak = 0
        else:
            if self._light_seen_in_waiting:
                self._light_gone_streak += 1
                # 2 aufeinanderfolgende Frames ohne Ampel-Box -> GO!
                if self._light_gone_streak >= 2:
                    self.transition_to(BotState.RACING, reason="Ampel verschwunden / GO!")
                    return
            else:
                # Falls das Popup doch noch offen ist
                _, popup_green = self.capture.sample_bbox_fast(
                    sct, self.continue_bbox, is_continue_button_green
                )
                if popup_green:
                    self._popup_green_streak += 1
                    if self._popup_green_streak >= 3:
                        self.transition_to(BotState.POST_RACE_POPUP, reason="WEITER-Button erkannt")
                        return
                else:
                    self._popup_green_streak = 0

        # 2. Drehzahl-Regelung am Needle_Target_Point (nur wenn Ampel sichtbar ist)
        if not self._light_seen_in_waiting:
            self.driver.release_w()
            time.sleep(0.015)
            return

        _, needle_at_target = self.capture.sample_bbox_fast(
            sct, self.needle_bbox, is_needle_white
        )

        timings = self.config.data["timings"]
        pulse_hold = float(timings.get("needle_pulse_hold", 0.022))
        release_pause = float(timings.get("needle_release_pause", 0.012))

        if needle_at_target:
            # Weiße Nadel / weißes Segment am Zielpunkt zwischen 4 und 5 erkannt -> sofort W loslassen!
            self.driver.release_w()
            time.sleep(release_pause)
        else:
            # Zeiger ist unterhalb des Zielpunkts -> kurzer Gas-Stoß (PWM / Feathering)
            self.driver.press_w()
            time.sleep(pulse_hold)
            # Direkt nochmal prüfen, ob die Nadel während des Gas-Stoßes den Punkt erreicht hat
            _, reached_now = self.capture.sample_bbox_fast(
                sct, self.needle_bbox, is_needle_white
            )
            if reached_now:
                self.driver.release_w()

    def _step_racing(self, sct: Any) -> None:
        """
        Phase 2 (`RACING`):
        - Hält `w` durchgehend gedrückt (im `FULL_AUTO`-Modus).
        - Überwacht `Shift_Point` mit ~120 Hz und drückt sofort `e`, sobald Grün erkannt wird.
        - Prüft alle 6 Frames (~50 ms) nebenbei, ob das End-Popup (`Continue_Button_Point`)
          oder bereits die nächste Ampel erschienen ist.
        """
        if self.mode == BotMode.FULL_AUTO:
            self.driver.press_w()

        timings = self.config.data["timings"]
        hold_dur = float(timings.get("key_hold_duration", 0.012))
        min_cd = float(timings.get("min_shift_cooldown", 0.085))
        max_cd = float(timings.get("max_shift_cooldown", 0.220))

        # 1. Höchste Priorität: Schalt-Indikator-Kreis prüfen
        rgb, is_shift_ready = self.capture.sample_bbox_fast(
            sct, self.shift_bbox, is_shift_green
        )

        if is_shift_ready:
            shift_start = time.perf_counter()
            self.driver.tap_e(hold_duration=hold_dur)
            # Sicherstellen, dass W nach dem E-Tap im Vollautomatik-Modus gehalten bleibt
            if self.mode == BotMode.FULL_AUTO:
                self.driver.press_w()

            self.shift_count += 1
            r, g, b = rgb
            timestamp = time.strftime("%H:%M:%S")
            print(
                f"[{timestamp}] SCHALTEN #{self.shift_count:02d} -> Taste 'e' gedrückt! "
                f"(RGB({r}, {g}, {b}) bei {self.shift_pt})",
                flush=True,
            )

            # Kurze Mindest-Entprellung (85 ms), damit Gang 1 -> 2 nicht doppelt schaltet
            elapsed = time.perf_counter() - shift_start
            if elapsed < min_cd:
                time.sleep(min_cd - elapsed)

            # Dynamisches Warten bis der Kreis wieder grau ist (max. 220 ms)
            while self.running and self.active:
                if (time.perf_counter() - shift_start) >= max_cd:
                    break
                _, still_green = self.capture.sample_bbox_fast(
                    sct, self.shift_bbox, is_shift_green
                )
                if not still_green:
                    break
            return

        # 2. Im FULL_AUTO-Modus alle 6 Frames (~50 ms) prüfen, ob das End-Popup da ist
        if self.mode == BotMode.FULL_AUTO:
            self._race_frame_counter += 1
            if (self._race_frame_counter % 6) == 0:
                _, popup_green = self.capture.sample_bbox_fast(
                    sct, self.continue_bbox, is_continue_button_green
                )
                if popup_green:
                    self._popup_green_streak += 1
                    if self._popup_green_streak >= 3:
                        self.transition_to(
                            BotState.POST_RACE_POPUP,
                            reason="Grüner WEITER-Button im Popup erkannt",
                        )
                        return
                else:
                    self._popup_green_streak = 0

                # Falls der Spieler manuell neu gestartet hat und die Ampel wieder da ist
                _, light_present = self.capture.sample_bbox_fast(
                    sct,
                    self.light_bbox,
                    lambda r, g, b: is_traffic_light_present(r, g, b, self.light_ref_rgb),
                )
                if light_present:
                    self._light_present_streak += 1
                    if self._light_present_streak >= 5:
                        self.transition_to(
                            BotState.WAITING_AT_LIGHT,
                            reason="Ampel oben in der Mitte erkannt",
                        )
                        return
                else:
                    self._light_present_streak = 0

    def _step_post_race_popup(self, sct: Any) -> None:
        """
        Phase 3 (`POST_RACE_POPUP`):
        - Stellt sicher, dass `w` losgelassen ist.
        - Klickt auf den grünen `WEITER`-Button (`Continue_Button_Point`), solange er sichtbar ist.
        - Sobald die Ampel (`Traffic_Light_Point`) für das nächste Rennen erscheint,
          wechselt der Bot zurück in Phase 1 (`WAITING_AT_LIGHT`).
        """
        self.driver.release_w()

        # 1. Prüfen, ob die Ampel für das nächste Rennen bereits erschienen ist
        _, light_present = self.capture.sample_bbox_fast(
            sct,
            self.light_bbox,
            lambda r, g, b: is_traffic_light_present(r, g, b, self.light_ref_rgb),
        )
        if light_present:
            self._light_present_streak += 1
            if self._light_present_streak >= 2:
                self._light_seen_in_waiting = True
                self.transition_to(
                    BotState.WAITING_AT_LIGHT,
                    reason="Nächstes Rennen gestartet – Ampel erkannt",
                )
                return
        else:
            self._light_present_streak = 0

        # 2. Prüfen, ob der grüne WEITER-Button (noch) sichtbar ist, und darauf klicken
        _, popup_green = self.capture.sample_bbox_fast(
            sct, self.continue_bbox, is_continue_button_green
        )
        now = time.monotonic()
        click_interval = float(self.config.data["timings"].get("popup_click_interval", 0.55))

        if popup_green and (now - self._last_popup_click_time) >= click_interval:
            self._last_popup_click_time = now
            cx, cy = self.continue_pt
            timestamp = time.strftime("%H:%M:%S")
            print(
                f"[{timestamp}] [POPUP] Klicke auf grünen WEITER-Button bei ({cx}, {cy}) ...",
                flush=True,
            )
            self.driver.click_at(cx, cy)

        time.sleep(0.04)

    def run_loop(self) -> None:
        """Hauptschleife des Bots (wird im Hintergrund-Thread ausgeführt)."""
        with create_mss_instance() as sct:
            while self.running:
                if not self.active:
                    time.sleep(0.025)
                    continue

                try:
                    if self.mode == BotMode.SHIFT_ONLY:
                        self._step_racing(sct)
                    else:
                        if self.state == BotState.WAITING_AT_LIGHT:
                            self._step_waiting_at_light(sct)
                        elif self.state == BotState.RACING:
                            self._step_racing(sct)
                        elif self.state == BotState.POST_RACE_POPUP:
                            self._step_post_race_popup(sct)
                except Exception as exc:
                    print(f"[WARNUNG] Fehler in State-Machine-Schleife: {exc}", flush=True)
                    time.sleep(0.05)

        # Garantierter Safety-Release beim Verlassen der Schleife
        self.driver.release_all()
