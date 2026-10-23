#!/usr/bin/env python3
"""
core_utils.py – Plattformübergreifende Kernfunktionen (macOS & Windows)
=======================================================================

Dieses Modul wird sowohl von `auto_shifter_mac.py` als auch von
`auto_shifter_windows.py` importiert und bündelt:
1. Farb- & Pixel-Analyse (Shift-Grün, Menü-Button-Grün, Tachonadel-Weiß, Ampel-Box)
2. Screen-Capture-Logik mit `mss` (inkl. macOS Retina 2x vs. Windows 1x Skalierung)
3. Konfigurations-Management (`config.json` laden/speichern + Screenshot-Standardwerte)
4. State-Machine-Grundlogik (`WAITING_AT_LIGHT`, `RACING`, `POST_RACE_POPUP`)

Referenz-Analyse der Screenshots (3420 x 2214 Retina / 1710 x 1107 Logisch):
----------------------------------------------------------------------------
- shift_point (Schalt-Kreis im Tacho):
    Relativ: (0.500000, 0.778681) | Logisch: (855, 862) | Retina: (1710, 1724)
- needle_target_point (Weiße Tachonadel / Segment zwischen 4 und 5 im blauen Bereich):
    Relativ: (0.512000, 0.764200) | Logisch: (876, 846) | Retina: (1751, 1692)
- traffic_light_point (Dunkle Ampel-Box oben in der Mitte):
    Relativ: (0.500000, 0.207769) | Logisch: (855, 230) | Retina: (1710, 460)
- weiter_button (Grüner "WEITER"-Button im Statistik-Popup, Menü 1/3):
    Relativ: (0.500000, 0.760614) | Logisch: (855, 842) | Retina: (1710, 1684)
- next_stage_button (Grüner "NEXT STAGE"-Button im Taxi City Ladder Popup, Menü 2/3):
    Relativ: (0.715205, 0.929539) | Logisch: (1223, 1029) | Retina: (2446, 2058)
- rennen_button (Grüner "RENNEN"-Button im VS-Popup nach Cutscene, Menü 3/3):
    Relativ: (0.697661, 0.837398) | Logisch: (1193, 927) | Retina: (2386, 1854)
"""

from __future__ import annotations

import enum
import json
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import mss


# ============================================================================
# EINSTELLBARE MICRO-TAP- & MENÜ-KONSTANTEN (FÜR FEINTUNING)
# ============================================================================

TAP_DURATION: float = 0.018        # Wie lange 'w' pro Micro-Tap gedrückt wird (z. B. 0.015 bis 0.025s)
TAP_COOLDOWN: float = 0.020        # Pause zwischen den Micro-Taps nach release('w')
MENU_CLICK_COOLDOWN: float = 0.50  # Cooldown (0.5s) nach jedem Klick auf einen grünen Menü-Button


# ============================================================================
# PFADE & STANDARD-KONFIGURATION
# ============================================================================

PROJECT_DIR: Path = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH: Path = PROJECT_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "points": {
        "shift_point": {
            "rel_x": 0.500000,
            "rel_y": 0.778681,
            "last_logical": [855, 862],
            "description": "Kleiner Kreis im Tacho für Up-Shift [E]",
        },
        "needle_target_point": {
            "rel_x": 0.512000,
            "rel_y": 0.764200,
            "last_logical": [876, 846],
            "description": "Zielpunkt für weiße Tachonadel zwischen 4 und 5 (Blaue Zone)",
        },
        "traffic_light_point": {
            "rel_x": 0.500000,
            "rel_y": 0.207769,
            "ref_rgb": [54, 54, 58],
            "last_logical": [855, 230],
            "description": "Punkt auf der dunklen Ampel-Box oben in der Mitte",
        },
        "weiter_button": {
            "rel_x": 0.500000,
            "rel_y": 0.760614,
            "last_logical": [855, 842],
            "description": "Menü 1/3: Grüner WEITER-Button im Rennstatistik-Popup",
        },
        "next_stage_button": {
            "rel_x": 0.715205,
            "rel_y": 0.929539,
            "last_logical": [1223, 1029],
            "description": "Menü 2/3: Grüner NEXT STAGE-Button im Taxi City Ladder-Popup",
        },
        "rennen_button": {
            "rel_x": 0.697661,
            "rel_y": 0.837398,
            "last_logical": [1193, 927],
            "description": "Menü 3/3: Grüner RENNEN-Button im VS-Screen nach der Cutscene",
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
        "tap_duration": TAP_DURATION,
        "tap_cooldown": TAP_COOLDOWN,
        "key_hold_duration": 0.012,
        "min_shift_cooldown": 0.085,
        "max_shift_cooldown": 0.220,
        "needle_pulse_hold": TAP_DURATION,
        "needle_release_pause": TAP_COOLDOWN,
        "popup_click_interval": MENU_CLICK_COOLDOWN,
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


def is_green_button(
    r: int,
    g: int,
    b: int,
    min_g: int = 115,
    ratio_r: float = 1.30,
    ratio_b: float = 1.60,
) -> bool:
    """
    Prüft, ob ein Pixel die grüne Farbe eines der drei Menü-Buttons
    ('WEITER', 'NEXT STAGE', 'RENNEN') hat.
    Alle drei Buttons haben in den Referenz-Screenshots exakt RGB(82, 146, 50).
    """
    return (
        g >= min_g
        and r >= 45
        and b <= 115
        and g > (r * ratio_r)
        and g > (b * ratio_b)
    )


# Alias für Abwärtskompatibilität
is_continue_button_green = is_green_button


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
                                if pt_name == "continue_button_point":
                                    if "weiter_button" not in loaded["points"] and isinstance(pt_val, dict):
                                        merged["points"]["weiter_button"].update(pt_val)
                                    continue
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
        if point_name == "continue_button_point":
            point_name = "weiter_button"
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
        if point_name == "continue_button_point":
            point_name = "weiter_button"
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
    POST_RACE_POPUP = "POST_RACE_POPUP"    # Phase 3: Rennende, W loslassen & 3-Stufen-Menü durchklicken


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
        Hält den weißen Tachozeiger durch High-Frequency Micro-Tapping von `w`
        (`TAP_DURATION` / `TAP_COOLDOWN`) am `needle_target_point` im blauen Bereich
        zwischen 4 und 5, ohne `w` jemals dauerhaft gedrückt zu halten.
        Wechselt sofort zu `RACING`, sobald die Ampel an `traffic_light_point` verschwindet.
      - Phase 2 (`RACING`):
        Hält `w` durchgehend gedrückt und schaltet mit `e` bei grünem `shift_point`
        (inkl. dynamischer 85–220 ms Entprellung für die schnellen ersten Gänge).
        Überwacht parallel den `weiter_button` für das Rennende.
      - Phase 3 (`POST_RACE_POPUP`):
        Lässt `w` sofort los, durchläuft den 3-Stufen-Menü-Loop (`WEITER` -> `NEXT STAGE` ->
        Cutscene abwarten -> `RENNEN`) und wechselt zurück zu `WAITING_AT_LIGHT`, sobald die
        Ampel für das nächste Rennen erscheint.
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
            self.weiter_pt = self.config.get_point_logical("weiter_button", lw, lh)
            self.continue_pt = self.weiter_pt
            self.next_stage_pt = self.config.get_point_logical("next_stage_button", lw, lh)
            self.rennen_pt = self.config.get_point_logical("rennen_button", lw, lh)
            self.light_ref_rgb = self.config.get_traffic_light_ref_rgb()

            self.shift_bbox = self.capture.build_bbox(*self.shift_pt, box_size=3)
            # 5x5 Boxen für Tachonadel, Ampel und die 3 grünen Menü-Buttons
            self.needle_bbox = self.capture.build_bbox(*self.needle_pt, box_size=5)
            self.light_bbox = self.capture.build_bbox(*self.light_pt, box_size=5)
            self.weiter_bbox = self.capture.build_bbox(*self.weiter_pt, box_size=5)
            self.continue_bbox = self.weiter_bbox
            self.next_stage_bbox = self.capture.build_bbox(*self.next_stage_pt, box_size=5)
            self.rennen_bbox = self.capture.build_bbox(*self.rennen_pt, box_size=5)

    def _is_any_menu_button_green(self, sct: Any) -> bool:
        """Prüft, ob irgendeiner der 3 Menü-Buttons (WEITER, NEXT STAGE, RENNEN) grün leuchtet."""
        for bbox in (self.weiter_bbox, self.next_stage_bbox, self.rennen_bbox):
            _, is_green = self.capture.sample_bbox_fast(sct, bbox, is_green_button)
            if is_green:
                return True
        return False

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
                f"  -> Micro-Tapping für blauen Bereich (4–5) bei {self.needle_pt} & warte auf Ampel-Start ...",
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
                f"  -> Rennen #{self.race_count} beendet! [W] losgelassen -> Starte 3-Stufen-Menü-Loop\n"
                f"     (1: WEITER {self.weiter_pt} -> 2: NEXT STAGE {self.next_stage_pt} -> 3: RENNEN {self.rennen_pt}) ...",
                flush=True,
            )

    def detect_initial_state(self, sct: Any) -> BotState:
        """Erkennt beim Aktivieren des Bots automatisch die aktuelle Spielphase."""
        if self._is_any_menu_button_green(sct):
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
            f"({'Vollautomatischer Renn-Bot (Ampel + W + E + 3-Stufen-Menü)' if new_mode == BotMode.FULL_AUTO else 'Nur Auto-Shifter (E bei Grün)'})",
            flush=True,
        )

    def stop(self) -> None:
        """Beendet die State-Machine und führt sofort einen Safety-Release für W und E aus."""
        with self._lock:
            self.running = False
            self.active = False
        self.driver.release_all()

    def _try_click_menu_buttons(self, sct: Any) -> bool:
        """
        Prüft der Reihe nach die drei grünen Menü-Buttons (WEITER, NEXT STAGE, RENNEN).
        Klickt bei Treffer (sofern Cooldown abgelaufen ist) und gibt True zurück, wenn ein Button grün ist.
        """
        menu_steps = (
            ("1/3", "WEITER", self.weiter_bbox, self.weiter_pt),
            ("2/3", "NEXT STAGE", self.next_stage_bbox, self.next_stage_pt),
            ("3/3", "RENNEN", self.rennen_bbox, self.rennen_pt),
        )
        now = time.monotonic()
        cooldown = float(
            self.config.data.get("timings", {}).get("popup_click_interval", MENU_CLICK_COOLDOWN)
        )

        for step_label, btn_name, bbox, (bx, by) in menu_steps:
            rgb, is_green = self.capture.sample_bbox_fast(sct, bbox, is_green_button)
            if is_green:
                if (now - self._last_popup_click_time) >= cooldown:
                    self._last_popup_click_time = now
                    r, g, b = rgb
                    timestamp = time.strftime("%H:%M:%S")
                    print(
                        f"[{timestamp}] [MENÜ {step_label}] Klicke auf {btn_name} bei ({bx}, {by}) "
                        f"[RGB({r}, {g}, {b})] ...",
                        flush=True,
                    )
                    self.driver.click_at(bx, by)
                return True
        return False

    def _step_waiting_at_light(self, sct: Any) -> None:
        """
        Phase 1 (`WAITING_AT_LIGHT`):
        1. Prüft die Ampel oben in der Mitte (`traffic_light_point`).
           Sobald die Ampel sichtbar war und dann verschwindet (GO!), sofort Wechsel in `RACING`.
        2. High-Frequency Micro-Tapping am `needle_target_point` (nur wenn Ampel erkannt wurde):
           - Sobald die weiße Tachonadel den Zielpunkt berührt oder minimal überschreitet
             (`needle_at_target == True`): Sofort `release_w()` + `time.sleep(tap_cooldown)`.
           - Solange die Tachonadel unterhalb des Zielpunkts ist (`needle_at_target == False`):
             W wird NIEMALS dauerhaft gehalten! Stattdessen ein kurzer, präziser Micro-Tap:
             `press_w()` -> `time.sleep(tap_duration)` -> `release_w()` -> `time.sleep(tap_cooldown)`.
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
                print(
                    "  [Ampel erkannt] Starte High-Frequency Micro-Tapping für blauen Bereich (4–5) ...",
                    flush=True,
                )
            self._light_gone_streak = 0
        else:
            if self._light_seen_in_waiting:
                self._light_gone_streak += 1
                # 2 aufeinanderfolgende Frames ohne Ampel-Box -> GO!
                if self._light_gone_streak >= 2:
                    self.transition_to(BotState.RACING, reason="Ampel verschwunden / GO!")
                    return
            else:
                # Noch keine Ampel da -> prüfen, ob einer der 3 grünen Menü-Buttons sichtbar ist
                self.driver.release_w()
                if self._is_any_menu_button_green(sct):
                    self._popup_green_streak += 1
                    if self._popup_green_streak >= 2:
                        self.transition_to(
                            BotState.POST_RACE_POPUP,
                            reason="Grüner Menü-Button erkannt",
                        )
                        self._try_click_menu_buttons(sct)
                        return
                else:
                    self._popup_green_streak = 0
                time.sleep(0.015)
                return

        # 2. High-Frequency Micro-Tapping am needle_target_point (wenn Ampel sichtbar ist)
        _, needle_at_target = self.capture.sample_bbox_fast(
            sct, self.needle_bbox, is_needle_white
        )

        timings = self.config.data.get("timings", {})
        tap_duration = float(timings.get("tap_duration", TAP_DURATION))
        tap_cooldown = float(timings.get("tap_cooldown", TAP_COOLDOWN))

        if needle_at_target:
            # Weiße Tachonadel berührt oder überschreitet Zielpunkt -> sofort W loslassen!
            self.driver.release_w()
            time.sleep(tap_cooldown)
        else:
            # Tachonadel ist unterhalb des Zielpunkts -> kurzer, präziser Micro-Tap (NIEMALS W dauerhaft halten!)
            self.driver.press_w()
            time.sleep(tap_duration)
            self.driver.release_w()
            time.sleep(tap_cooldown)

    def _step_racing(self, sct: Any) -> None:
        """
        Phase 2 (`RACING`):
        - Hält `w` durchgehend gedrückt (im `FULL_AUTO`-Modus).
        - Überwacht `shift_point` mit ~120 Hz und drückt sofort `e`, sobald Grün erkannt wird.
        - Prüft alle 6 Frames (~50 ms) nebenbei `self.weiter_bbox` mit `is_green_button`:
          Sobald 3-mal in Folge Grün erkannt wird -> `release_all()` & Wechsel in `POST_RACE_POPUP`.
        """
        if self.mode == BotMode.FULL_AUTO:
            self.driver.press_w()

        timings = self.config.data.get("timings", {})
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

        # 2. Im FULL_AUTO-Modus alle 6 Frames (~50 ms) prüfen, ob das End-Popup (WEITER) da ist
        if self.mode == BotMode.FULL_AUTO:
            self._race_frame_counter += 1
            if (self._race_frame_counter % 6) == 0:
                _, popup_green = self.capture.sample_bbox_fast(
                    sct, self.weiter_bbox, is_green_button
                )
                if popup_green:
                    self._popup_green_streak += 1
                    if self._popup_green_streak >= 3:
                        self.driver.release_all()
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
                        self._light_seen_in_waiting = True
                        return
                else:
                    self._light_present_streak = 0

    def _step_post_race_popup(self, sct: Any) -> None:
        """
        Phase 3 (`POST_RACE_POPUP`):
        - Garantiert `self.driver.release_w()`.
        - Prüft zuerst, ob an `self.light_bbox` die Ampel für das nächste Rennen erschienen ist
          (2 Frames in Folge `is_traffic_light_present` -> Wechsel zurück in `WAITING_AT_LIGHT`
          mit `self._light_seen_in_waiting = True`).
        - Prüft andernfalls mit `is_green_button` der Reihe nach die drei Menü-Button-Positionen:
          1. `weiter_button` (`self.weiter_bbox` / `self.weiter_pt`) – Popup 1: Rennstatistik ("WEITER")
          2. `next_stage_button` (`self.next_stage_bbox` / `self.next_stage_pt`) – Popup 2: Taxi City Ladder ("NEXT STAGE")
          3. `rennen_button` (`self.rennen_bbox` / `self.rennen_pt`) – Popup 3 nach Cutscene: VS-Screen ("RENNEN")
        - Während der Cutscene wartet die Schleife geduldig weiter.
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
                self.transition_to(
                    BotState.WAITING_AT_LIGHT,
                    reason="Nächstes Rennen gestartet – Ampel erkannt",
                )
                self._light_seen_in_waiting = True
                return
        else:
            self._light_present_streak = 0

        # 2. Der Reihe nach die 3 grünen Menü-Buttons prüfen und anklicken (mit 0.5s Cooldown)
        self._try_click_menu_buttons(sct)

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
