# -*- coding: utf-8 -*-
"""
core_utils.py – Kern des Taxi-Boss Drag-Race-Bots (plattformunabhängig).

Enthält:
  * DEFAULT_CONFIG / ConfigManager   – config.json laden/speichern (thread-safe, Deep-Merge)
  * Farbhelfer                        – color_distance, color_match, is_green, ...
  * needle_progress                   – Nadel-Fortschritt entlang der Tacho-Skala (wraparound-sicher)
  * ScreenReader                      – mss-Capture (Instanz pro Thread) inkl. Retina-Skalierung
  * InputBackend                      – abstrakte Tastatur/Maus-Schnittstelle (mac_bot / win_bot)
  * BotState / BotEngine              – State Machine im eigenen Daemon-Thread
  * BotGUI / create_gui / run_gui      – tkinter-Oberfläche (Main-Thread)

Koordinatensysteme:
  * rel      : 0.0–1.0 relativ zum primären Monitor (mss monitors[1]) – so steht es in config.json
  * logical  : Bildschirmpunkte (Maus, mss-Regionen); global, d.h. inkl. monitor left/top
  * phys     : physische Pixel des Capture-Bildes relativ zur Monitor-Ecke (Retina = logical * scale)

Python 3.9-kompatibel, nur Standardbibliothek + mss (lazy importiert).
"""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Konfiguration (EINZIGE Stelle für Default-Werte)
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

POINT_NAMES = [
    "light_box_1",
    "light_box_2",
    "light_lamp",
    "tacho_center",
    "needle_target",
    "shift_circle",
    "btn_weiter",
    "btn_next_stage",
    "btn_rennen",
    "Give_Up_Button",
]

MODULE_NAMES = ["auto_shift", "auto_start", "auto_skip"]

# Werte gemessen aus den 6 Screenshots (3420x2214 phys = 1710x1107 logisch, Retina x2,
# Roblox im Vollbild). Siehe README.md (Abschnitt „Vorkalibrierte Standardwerte“). Alle Punkte relativ zum Monitor.
#   * Ampelkasten normal  (13.53.03): (54,55,59)   dunkelgrau
#   * Ampelkasten Boss    (22.09.01): (142,57,60)  dunkelrot/weinrot
#   * an denselben Punkten im GO-Bild (09.32.46): Himmel (168,157,166) -> kein Match
#   * Lampe 1 leuchtet in beiden Varianten (197,66,61)
#   * Tacho-Zentrum phys (1709.5, 1886); Nadel nur weiß im Radius ~110-240 px phys
#   * Blaue Zone (Ring r=300 px phys, Farbe ~(146,220,235)):
#       normal 87.5°-140.0°, Boss 93.4°-103.9° -> wird live erkannt (needle.zone_*)
#   * Buttons grün (82,146,50); CASH OUT (245,189,89)
#   * ACHTUNG: Tacho-Ring ist im Rennen grün (74,143,52) bei r=283-318 px -> der
#     WEITER-Punkt liegt bewusst links unterhalb vom Text, innerhalb des Tacho-Zifferblatts.
DEFAULT_CONFIG = {
    "modules": {"auto_shift": True, "auto_start": True, "auto_skip": True},
    "timing": {
        "tap_duration": 0.014,
        "tap_cooldown": 0.025,
        "fine_tap_duration": 0.014,
        "fine_tap_cooldown": 0.025,
        "boost_tap_duration": 0.035,
        "boost_tap_cooldown": 0.012,
        "shift_cooldown": 0.25,
        "menu_click_delay": 0.5,
        "loop_interval": 0.004,
        "go_confirm_frames": 3,
        "shift_tap_duration": 0.03,
        "shift_green_threshold": 100,
        # Zusatz (nicht im Vertrag): derselbe Button wird erst nach dieser Zeit erneut geklickt
        "button_reclick_cooldown": 1.5,
    },
    "needle": {
        "start_angle_deg": 240.0,
        "end_angle_deg": -80.0,
        "radius_rel": 0.04386,  # 150 px phys bei 3420 px Breite
        "white_min": 215,
        "white_max_spread": 35,
        "target_offset_deg": 0.0,
        "angle_tolerance": 2.0,
        "slow_zone_angle": 15.0,
        # Zusatz: Abtastradien als Faktor von radius_rel (ca. 120–180 px phys)
        "radius_factors": [0.80, 0.90, 1.0, 1.10, 1.20],
        # Zusatz: blaue Zielzone live auf dem Tacho-Ring erkennen (normal breit, Boss schmal)
        "zone_detect": True,
        "zone_radius_rel": 0.08772,  # 300 px phys
        "zone_target_fraction": 0.45,  # leicht links der Mitte (0 = Anfang, 0.5 = Mitte, 1 = Ende)
        "zone_refresh_s": 0.2,
        "zone_r_max": 165,
        "zone_g_min": 180,
        "zone_b_min": 210,
    },
    "points": {
        "light_box_1": {"x": 0.48246, "y": 0.20777, "rgb": [54, 55, 59], "tol": 32},
        "light_box_2": {"x": 0.52632, "y": 0.20777, "rgb": [54, 55, 59], "tol": 32},
        "light_lamp": {"x": 0.45906, "y": 0.16938, "rgb": [197, 66, 61], "tol": 45},
        "tacho_center": {"x": 0.49985, "y": 0.85185, "rgb": [200, 200, 200], "tol": 40},
        # Fallback-Ziel (wenn Zone nicht erkannt): 99° – liegt in normaler UND Boss-Zone
        "needle_target": {"x": 0.48614, "y": 0.71802, "rgb": [146, 221, 238], "tol": 40},
        "shift_circle": {"x": 0.50000, "y": 0.77913, "rgb": [50, 50, 50], "tol": 50},
        "btn_weiter": {"x": 0.46667, "y": 0.76016, "rgb": [82, 146, 50], "tol": 45},
        "btn_next_stage": {"x": 0.71053, "y": 0.92954, "rgb": [82, 146, 50], "tol": 45},
        "btn_rennen": {"x": 0.50292, "y": 0.83740, "rgb": [82, 146, 50], "tol": 45},
        "Give_Up_Button": {"x": 0.35088, "y": 0.63415, "rgb": [213, 87, 78], "tol": 45},
    },
    "colors": {
        "light_box_normal": [54, 55, 59],
        "light_box_boss": [142, 57, 60],
        "light_lamp_red": [197, 66, 61],
        "needle_white": [255, 255, 255],
        "shift_green": [80, 200, 80],  # Schätzung – im Screenshot nicht grün sichtbar
        "button_green": [82, 146, 50],
        "give_up_red": [213, 87, 78],
        "zone_blue": [146, 220, 235],
    },
    "hotkeys": {"calibrate": "f8", "toggle": "f9", "emergency": "f10"},
}

RGB = Tuple[int, int, int]


def _deep_merge(defaults: dict, data: dict) -> dict:
    """Gibt neue Dict zurück: defaults, überschrieben von data (rekursiv)."""
    out = copy.deepcopy(defaults)
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class ConfigManager:
    """Thread-sichere Verwaltung von config.json."""

    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULT_CONFIG)
        self.load()

    def load(self) -> dict:
        with self._lock:
            if not os.path.exists(self.path):
                self._data = copy.deepcopy(DEFAULT_CONFIG)
                try:
                    self.save()
                except OSError:
                    pass
                return copy.deepcopy(self._data)
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("config root is not an object")
                self._data = _deep_merge(DEFAULT_CONFIG, raw)
            except (ValueError, OSError):
                # kaputte Datei -> Backup + Defaults
                backup = "%s.broken-%d.bak" % (self.path, int(time.time()))
                try:
                    os.replace(self.path, backup)
                except OSError:
                    pass
                self._data = copy.deepcopy(DEFAULT_CONFIG)
                try:
                    self.save()
                except OSError:
                    pass
            return copy.deepcopy(self._data)

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)

    def get(self, *keys: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for k in keys:
                if isinstance(node, dict) and k in node:
                    node = node[k]
                else:
                    return default
            return copy.deepcopy(node)

    def set(self, *keys_and_value: Any) -> None:
        """set("timing", "tap_duration", 0.02)"""
        if len(keys_and_value) < 2:
            raise ValueError("set() braucht mindestens einen Key und einen Wert")
        keys, value = keys_and_value[:-1], keys_and_value[-1]
        with self._lock:
            node = self._data
            for k in keys[:-1]:
                if not isinstance(node.get(k), dict):
                    node[k] = {}
                node = node[k]
            node[keys[-1]] = copy.deepcopy(value)

    def reset_to_defaults(self) -> dict:
        with self._lock:
            self._data = copy.deepcopy(DEFAULT_CONFIG)
            try:
                self.save()
            except OSError:
                pass
            return copy.deepcopy(self._data)

    @property
    def data(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)


# ---------------------------------------------------------------------------
# Farbhelfer
# ---------------------------------------------------------------------------
def color_distance(a: Sequence[int], b: Sequence[int]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def color_match(rgb: Sequence[int], target: Sequence[int], tol: float) -> bool:
    return color_distance(rgb, target) <= tol


def is_green(rgb: Sequence[int], min_g: int = 100) -> bool:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return g > r + 40 and g > b + 30 and g > min_g


def is_orange_or_yellow(rgb: Sequence[int]) -> bool:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return r > 180 and g >= 90 and b < 110 and (r - b) > 100 and r >= g - 15


def is_bright_red(rgb: Sequence[int]) -> bool:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return r > 170 and g < 90 and b < 90 and r > g + 100


def is_white(rgb: Sequence[int], min_val: int = 215, max_spread: int = 35) -> bool:
    lo = min(rgb[0], rgb[1], rgb[2])
    hi = max(rgb[0], rgb[1], rgb[2])
    return lo >= min_val and (hi - lo) <= max_spread


def is_zone_blue(
    rgb: Sequence[int],
    r_max: int = 165,
    g_min: int = 180,
    b_min: int = 210,
) -> bool:
    """Hellblaue Ziel-Zone am Tacho-Ring (~(146,220,235)); NICHT der blassblaue Rest-Ring (~(82,139,149))."""
    r, g, b = rgb[0], rgb[1], rgb[2]
    return r <= r_max and g >= g_min and b >= b_min and b > r + 35


def _is_set_color(c: Any) -> bool:
    """Farbe gilt als konfiguriert, wenn nicht [0,0,0]."""
    try:
        return any(int(v) != 0 for v in c[:3])
    except Exception:
        return False


def needle_progress(angle: float, start: float, end: float) -> float:
    """Fortschritt (in Grad) der Nadel entlang der Skala von start Richtung end.

    Winkel mathematisch (0°=rechts, 90°=oben). 0 = Nadel bei start, sweep = bei end.
    Wraparound-sicher; Winkel knapp vor start ergeben kleine negative Werte.
    """
    direction = -1.0 if end < start else 1.0  # -1 = im Uhrzeigersinn (Winkel fällt)
    sweep = ((end - start) * direction) % 360.0
    if sweep == 0.0:
        sweep = 360.0
    d = ((angle - start) * direction) % 360.0
    if d > sweep + (360.0 - sweep) / 2.0:
        d -= 360.0
    return d


# ---------------------------------------------------------------------------
# ScreenReader
# ---------------------------------------------------------------------------
class ScreenReader:
    """Pixel-Zugriff via mss. mss-Instanz pro Thread (mss ist nicht thread-safe)."""

    # Max. Kantenlänge (phys. px) eines Grab-Clusters; weit verstreute Punkte -> mehrere kleine Grabs
    MAX_CLUSTER_SIDE = 700

    def __init__(self) -> None:
        self._local = threading.local()
        self._lock = threading.Lock()
        self._monitor: Optional[dict] = None
        self._scale: Optional[float] = None

    # -- intern -----------------------------------------------------------
    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            import mss  # lazy import

            sct = mss.mss()
            self._local.sct = sct
        return sct

    def refresh(self) -> None:
        """Monitor-Geometrie/Skalierung neu ermitteln (z.B. nach Auflösungswechsel)."""
        with self._lock:
            self._monitor = None
            self._scale = None

    # -- Geometrie ----------------------------------------------------------
    def monitor(self) -> dict:
        if self._monitor is None:
            mons = self._sct().monitors
            mon = mons[1] if len(mons) > 1 else mons[0]
            with self._lock:
                self._monitor = dict(mon)
        return dict(self._monitor)

    def scale(self) -> float:
        if self._scale is None:
            mon = self.monitor()
            region = {"left": mon["left"], "top": mon["top"], "width": 10, "height": 10}
            img = self._sct().grab(region)
            with self._lock:
                self._scale = max(0.1, img.width / 10.0)
        return float(self._scale)

    def rel_to_logical(self, rx: float, ry: float) -> Tuple[int, int]:
        mon = self.monitor()
        return (int(round(mon["left"] + rx * mon["width"])), int(round(mon["top"] + ry * mon["height"])))

    def logical_to_rel(self, x: float, y: float) -> Tuple[float, float]:
        mon = self.monitor()
        return ((x - mon["left"]) / float(mon["width"]), (y - mon["top"]) / float(mon["height"]))

    def rel_to_phys(self, rx: float, ry: float) -> Tuple[float, float]:
        mon = self.monitor()
        s = self.scale()
        return (rx * mon["width"] * s, ry * mon["height"] * s)

    # -- Pixel lesen ----------------------------------------------------------
    def get_pixel(self, rx: float, ry: float) -> RGB:
        return self.get_pixels([(rx, ry)])[0]

    def get_pixels(self, rel_points: Sequence[Tuple[float, float]]) -> List[RGB]:
        pts = [self.rel_to_phys(rx, ry) for rx, ry in rel_points]
        return self.get_pixels_phys(pts)

    def get_avg(self, rx: float, ry: float, radius_px: int = 2) -> RGB:
        """Mittelwert eines (2r+1)²-Quadrats (radius_px in physischen Pixeln)."""
        cx, cy = self.rel_to_phys(rx, ry)
        pts = [(cx + dx, cy + dy) for dy in range(-radius_px, radius_px + 1) for dx in range(-radius_px, radius_px + 1)]
        pix = self.get_pixels_phys(pts)
        n = float(len(pix))
        return (
            int(round(sum(p[0] for p in pix) / n)),
            int(round(sum(p[1] for p in pix) / n)),
            int(round(sum(p[2] for p in pix) / n)),
        )

    def get_pixels_phys(self, phys_points: Sequence[Tuple[float, float]]) -> List[RGB]:
        """Pixel an physischen Koordinaten (relativ zur Monitor-Ecke) lesen."""
        if not phys_points:
            return []
        mon = self.monitor()
        s = self.scale()
        max_x = int(mon["width"] * s) - 1
        max_y = int(mon["height"] * s) - 1
        ipts = []
        for px, py in phys_points:
            ix = min(max(int(round(px)), 0), max_x)
            iy = min(max(int(round(py)), 0), max_y)
            ipts.append((ix, iy))
        return self._read_pixels_phys(ipts)

    def _cluster(self, ipts: List[Tuple[int, int]]) -> List[list]:
        """Greedy-Clustering: Punkte in möglichst wenige Bounding-Boxen (Kante <= MAX_CLUSTER_SIDE)."""
        side = self.MAX_CLUSTER_SIDE
        clusters: List[list] = []  # [x0, y0, x1, y1, [indices]]
        for i, (x, y) in enumerate(ipts):
            for c in clusters:
                nx0, ny0 = min(c[0], x), min(c[1], y)
                nx1, ny1 = max(c[2], x), max(c[3], y)
                if nx1 - nx0 <= side and ny1 - ny0 <= side:
                    c[0], c[1], c[2], c[3] = nx0, ny0, nx1, ny1
                    c[4].append(i)
                    break
            else:
                clusters.append([x, y, x, y, [i]])
        return clusters

    def _grab_phys_bbox(self, x0: int, y0: int, x1: int, y1: int):
        """Grab der phys. Bounding-Box (inklusive). Rückgabe (img, lx0, ly0, s_img)."""
        mon = self.monitor()
        s = self.scale()
        lx0 = int(math.floor(x0 / s))
        ly0 = int(math.floor(y0 / s))
        lx1 = min(int(math.ceil((x1 + 1) / s)), mon["width"])
        ly1 = min(int(math.ceil((y1 + 1) / s)), mon["height"])
        w = max(1, lx1 - lx0)
        h = max(1, ly1 - ly0)
        region = {"left": mon["left"] + lx0, "top": mon["top"] + ly0, "width": w, "height": h}
        img = self._sct().grab(region)
        s_img = img.width / float(w)
        if abs(s_img - s) > 0.01:  # Auflösung/Skalierung hat sich geändert
            with self._lock:
                self._scale = s_img
        return img, lx0, ly0, s_img

    def _read_pixels_phys(self, ipts: List[Tuple[int, int]]) -> List[RGB]:
        """Override-Punkt für Tests. ipts: geklemmte int-Koordinaten (phys)."""
        out: List[RGB] = [(0, 0, 0)] * len(ipts)
        for x0, y0, x1, y1, idxs in self._cluster(ipts):
            img, lx0, ly0, s_img = self._grab_phys_bbox(x0, y0, x1, y1)
            raw = img.raw if hasattr(img, "raw") else img.bgra  # BGRA
            w, h = img.width, img.height
            ox, oy = lx0 * s_img, ly0 * s_img
            for i in idxs:
                px, py = ipts[i]
                ix = min(max(int(px - ox), 0), w - 1)
                iy = min(max(int(py - oy), 0), h - 1)
                k = (iy * w + ix) * 4
                out[i] = (raw[k + 2], raw[k + 1], raw[k])
        return out

    def grab_region(self, rx1: float, ry1: float, rx2: float, ry2: float):
        """Grab einer rel. Region. Rückgabe (mss-Image, (origin_x_phys, origin_y_phys))."""
        x0, y0 = self.rel_to_phys(min(rx1, rx2), min(ry1, ry2))
        x1, y1 = self.rel_to_phys(max(rx1, rx2), max(ry1, ry2))
        img, lx0, ly0, s_img = self._grab_phys_bbox(int(x0), int(y0), int(x1), int(y1))
        return img, (int(round(lx0 * s_img)), int(round(ly0 * s_img)))


# ---------------------------------------------------------------------------
# InputBackend (abstrakt)
# ---------------------------------------------------------------------------
class InputBackend:
    """Abstrakte Eingabe-Schnittstelle. mac_bot.py / win_bot.py implementieren sie."""

    def press(self, key: str) -> None:
        raise NotImplementedError

    def release(self, key: str) -> None:
        raise NotImplementedError

    def tap(self, key: str, duration: float) -> None:
        self.press(key)
        try:
            time.sleep(max(0.0, duration))
        finally:
            self.release(key)

    def click(self, x: int, y: int) -> None:
        """Linksklick an logischen Koordinaten (inkl. 1px-Wackler für Roblox)."""
        raise NotImplementedError

    def get_mouse_position(self) -> Tuple[int, int]:
        raise NotImplementedError

    def release_all(self) -> None:
        for k in ("w", "e"):
            try:
                self.release(k)
            except Exception:
                pass


class DummyInputBackend(InputBackend):
    """Zeichnet Eingaben nur auf (Tests / Trockenlauf)."""

    def __init__(self, on_event: Optional[Callable[[tuple], None]] = None):
        self.events: List[tuple] = []  # (t, kind, payload)
        self.held: set = set()
        self.mouse = (0, 0)
        self.on_event = on_event
        self._lock = threading.Lock()

    def _rec(self, kind: str, payload: Any) -> None:
        ev = (time.perf_counter(), kind, payload)
        with self._lock:
            self.events.append(ev)
        if self.on_event:
            self.on_event(ev)

    def press(self, key: str) -> None:
        self.held.add(key)
        self._rec("press", key)

    def release(self, key: str) -> None:
        self.held.discard(key)
        self._rec("release", key)

    def click(self, x: int, y: int) -> None:
        self.mouse = (x, y)
        self._rec("click", (x, y))

    def get_mouse_position(self) -> Tuple[int, int]:
        return self.mouse

    def snapshot(self) -> List[tuple]:
        with self._lock:
            return list(self.events)


# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------
class BotState(str, Enum):
    PAUSED = "PAUSED"
    WAITING = "WAITING"
    AT_LIGHT = "AT_LIGHT"
    RACING = "RACING"
    MENU_SKIP = "MENU_SKIP"
    STOPPED = "STOPPED"


class BotEngine:
    GREEN_MENU_BUTTONS = ("btn_weiter", "btn_next_stage", "btn_rennen")
    MENU_BUTTONS = ("btn_weiter", "btn_next_stage", "btn_rennen", "Give_Up_Button")
    MENU_HOLD_S = 2.0  # so lange nach einem Klick in MENU_SKIP bleiben (gegen Status-Flackern)
    BUTTON_SAMPLE_OFFSET_LOGICAL = 3

    def __init__(
        self,
        config: ConfigManager,
        input_backend: InputBackend,
        screen: Optional[ScreenReader] = None,
        log: Callable[[str], Any] = print,
    ):
        self.config = config
        self.input = input_backend
        self.screen = screen if screen is not None else ScreenReader()
        self._log_fn = log
        self._state_lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._dbg_lock = threading.Lock()
        self._state = BotState.PAUSED
        self._halt = threading.Event()  # gesetzt = nicht aktiv (unterbricht Sleeps sofort)
        self._halt.set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_debug: Dict[str, Any] = {}
        self._last_err = ("", 0.0)
        self._reset_runtime()

    # -- Hilfen -------------------------------------------------------------
    def log(self, msg: str) -> None:
        try:
            self._log_fn(msg)
        except Exception:
            pass

    def _reset_runtime(self) -> None:
        self._light_seen = False
        self._light_missing = 0
        self._w_held = False
        self._last_shift = 0.0
        self._last_click: Dict[str, float] = {}
        self._last_click_time = 0.0
        self._await_light = False
        self._zone_cache: Tuple[float, Optional[Tuple[float, float]]] = (-1e9, None)

    @property
    def state(self) -> BotState:
        with self._state_lock:
            return self._state

    @property
    def last_debug(self) -> dict:
        with self._dbg_lock:
            return dict(self._last_debug)

    def _dbg(self, **kw: Any) -> None:
        with self._dbg_lock:
            self._last_debug.update(kw)

    def _set_state(self, new: BotState, reason: str = "") -> None:
        with self._state_lock:
            old = self._state
            if old == new:
                return
            self._state = new
        self.log("Status: %s -> %s%s" % (old.value, new.value, (" (%s)" % reason) if reason else ""))

    def _is_active(self) -> bool:
        return (not self._stop.is_set()) and self.state not in (BotState.PAUSED, BotState.STOPPED)

    def _sleep(self, t: float) -> bool:
        """Unterbrechbarer Sleep. False = Bot wurde inzwischen pausiert/gestoppt."""
        if t > 0:
            if self._halt.wait(t):
                return False
        return self._is_active()

    def _io_press(self, key: str) -> bool:
        with self._io_lock:
            if not self._is_active():
                return False
            self.input.press(key)
            return True

    def _io_release(self, key: str) -> None:
        with self._io_lock:
            self.input.release(key)

    def _hold_w(self) -> None:
        if not self._w_held and self._io_press("w"):
            self._w_held = True

    def _release_w(self) -> None:
        if self._w_held:
            self._io_release("w")
            self._w_held = False

    # -- Steuerung (von GUI/Hotkey-Threads aufrufbar) -----------------------
    def start_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._halt.set()
        with self._state_lock:
            self._state = BotState.PAUSED
        self._reset_runtime()
        self._thread = threading.Thread(target=self._run, name="BotEngine", daemon=True)
        self._thread.start()

    def safety_release(self) -> None:
        with self._io_lock:
            try:
                self.input.release_all()
            except Exception:
                for k in ("w", "e"):
                    try:
                        self.input.release(k)
                    except Exception:
                        pass
            self._w_held = False

    def pause(self) -> None:
        with self._io_lock:
            with self._state_lock:
                if self._state == BotState.STOPPED:
                    return
                self._halt.set()
                self._set_state(BotState.PAUSED, "Pause")
            self.safety_release()

    def resume(self) -> None:
        if self._thread is None or not self._thread.is_alive() or self.state == BotState.STOPPED:
            self.start_thread()
        with self._state_lock:
            if self._state != BotState.PAUSED:
                return
            self._reset_runtime()
            self._set_state(BotState.WAITING, "Start")
            self._halt.clear()

    def toggle_pause(self) -> None:
        if self._is_active():
            self.pause()
        else:
            self.resume()

    def emergency_stop(self) -> None:
        with self._io_lock:
            self._stop.set()
            self._halt.set()
            self._set_state(BotState.STOPPED, "NOT-AUS")
            self.safety_release()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        self.safety_release()

    def set_module(self, name: str, enabled: bool) -> None:
        if name not in MODULE_NAMES:
            raise ValueError("Unbekanntes Modul: %r" % name)
        self.config.set("modules", name, bool(enabled))
        self.safety_release()
        self.log("Modul %s: %s" % (name, "AN" if enabled else "AUS"))

    def calibrate_point(self, name: str, x: Optional[int] = None, y: Optional[int] = None) -> dict:
        if name not in POINT_NAMES:
            raise ValueError("Unbekannter Punkt: %r" % name)
        if x is None or y is None:
            x, y = self.input.get_mouse_position()
        rx, ry = self.screen.logical_to_rel(x, y)
        rx = min(max(rx, 0.0), 1.0)
        ry = min(max(ry, 0.0), 1.0)
        rgb = list(self.screen.get_avg(rx, ry, 2))
        old = self.config.get("points", name, default={}) or {}
        tol = old.get("tol", DEFAULT_CONFIG["points"][name]["tol"])
        entry = {"x": round(rx, 5), "y": round(ry, 5), "rgb": rgb, "tol": tol}
        self.config.set("points", name, entry)
        try:
            self.config.save()
        except OSError as e:
            self.log("Config speichern fehlgeschlagen: %s" % e)
        self.log("Kalibriert %s: x=%.4f y=%.4f rgb=%s" % (name, rx, ry, rgb))
        return dict(entry)

    # -- Erkennung ------------------------------------------------------------
    @staticmethod
    def _pt_ok(pt: Optional[dict]) -> bool:
        return bool(pt) and not (float(pt.get("x", 0)) == 0.0 and float(pt.get("y", 0)) == 0.0)

    def _eval_light(self, cfg: dict, b1: RGB, b2: RGB, lamp: RGB) -> Tuple[bool, str]:
        pts, colors = cfg["points"], cfg["colors"]
        p1, p2, pl = pts.get("light_box_1"), pts.get("light_box_2"), pts.get("light_lamp")
        if self._pt_ok(p1) and self._pt_ok(p2):
            t1, t2 = float(p1.get("tol", 40)), float(p2.get("tol", 40))
            cand = []
            for kind, key in (("normal", "light_box_normal"), ("boss", "light_box_boss")):
                c = colors.get(key)
                if c and _is_set_color(c) and color_match(b1, c, t1) and color_match(b2, c, t2):
                    cand.append((color_distance(b1, c) + color_distance(b2, c), kind))
            if cand:
                return True, min(cand)[1]
        if self._pt_ok(pl):
            c = colors.get("light_lamp_red")
            if is_bright_red(lamp) or (c and _is_set_color(c) and color_match(lamp, c, float(pl.get("tol", 60)))):
                return True, "lamp"
        return False, "none"

    def detect_light(self) -> Tuple[bool, str]:
        cfg = self.config.data
        pts = cfg["points"]
        rel = [(pts[n]["x"], pts[n]["y"]) for n in ("light_box_1", "light_box_2", "light_lamp")]
        b1, b2, lamp = self.screen.get_pixels(rel)
        return self._eval_light(cfg, b1, b2, lamp)

    def _target_angle(self, cfg: dict) -> Optional[float]:
        """Zielwinkel der Nadel: Mitte der live erkannten blauen Zone, sonst needle_target-Punkt."""
        nd = cfg["needle"]
        offset = float(nd.get("target_offset_deg", 0.0))
        if nd.get("zone_detect", True):
            zone = self._zone_cached(cfg)
            if zone is not None:
                frac = min(max(float(nd.get("zone_target_fraction", 0.45)), 0.0), 1.0)
                return zone[0] + (zone[1] - zone[0]) * frac + offset
        pts = cfg["points"]
        c, t = pts.get("tacho_center"), pts.get("needle_target")
        if not (self._pt_ok(c) and self._pt_ok(t)):
            return None
        cx, cy = self.screen.rel_to_phys(c["x"], c["y"])
        tx, ty = self.screen.rel_to_phys(t["x"], t["y"])
        ang = math.degrees(math.atan2(-(ty - cy), tx - cx))  # y-Achse invertiert
        return ang + offset

    def _zone_cached(self, cfg: dict) -> Optional[Tuple[float, float]]:
        now = time.perf_counter()
        t, zone = self._zone_cache
        if now - t >= float(cfg["needle"].get("zone_refresh_s", 0.2)):
            zone = self.read_blue_zone(cfg)
            self._zone_cache = (now, zone)
        return zone

    def read_blue_zone(self, cfg: Optional[dict] = None) -> Optional[Tuple[float, float]]:
        """Blaue Ziel-Zone auf dem Tacho-Ring suchen.

        Rückgabe (erster_winkel, letzter_winkel) in Skalenrichtung (start -> end), oder None.
        Normal: ca. (140°, 87.5°) – Boss: ca. (104°, 93.4°).
        """
        cfg = cfg if cfg is not None else self.config.data
        nd = cfg["needle"]
        c = cfg["points"].get("tacho_center")
        if not self._pt_ok(c):
            return None
        mon = self.screen.monitor()
        s = self.screen.scale()
        cx, cy = self.screen.rel_to_phys(c["x"], c["y"])
        r = float(nd.get("zone_radius_rel", 0.08772)) * mon["width"] * s
        start = float(nd.get("start_angle_deg", 240.0))
        end = float(nd.get("end_angle_deg", -80.0))
        r_max = int(nd.get("zone_r_max", 165))
        g_min = int(nd.get("zone_g_min", 180))
        b_min = int(nd.get("zone_b_min", 210))
        step = -0.5 if end < start else 0.5
        n = int(abs(end - start) / 0.5) + 1
        angles = [start + i * step for i in range(n)]
        radii = (r * 0.94, r * 0.98, r * 1.02, r * 1.05)
        nr = len(radii)
        pts = []
        for a in angles:
            ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
            for rr in radii:
                pts.append((cx + rr * ca, cy - rr * sa))
        pix = self.screen.get_pixels_phys(pts)
        hit = [
            any(is_zone_blue(pix[nr * i + j], r_max, g_min, b_min) for j in range(nr))
            for i in range(n)
        ]
        # längster Lauf, Lücken bis 3 Samples (Skalenstriche) werden überbrückt
        best: Optional[Tuple[int, int]] = None
        i = 0
        while i < n:
            if not hit[i]:
                i += 1
                continue
            j, last, gap = i, i, 0
            while j + 1 < n:
                j += 1
                if hit[j]:
                    last, gap = j, 0
                else:
                    gap += 1
                    if gap > 3:
                        break
            if best is None or (last - i) > (best[1] - best[0]):
                best = (i, last)
            i = last + 1
        if best is None or best[1] - best[0] < 2:  # < 1° -> Rauschen
            return None
        return angles[best[0]], angles[best[1]]

    def _read_needle(self, cfg: dict) -> Optional[float]:
        nd = cfg["needle"]
        c = cfg["points"].get("tacho_center")
        if not self._pt_ok(c):
            return None
        mon = self.screen.monitor()
        s = self.screen.scale()
        cx, cy = self.screen.rel_to_phys(c["x"], c["y"])
        r = float(nd.get("radius_rel", 0.04386)) * mon["width"] * s
        start = float(nd.get("start_angle_deg", 240.0))
        end = float(nd.get("end_angle_deg", -80.0))
        wmin = int(nd.get("white_min", 215))
        wspread = int(nd.get("white_max_spread", 35))
        factors = nd.get("radius_factors") or [0.80, 0.90, 1.0, 1.10, 1.20]
        step = -1.0 if end < start else 1.0
        n = int(abs(end - start)) + 1
        angles = [start + i * step for i in range(n)]
        pts = []
        for a in angles:
            ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
            for f in factors:
                pts.append((cx + r * f * ca, cy - r * f * sa))
        pix = self.screen.get_pixels_phys(pts)
        nf = len(factors)
        scores = [0] * n
        for i in range(n):
            base = i * nf
            scores[i] = sum(1 for j in range(nf) if is_white(pix[base + j], wmin, wspread))
        best = max(scores) if scores else 0
        if best <= 0:
            return None
        bi = scores.index(best)
        # Cluster um das Maximum: zusammenhängende Winkel mit >= best-1 (mind. 1) Treffern
        thr = max(1, best - 1)
        lo = bi
        while lo - 1 >= 0 and scores[lo - 1] >= thr:
            lo -= 1
        hi = bi
        while hi + 1 < n and scores[hi + 1] >= thr:
            hi += 1
        wsum = sum(scores[i] for i in range(lo, hi + 1))
        return sum(angles[i] * scores[i] for i in range(lo, hi + 1)) / float(wsum)

    def read_needle_angle(self) -> Optional[float]:
        cfg = self.config.data
        ang = self._read_needle(cfg)
        nd = cfg["needle"]
        start = float(nd.get("start_angle_deg", 240.0))
        end = float(nd.get("end_angle_deg", -80.0))
        cw = needle_progress(ang, start, end) if ang is not None else None
        self._dbg(needle_angle=ang, cw_angle=None if cw is None else round(cw, 1))
        return ang

    def _button_samples(self, pt: dict, s: float) -> List[Tuple[float, float]]:
        cx, cy = self.screen.rel_to_phys(pt["x"], pt["y"])
        d = self.BUTTON_SAMPLE_OFFSET_LOGICAL * s
        return [(cx, cy), (cx - d, cy), (cx + d, cy), (cx, cy - d), (cx, cy + d)]

    @staticmethod
    def _is_flat_ui_color(samples: Sequence[RGB], max_spread: int = 18) -> bool:
        """Prüft, ob alle Sample-Punkte im Button denselben flachen UI-Farbton haben.

        Verhindert, dass ein schattiertes 3D-Auto im Hintergrund mit einem UI-Button verwechselt wird.
        """
        if len(samples) < 3:
            return False
        for ch in range(3):
            vals = [p[ch] for p in samples]
            if max(vals) - min(vals) > max_spread:
                return False
        return True

    def _is_button_green(self, rgb: RGB, cfg: dict, tol: float) -> bool:
        """Prüft ausschließlich auf den grünen UI-Farbton (G deutlich > R und B)."""
        r, g, b = rgb[0], rgb[1], rgb[2]
        if g <= r + 35 or g <= b + 25 or g < 100:
            return False
        c = cfg["colors"].get("button_green")
        if c and _is_set_color(c) and color_match(rgb, c, tol):
            return True
        return is_green(rgb)

    def _is_give_up_red(self, rgb: RGB, cfg: dict, tol: float) -> bool:
        """Prüft ausschließlich auf den roten GIVE UP-Farbton (R deutlich > G und B)."""
        r, g, b = rgb[0], rgb[1], rgb[2]
        if not (r > 160 and g < 130 and b < 130 and r > g + 75 and r > b + 75):
            return False
        c = cfg["colors"].get("give_up_red") or cfg["points"].get("Give_Up_Button", {}).get("rgb")
        if c and _is_set_color(c):
            return color_match(rgb, c, tol)
        return True

    def _is_shift_green(self, rgb: RGB, cfg: dict, tol: float) -> bool:
        thr = int(
            cfg.get("timing", {}).get(
                "shift_green_threshold",
                cfg.get("colors", {}).get("shift_green_threshold", 100),
            )
        )
        if is_green(rgb, min_g=thr):
            return True
        c = cfg["colors"].get("shift_green")
        return bool(c) and _is_set_color(c) and color_match(rgb, c, tol) and rgb[1] > rgb[0]

    # -- Aktionen -------------------------------------------------------------
    def _micro_tap(
        self,
        timing: dict,
        duration: Optional[float] = None,
        cooldown: Optional[float] = None,
    ) -> None:
        if not self._io_press("w"):
            return
        self._w_held = True
        dur = float(
            duration
            if duration is not None
            else timing.get("fine_tap_duration", timing.get("tap_duration", 0.014))
        )
        cd = float(
            cooldown
            if cooldown is not None
            else timing.get("fine_tap_cooldown", timing.get("tap_cooldown", 0.025))
        )
        ok = self._sleep(dur)
        self._io_release("w")
        self._w_held = False
        if ok:
            self._sleep(cd)

    def _shift(self, timing: dict) -> None:
        if not self._io_press("e"):
            return
        try:
            self._sleep(float(timing.get("shift_tap_duration", 0.03)))
        finally:
            self._io_release("e")
        self._last_shift = time.perf_counter()
        self.log("Shift (E)")

    def _click_button(self, name: str, cfg: dict) -> None:
        pt = cfg["points"][name]
        x, y = self.screen.rel_to_logical(pt["x"], pt["y"])
        with self._io_lock:
            if not self._is_active():
                return
            self.input.release("w")  # immer vor dem Klick
            self._w_held = False
            self.input.click(x, y)
        now = time.perf_counter()
        self._last_click[name] = now
        self._last_click_time = now
        self._light_seen = False
        self._light_missing = 0
        self._await_light = name == "btn_rennen"
        self.log("Klick %s @ (%d, %d)" % (name, x, y))
        self._set_state(BotState.MENU_SKIP, name)
        self._sleep(float(cfg["timing"].get("menu_click_delay", 0.5)))

    # -- Hauptschleife ----------------------------------------------------------
    def _run(self) -> None:
        self.log("Engine-Thread gestartet (PAUSED)")
        try:
            while not self._stop.is_set():
                if not self._is_active():
                    self._stop.wait(0.02)
                    continue
                t0 = time.perf_counter()
                try:
                    self._step()
                except Exception as e:  # noqa: BLE001
                    self.safety_release()
                    msg = "%s: %s" % (type(e).__name__, e)
                    now = time.perf_counter()
                    if msg != self._last_err[0] or now - self._last_err[1] > 2.0:
                        self.log("Fehler in Engine-Loop: %s" % msg)
                        self._last_err = (msg, now)
                    self._stop.wait(0.1)
                    continue
                dt = time.perf_counter() - t0
                self._dbg(loop_ms=round(dt * 1000.0, 2))
                interval = float(self.config.get("timing", "loop_interval", default=0.004))
                rest = interval - dt
                if rest > 0:
                    self._stop.wait(rest)
        finally:
            self.safety_release()
            self.log("Engine-Thread beendet")

    def _step(self) -> None:
        cfg = self.config.data  # jede Iteration frisch (Live-Änderungen aus GUI)
        mods, timing, pts = cfg["modules"], cfg["timing"], cfg["points"]
        now = time.perf_counter()
        s = self.screen.scale()

        # ---- EIN Lesevorgang für alle Einzelpunkte -----------------------
        req: List[Tuple[float, float]] = []
        for n in ("light_box_1", "light_box_2", "light_lamp", "shift_circle"):
            req.append(self.screen.rel_to_phys(pts[n]["x"], pts[n]["y"]))
        btn_slices: Dict[str, Tuple[int, int]] = {}
        if mods.get("auto_skip"):
            reclick = max(float(timing.get("menu_click_delay", 0.5)), float(timing.get("button_reclick_cooldown", 1.5)))
            for n in self.MENU_BUTTONS:
                p = pts.get(n)
                if not self._pt_ok(p) or now - self._last_click.get(n, -1e9) < reclick:
                    continue
                sm = self._button_samples(p, s)
                btn_slices[n] = (len(req), len(req) + len(sm))
                req.extend(sm)
        pix = self.screen.get_pixels_phys(req)
        b1, b2, lamp, shift_px = pix[0], pix[1], pix[2], pix[3]

        # ---- 1. Menü (Auto-Skip) ---------------------------------------------
        # Priorität 1: Immer zuerst ausschließlich die 3 grünen Buttons prüfen
        # (WEITER, NEXT STAGE, RENNEN). Mindestens 3 / alle Sample-Punkte im Button
        # müssen gleichzeitig denselben flachen grünen UI-Farbton haben.
        for n in self.GREEN_MENU_BUTTONS:
            if n not in btn_slices:
                continue
            a, b = btn_slices[n]
            tol = float(pts[n].get("tol", 45))
            samples = pix[a:b]
            if all(self._is_button_green(p, cfg, tol) for p in samples) and self._is_flat_ui_color(samples):
                self._click_button(n, cfg)
                return

        # Priorität 2: Nur wenn an keiner der 3 grünen Button-Positionen Grün erkannt wird,
        # die Position des roten GIVE UP-Buttons prüfen.
        if "Give_Up_Button" in btn_slices:
            a, b = btn_slices["Give_Up_Button"]
            tol = float(pts["Give_Up_Button"].get("tol", 45))
            samples = pix[a:b]
            if all(self._is_give_up_red(p, cfg, tol) for p in samples) and self._is_flat_ui_color(samples):
                self._click_button("Give_Up_Button", cfg)
                return

        # ---- 2. Ampel --------------------------------------------------------
        visible, ltype = self._eval_light(cfg, b1, b2, lamp)
        self._dbg(light_type=ltype)
        if visible:
            if not self._light_seen:
                self._zone_cache = (-1e9, None)  # neue Ampel -> blaue Zone sofort neu vermessen
            self._light_seen = True
            self._light_missing = 0
            self._await_light = False
            self._set_state(BotState.AT_LIGHT, "Ampel %s" % ltype)
            if mods.get("auto_start"):
                self._throttle_at_light(cfg)
            else:
                self._release_w()
            return

        # ---- 3. GO -----------------------------------------------------------
        if self._light_seen:
            self._light_missing += 1
            if self._light_missing < int(timing.get("go_confirm_frames", 3)):
                return
            self._light_seen = False
            self._light_missing = 0
            self.log("GO!")
            if mods.get("auto_start"):
                self._hold_w()
            self._set_state(BotState.RACING, "GO")
            return

        state = self.state
        # ---- 4. RACING -------------------------------------------------------
        if state == BotState.RACING:
            if mods.get("auto_start"):
                self._hold_w()  # erneut drücken, falls z.B. nach Modulwechsel losgelassen
            self._check_shift(cfg, shift_px, now)
            return

        # ---- 5. MENU_SKIP halten / WAITING ------------------------------------
        if state == BotState.MENU_SKIP and (self._await_light or now - self._last_click_time < self.MENU_HOLD_S):
            return
        self._set_state(BotState.WAITING)
        self._check_shift(cfg, shift_px, now)

    def _check_shift(self, cfg: dict, shift_px: RGB, now: float) -> None:
        if not cfg["modules"].get("auto_shift"):
            return
        p = cfg["points"].get("shift_circle")
        if not self._pt_ok(p):
            return
        if now - self._last_shift < float(cfg["timing"].get("shift_cooldown", 0.25)):
            return
        if self._is_shift_green(shift_px, cfg, float(p.get("tol", 50))):
            self._shift(cfg["timing"])

    def _throttle_at_light(self, cfg: dict) -> None:
        nd = cfg["needle"]
        timing = cfg["timing"]
        start = float(nd.get("start_angle_deg", 240.0))
        end = float(nd.get("end_angle_deg", -80.0))
        angle = self._read_needle(cfg)
        target = self._target_angle(cfg)
        pn = needle_progress(angle, start, end) if angle is not None else None
        pt = needle_progress(target, start, end) if target is not None else None
        zone = self._zone_cache[1]
        self._dbg(
            zone=None if zone is None else "%.0f..%.0f" % zone,
            needle_angle=None if angle is None else round(angle, 1),
            target_angle=None if target is None else round(target, 1),
            needle_progress=None if pn is None else round(pn, 1),
            target_progress=None if pt is None else round(pt, 1),
            cw_angle=None if pn is None else round(pn, 1),
            cw_target=None if pt is None else round(pt, 1),
        )
        if pn is None or pt is None:
            self._release_w()  # Nadel/Ziel nicht erkannt -> kein Gas
            return

        angle_tolerance = float(nd.get("angle_tolerance", 2.0))
        slow_zone_angle = max(float(nd.get("slow_zone_angle", 15.0)), angle_tolerance + 0.1)
        fine_tap_duration = float(timing.get("fine_tap_duration", timing.get("tap_duration", 0.014)))
        fine_tap_cooldown = float(timing.get("fine_tap_cooldown", timing.get("tap_cooldown", 0.025)))
        boost_tap_duration = float(timing.get("boost_tap_duration", 0.035))
        boost_tap_cooldown = float(timing.get("boost_tap_cooldown", 0.012))

        diff = pt - pn
        if diff <= angle_tolerance:
            # Fall 1: Nadel ist über oder im Zielbereich (pn >= pt - angle_tolerance) -> sofort W loslassen!
            self._release_w()
        elif diff <= slow_zone_angle:
            # Fall 2: Nadel ist knapp unter dem Zielbereich (pt - slow_zone_angle <= pn < pt - angle_tolerance)
            span = max(slow_zone_angle - angle_tolerance, 1e-6)
            ratio = min(max((diff - angle_tolerance) / span, 0.0), 1.0)
            dur = fine_tap_duration * (0.75 + 0.25 * ratio)
            self._micro_tap(timing, duration=dur, cooldown=fine_tap_cooldown)
        else:
            # Fall 3: Nadel ist noch weit links/unten (pn < pt - slow_zone_angle)
            self._micro_tap(timing, duration=boost_tap_duration, cooldown=boost_tap_cooldown)


# =============================================================================
# GUI (tkinter + ttk Dark-Theme) – läuft im Main-Thread, Engine im Daemon-Thread
# tkinter wird erst in BotGUI.__init__() importiert, damit core_utils auch
# ohne Display/Tk importierbar bleibt.
# =============================================================================
import queue as _gui_queue
import threading as _gui_threading
import time as _gui_time
import traceback as _gui_traceback

GUI_POLL_MS = 80
GUI_MAX_LOG_LINES = 1500
GUI_CALIBRATE_COUNTDOWN = 3

GUI_THEME = {
    "bg_root": "#12141a",
    "bg_card": "#181a20",
    "bg_card_alt": "#1b1f2a",
    "bg_elevated": "#22252e",
    "bg_input": "#14171f",
    "border": "#2d3343",
    "border_active": "#3b82f6",
    "fg_primary": "#f1f5f9",
    "fg_secondary": "#94a3b8",
    "fg_muted": "#64748b",
    "accent_blue": "#3b82f6",
    "accent_blue_hover": "#2563eb",
    "accent_green": "#10b981",
    "accent_green_hover": "#059669",
    "accent_red": "#ef4444",
    "accent_red_hover": "#dc2626",
    "accent_amber": "#f59e0b",
    "accent_purple": "#8b5cf6",
    "accent_cyan": "#06b6d4",
}

GUI_STATE_COLORS = {
    "PAUSED": ("#64748b", "#ffffff"),
    "WAITING": ("#3b82f6", "#ffffff"),
    "AT_LIGHT": ("#f59e0b", "#0f172a"),
    "RACING": ("#10b981", "#052e16"),
    "MENU_SKIP": ("#8b5cf6", "#ffffff"),
    "STOPPED": ("#ef4444", "#ffffff"),
}

GUI_RUN_BADGE_COLORS = {
    "RUNNING": ("#10b981", "#052e16"),
    "PAUSED": ("#64748b", "#ffffff"),
    "STOPPED": ("#ef4444", "#ffffff"),
}

GUI_MODULES = [
    ("auto_start", "Auto-Start (Launch Control)", "Hält Nadel in blauer Zone & gibt Vollgas bei GO"),
    ("auto_shift", "Auto-Shift (Perfect Gear)", "Drückt E exakt bei grünem Schalt-Kreis"),
    ("auto_skip", "Auto-Skip (Menü & Give Up Loop)", "Klickt WEITER, GIVE UP, NEXT STAGE & RENNEN"),
]

# Kategorisierte Parameter-Definitionen:
# (category_id, section, key, label, typ, lo, hi, default_val, step, fmt, description, sync_key)
GUI_PARAM_SPECS = [
    # --- Kategorie 1: Start & Tacho-Tuning ---
    (
        "tacho",
        "timing",
        "fine_tap_duration",
        "Fine Tap Duration (s)",
        float,
        0.002,
        0.100,
        0.014,
        0.001,
        "%.3f",
        "Kurzer Gas-Impuls nahe der Zielzone (synchron mit tap_duration)",
        ("timing", "tap_duration"),
    ),
    (
        "tacho",
        "timing",
        "fine_tap_cooldown",
        "Fine Tap Cooldown (s)",
        float,
        0.001,
        0.150,
        0.025,
        0.001,
        "%.3f",
        "Pause zwischen Fine-Taps zum Ausrollen (synchron mit tap_cooldown)",
        ("timing", "tap_cooldown"),
    ),
    (
        "tacho",
        "timing",
        "boost_tap_duration",
        "Boost Tap Duration (s)",
        float,
        0.010,
        0.150,
        0.035,
        0.001,
        "%.3f",
        "Kräftiger Gas-Impuls, wenn Nadel noch weit unter Ziel ist",
        None,
    ),
    (
        "tacho",
        "timing",
        "boost_tap_cooldown",
        "Boost Tap Cooldown (s)",
        float,
        0.001,
        0.100,
        0.012,
        0.001,
        "%.3f",
        "Kurze Pause zwischen Boost-Taps beim schnellen Hochdrehen",
        None,
    ),
    (
        "tacho",
        "needle",
        "angle_tolerance",
        "Angle Tolerance (°)",
        float,
        0.5,
        15.0,
        2.0,
        0.1,
        "%.1f",
        "Wie früh vor dem Zielwinkel bereits das Gas weggenommen wird",
        None,
    ),
    (
        "tacho",
        "needle",
        "slow_zone_angle",
        "Slow Zone Angle (°)",
        float,
        5.0,
        45.0,
        15.0,
        0.5,
        "%.1f",
        "Abstand zum Zielwinkel für Übergang von Boost- zu Fine-Tap",
        None,
    ),
    (
        "tacho",
        "needle",
        "target_offset_deg",
        "Target Angle Offset (°)",
        float,
        -30.0,
        30.0,
        0.0,
        0.5,
        "%.1f",
        "Manuelle Feinjustierung des Zielwinkels in Grad",
        None,
    ),
    (
        "tacho",
        "needle",
        "zone_target_fraction",
        "Position in Blue Zone (0..1)",
        float,
        0.10,
        0.90,
        0.45,
        0.01,
        "%.2f",
        "Zielposition innerhalb der blauen Zone (0.1=Anfang, 0.5=Mitte, 0.9=Ende)",
        None,
    ),
    # --- Kategorie 2: Blue Zone & Needle RGB Thresholds ---
    (
        "rgb",
        "needle",
        "white_min",
        "Needle White Min (RGB)",
        int,
        150,
        250,
        215,
        1,
        "%d",
        "Minimaler RGB-Helligkeitswert für die weiße Tachonadel",
        None,
    ),
    (
        "rgb",
        "needle",
        "white_max_spread",
        "Needle White Max Spread",
        int,
        10,
        80,
        35,
        1,
        "%d",
        "Maximal erlaubte Differenz max(R,G,B) - min(R,G,B) für Weiß",
        None,
    ),
    (
        "rgb",
        "needle",
        "zone_r_max",
        "Blue Zone R Max",
        int,
        80,
        220,
        165,
        1,
        "%d",
        "Maximaler Rot-Anteil für die hellblaue Zielzone am Tacho-Ring",
        None,
    ),
    (
        "rgb",
        "needle",
        "zone_g_min",
        "Blue Zone G Min",
        int,
        120,
        250,
        180,
        1,
        "%d",
        "Minimaler Grün-Anteil für die hellblaue Zielzone",
        None,
    ),
    (
        "rgb",
        "needle",
        "zone_b_min",
        "Blue Zone B Min",
        int,
        150,
        255,
        210,
        1,
        "%d",
        "Minimaler Blau-Anteil für die hellblaue Zielzone",
        None,
    ),
    # --- Kategorie 3: Schalten & Menü-Timings ---
    (
        "shift",
        "timing",
        "shift_cooldown",
        "Shift Cooldown (s)",
        float,
        0.01,
        2.0,
        0.25,
        0.01,
        "%.2f",
        "Mindestabstand zwischen zwei Schaltvorgängen (Taste E)",
        None,
    ),
    (
        "shift",
        "timing",
        "shift_tap_duration",
        "Shift Tap Duration (s)",
        float,
        0.005,
        0.20,
        0.030,
        0.005,
        "%.3f",
        "Haltedauer der Taste E beim Schalten",
        None,
    ),
    (
        "shift",
        "timing",
        "shift_green_threshold",
        "Shift Green Threshold",
        int,
        60,
        220,
        100,
        1,
        "%d",
        "Grün-Empfindlichkeit (Mindest-G-Wert) für den Schalt-Kreis",
        None,
    ),
    (
        "shift",
        "timing",
        "menu_click_delay",
        "Menu Click Delay (s)",
        float,
        0.05,
        5.0,
        0.50,
        0.05,
        "%.2f",
        "Wartezeit nach Klicks auf WEITER, GIVE UP, NEXT STAGE, RENNEN",
        None,
    ),
    (
        "shift",
        "timing",
        "loop_interval",
        "Loop Interval (s)",
        float,
        0.001,
        0.050,
        0.004,
        0.001,
        "%.3f",
        "Ziel-Intervall der Hauptschleife (0.004s = ~250 Hz)",
        None,
    ),
    (
        "shift",
        "timing",
        "go_confirm_frames",
        "GO Confirm Frames",
        int,
        1,
        20,
        3,
        1,
        "%d",
        "Anzahl Frames ohne Ampel bis GO ausgelöst wird",
        None,
    ),
]

# Abwärtskompatible Liste (Sektion, Key, Label, Typ, Min, Max)
GUI_PARAM_FIELDS = [
    (sec, key, label, typ, lo, hi)
    for (_cat, sec, key, label, typ, lo, hi, _def, _step, _fmt, _desc, _sync) in GUI_PARAM_SPECS
]

POINT_DESCRIPTIONS = {
    "light_box_1": "Ampelkasten links (Dunkelgrau / Boss-Rot)",
    "light_box_2": "Ampelkasten rechts (Dunkelgrau / Boss-Rot)",
    "light_lamp": "Rote Ampel-Lampe oben",
    "tacho_center": "Drehpunkt der Tachonadel (Mitte)",
    "needle_target": "Fallback-Zielpunkt in blauer Zone",
    "shift_circle": "Schalt-Kreis (wird grün beim Schalten)",
    "btn_weiter": "Grüner WEITER-Button nach Rennen",
    "btn_next_stage": "Grüner NEXT STAGE-Button",
    "btn_rennen": "Grüner RENNEN-Button (Start)",
    "Give_Up_Button": "Roter GIVE UP-Button (Streak Over)",
}

QUICK_CALIBRATE_POINTS = [
    ("tacho_center", "🎯 Tacho-Mitte"),
    ("needle_target", "🎯 Ziel-Zone"),
    ("shift_circle", "🎯 Shift-Kreis"),
    ("Give_Up_Button", "🎯 Give Up"),
    ("light_box_1", "🎯 Ampel 1"),
    ("btn_weiter", "🎯 Weiter"),
    ("btn_next_stage", "🎯 Next Stage"),
    ("btn_rennen", "🎯 Rennen"),
]


def _gui_state_name(state: Any) -> str:
    """BotState (str-Enum) oder String -> 'PAUSED' etc."""
    if state is None:
        return "?"
    name = getattr(state, "name", None)
    if name:
        return str(name)
    s = str(state)
    if "." in s:
        s = s.split(".")[-1]
    return s.upper()


def _gui_format_debug(dbg: Any) -> str:
    if not dbg:
        return "Debug: –"
    parts = []
    try:
        items = list(dbg.items())
    except Exception:
        return "Debug: %r" % (dbg,)
    for k, v in items:
        if isinstance(v, float):
            v = "%.1f" % v
        elif v is None:
            v = "–"
        parts.append("%s=%s" % (k, v))
    return "Debug: " + "  |  ".join(parts)


def _rgb_to_hex(rgb: Any) -> str:
    try:
        if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
            r = min(max(int(rgb[0]), 0), 255)
            g = min(max(int(rgb[1]), 0), 255)
            b = min(max(int(rgb[2]), 0), 255)
            return "#%02x%02x%02x" % (r, g, b)
    except Exception:
        pass
    return "#334155"


class BotGUI(object):
    """Moderne Dark-Theme tkinter-Oberfläche für die BotEngine.

    Thread-safe Methoden (aus Hotkey-/Engine-Threads aufrufbar):
        log(msg), request(callable), calibrate_selected(),
        toggle_pause(), emergency(), shutdown()
    Alle anderen Methoden nur im Tk-Main-Thread benutzen.
    """

    def __init__(self, engine: Any, config: Any, platform_name: str, on_close: Optional[Callable[[], Any]] = None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.engine = engine
        self.config = config
        self.platform_name = platform_name
        self.on_close = on_close

        self._log_queue: _gui_queue.Queue = _gui_queue.Queue()
        self._req_queue: _gui_queue.Queue = _gui_queue.Queue()
        self._closed = False
        self._countdown_active = False
        self._last_state: Optional[str] = None
        self._last_debug_text: Optional[str] = None
        self._syncing_vars = False
        self._main_thread = _gui_threading.current_thread()

        self._spec_map: Dict[Tuple[str, str], tuple] = {}
        self._sync_pairs: Dict[Tuple[str, str], Tuple[str, str]] = {}
        for spec in GUI_PARAM_SPECS:
            _cat, sec, key, _lbl, _typ, _lo, _hi, _def, _step, _fmt, _desc, sync_key = spec
            self._spec_map[(sec, key)] = spec
            if sync_key is not None:
                self._sync_pairs[(sec, key)] = sync_key
                self._sync_pairs[sync_key] = (sec, key)

        self.root = tk.Tk()
        self.root.title("Taxi Boss Drag Bot V2 – %s" % platform_name)
        self.root.configure(bg=GUI_THEME["bg_root"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.root.geometry("940x860")
            self.root.minsize(780, 700)
        except Exception:
            pass

        self._pick_fonts()
        self._setup_styles()
        self._build()
        self.refresh_points()
        self.root.after(GUI_POLL_MS, self._poll)

    def _pick_fonts(self) -> None:
        if str(self.platform_name).lower().startswith("mac"):
            base = "Helvetica Neue"
            mono = "Menlo"
        elif str(self.platform_name).lower().startswith("win"):
            base = "Segoe UI"
            mono = "Consolas"
        else:
            base = "DejaVu Sans"
            mono = "Courier"
        self.fonts = {
            "title": (base, 17, "bold"),
            "subtitle": (base, 10),
            "badge": (base, 12, "bold"),
            "badge_sm": (base, 9, "bold"),
            "section": (base, 11, "bold"),
            "body": (base, 10),
            "body_bold": (base, 10, "bold"),
            "small": (base, 9),
            "metric_val": (mono, 13, "bold"),
            "metric_lbl": (base, 8, "bold"),
            "mono": (mono, 10),
            "mono_sm": (mono, 9),
            "btn_lg": (base, 12, "bold"),
            "btn": (base, 10, "bold"),
        }

    def _setup_styles(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        T = GUI_THEME
        style.configure(".", background=T["bg_root"], foreground=T["fg_primary"], font=self.fonts["body"])
        style.configure("TFrame", background=T["bg_root"])
        style.configure("Card.TFrame", background=T["bg_card"])
        style.configure("Elevated.TFrame", background=T["bg_elevated"])
        style.configure("TLabel", background=T["bg_card"], foreground=T["fg_primary"], font=self.fonts["body"])

        # Notebook (Tabs)
        style.configure(
            "Dark.TNotebook",
            background=T["bg_root"],
            borderwidth=0,
            tabmargins=[0, 4, 0, 0],
        )
        style.configure(
            "Dark.TNotebook.Tab",
            background=T["bg_card"],
            foreground=T["fg_secondary"],
            padding=[14, 8],
            font=self.fonts["body_bold"],
            borderwidth=1,
            lightcolor=T["border"],
            darkcolor=T["border"],
        )
        style.map(
            "Dark.TNotebook.Tab",
            background=[("selected", T["accent_blue"]), ("active", T["bg_elevated"])],
            foreground=[("selected", "#ffffff"), ("active", T["fg_primary"])],
        )

        # Buttons (ttk mit clam-Theme rendert Farben auf macOS & Windows zuverlässig)
        style.configure(
            "Start.TButton",
            background=T["accent_green"],
            foreground="#052e16",
            font=self.fonts["btn_lg"],
            padding=[16, 10],
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Start.TButton",
            background=[("active", T["accent_green_hover"]), ("disabled", T["bg_elevated"])],
            foreground=[("disabled", T["fg_muted"])],
        )

        style.configure(
            "Pause.TButton",
            background=T["accent_amber"],
            foreground="#0f172a",
            font=self.fonts["btn_lg"],
            padding=[16, 10],
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Pause.TButton",
            background=[("active", "#d97706"), ("disabled", T["bg_elevated"])],
        )

        style.configure(
            "Danger.TButton",
            background=T["accent_red"],
            foreground="#ffffff",
            font=self.fonts["btn_lg"],
            padding=[16, 10],
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Danger.TButton",
            background=[("active", T["accent_red_hover"]), ("disabled", T["bg_elevated"])],
        )

        style.configure(
            "Primary.TButton",
            background=T["accent_blue"],
            foreground="#ffffff",
            font=self.fonts["btn"],
            padding=[12, 7],
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", T["accent_blue_hover"]), ("disabled", T["bg_elevated"])],
            foreground=[("disabled", T["fg_muted"])],
        )

        style.configure(
            "Secondary.TButton",
            background=T["bg_elevated"],
            foreground=T["fg_primary"],
            font=self.fonts["btn"],
            padding=[12, 7],
            borderwidth=1,
            bordercolor=T["border"],
            lightcolor=T["border"],
            darkcolor=T["border"],
            focusthickness=0,
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#2d3343"), ("disabled", T["bg_card"])],
            foreground=[("disabled", T["fg_muted"])],
        )

        style.configure(
            "Quick.TButton",
            background=T["bg_elevated"],
            foreground=T["fg_primary"],
            font=self.fonts["small"],
            padding=[8, 5],
            borderwidth=1,
            bordercolor=T["border"],
            lightcolor=T["border"],
            darkcolor=T["border"],
            focusthickness=0,
        )
        style.map(
            "Quick.TButton",
            background=[("active", T["accent_blue"]), ("disabled", T["bg_card"])],
            foreground=[("active", "#ffffff"), ("disabled", T["fg_muted"])],
        )

        # Scale (Slider)
        style.configure(
            "Dark.Horizontal.TScale",
            background=T["bg_card"],
            troughcolor=T["bg_input"],
            bordercolor=T["border"],
            lightcolor=T["accent_blue"],
            darkcolor=T["accent_blue"],
            sliderlength=18,
            sliderthickness=14,
        )
        style.map(
            "Dark.Horizontal.TScale",
            background=[("active", T["accent_blue"])],
        )

        # Combobox
        style.configure(
            "Dark.TCombobox",
            fieldbackground=T["bg_input"],
            background=T["bg_elevated"],
            foreground=T["fg_primary"],
            arrowcolor=T["fg_primary"],
            bordercolor=T["border"],
            lightcolor=T["border"],
            darkcolor=T["border"],
            padding=5,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", T["bg_input"])],
            foreground=[("readonly", T["fg_primary"])],
            selectbackground=[("readonly", T["accent_blue"])],
            selectforeground=[("readonly", "#ffffff")],
        )

        # Treeview
        style.configure(
            "Dark.Treeview",
            background=T["bg_input"],
            fieldbackground=T["bg_input"],
            foreground=T["fg_primary"],
            rowheight=26,
            font=self.fonts["mono_sm"],
            borderwidth=1,
            bordercolor=T["border"],
            lightcolor=T["border"],
            darkcolor=T["border"],
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=T["bg_elevated"],
            foreground=T["fg_secondary"],
            font=self.fonts["body_bold"],
            padding=[8, 5],
            borderwidth=1,
            bordercolor=T["border"],
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", T["accent_blue"])],
            foreground=[("selected", "#ffffff")],
        )
        style.map(
            "Dark.Treeview.Heading",
            background=[("active", "#2d3343")],
        )

    def _make_card(self, parent: Any, bg: Optional[str] = None, border: Optional[str] = None) -> Any:
        T = GUI_THEME
        return self.tk.Frame(
            parent,
            bg=bg or T["bg_card"],
            highlightbackground=border or T["border"],
            highlightcolor=border or T["border"],
            highlightthickness=1,
            bd=0,
        )

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        tk, ttk = self.tk, self.ttk
        T = GUI_THEME
        root = self.root

        main_wrap = tk.Frame(root, bg=T["bg_root"])
        main_wrap.pack(fill="both", expand=True, padx=12, pady=10)

        # =====================================================================
        # 1. HEADER CARD (Titel + Plattform-Badge + Status-Badges + Telemetrie)
        # =====================================================================
        header_card = self._make_card(main_wrap, bg=T["bg_card_alt"])
        header_card.pack(fill="x", pady=(0, 8))

        hdr_top = tk.Frame(header_card, bg=T["bg_card_alt"])
        hdr_top.pack(fill="x", padx=14, pady=(10, 8))

        # Left: Title & Platform Badge
        title_box = tk.Frame(hdr_top, bg=T["bg_card_alt"])
        title_box.pack(side="left", anchor="w")

        title_row = tk.Frame(title_box, bg=T["bg_card_alt"])
        title_row.pack(anchor="w")
        tk.Label(
            title_row,
            text="TAXI BOSS DRAG BOT",
            font=self.fonts["title"],
            bg=T["bg_card_alt"],
            fg=T["fg_primary"],
        ).pack(side="left")

        tk.Label(
            title_row,
            text=" %s " % self.platform_name.upper(),
            font=self.fonts["badge_sm"],
            bg=T["accent_blue"],
            fg="#ffffff",
            padx=6,
            pady=2,
        ).pack(side="left", padx=(10, 0))

        tk.Label(
            title_box,
            text="V2 Launch-Control, Blue-Zone Auto-Tacho, Perfect Shift & Auto-Loop",
            font=self.fonts["subtitle"],
            bg=T["bg_card_alt"],
            fg=T["fg_secondary"],
        ).pack(anchor="w", pady=(2, 0))

        # Right: Status Badges (Badge 1: RUNNING/PAUSED/STOPPED, Badge 2: CURRENT STATE)
        badges_box = tk.Frame(hdr_top, bg=T["bg_card_alt"])
        badges_box.pack(side="right", anchor="e")

        b1_wrap = tk.Frame(badges_box, bg=T["bg_card_alt"])
        b1_wrap.pack(side="left", padx=(0, 10))
        tk.Label(
            b1_wrap,
            text="BOT STATUS",
            font=self.fonts["metric_lbl"],
            bg=T["bg_card_alt"],
            fg=T["fg_muted"],
        ).pack(anchor="e")
        self.run_badge = tk.Label(
            b1_wrap,
            text="PAUSED",
            font=self.fonts["badge"],
            bg=GUI_RUN_BADGE_COLORS["PAUSED"][0],
            fg=GUI_RUN_BADGE_COLORS["PAUSED"][1],
            padx=14,
            pady=5,
            width=10,
        )
        self.run_badge.pack(anchor="e", pady=(2, 0))

        b2_wrap = tk.Frame(badges_box, bg=T["bg_card_alt"])
        b2_wrap.pack(side="left")
        tk.Label(
            b2_wrap,
            text="CURRENT STATE",
            font=self.fonts["metric_lbl"],
            bg=T["bg_card_alt"],
            fg=T["fg_muted"],
        ).pack(anchor="e")
        self.status_label = tk.Label(
            b2_wrap,
            text="PAUSED",
            font=self.fonts["badge"],
            bg=GUI_STATE_COLORS["PAUSED"][0],
            fg=GUI_STATE_COLORS["PAUSED"][1],
            padx=14,
            pady=5,
            width=12,
        )
        self.status_label.pack(anchor="e", pady=(2, 0))

        # --- Live Tacho Telemetry Strip ---
        telem_frame = tk.Frame(
            header_card,
            bg=T["bg_card"],
            highlightbackground=T["border"],
            highlightthickness=1,
        )
        telem_frame.pack(fill="x", padx=14, pady=(0, 10))

        metrics_row = tk.Frame(telem_frame, bg=T["bg_card"])
        metrics_row.pack(fill="x", padx=10, pady=(6, 4))

        self.telem_vars = {
            "needle": tk.StringVar(value="–"),
            "target": tk.StringVar(value="–"),
            "zone": tk.StringVar(value="–"),
            "light": tk.StringVar(value="–"),
            "loop": tk.StringVar(value="–"),
        }
        telem_defs = [
            ("needle", "NADEL-WINKEL", T["fg_primary"]),
            ("target", "ZIEL-WINKEL", T["accent_green"]),
            ("zone", "BLAUE ZONE", T["accent_cyan"]),
            ("light", "AMPEL-TYP", T["accent_amber"]),
            ("loop", "LOOP-ZEIT", T["fg_secondary"]),
        ]
        for idx, (k, title_txt, col) in enumerate(telem_defs):
            cell = tk.Frame(metrics_row, bg=T["bg_elevated"], padx=10, pady=4)
            cell.pack(side="left", fill="x", expand=True, padx=(0 if idx == 0 else 6, 0))
            tk.Label(
                cell,
                text=title_txt,
                font=self.fonts["metric_lbl"],
                bg=T["bg_elevated"],
                fg=T["fg_muted"],
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                cell,
                textvariable=self.telem_vars[k],
                font=self.fonts["metric_val"],
                bg=T["bg_elevated"],
                fg=col,
                anchor="w",
            ).pack(fill="x")

        # Visual Tacho Progress Bar (Canvas)
        bar_wrap = tk.Frame(telem_frame, bg=T["bg_card"])
        bar_wrap.pack(fill="x", padx=10, pady=(2, 6))
        self.tacho_canvas = tk.Canvas(
            bar_wrap,
            height=24,
            bg=T["bg_input"],
            highlightthickness=1,
            highlightbackground=T["border"],
            bd=0,
        )
        self.tacho_canvas.pack(fill="x")
        self.tacho_canvas.bind("<Configure>", lambda _e: self._draw_tacho_bar(getattr(self.engine, "last_debug", None)))

        # Compact raw debug line (also preserves self.debug_var compatibility)
        self.debug_var = tk.StringVar(value="Debug: –")
        tk.Label(
            telem_frame,
            textvariable=self.debug_var,
            anchor="w",
            font=self.fonts["mono_sm"],
            bg=T["bg_card"],
            fg=T["fg_muted"],
            padx=10,
            pady=2,
        ).pack(fill="x")

        # =====================================================================
        # 2. CONTROL BAR & LIVE MODULE TOGGLES
        # =====================================================================
        ctrl_card = self._make_card(main_wrap)
        ctrl_card.pack(fill="x", pady=(0, 8))

        ctrl_row = tk.Frame(ctrl_card, bg=T["bg_card"])
        ctrl_row.pack(fill="x", padx=12, pady=(8, 6))

        self.btn_toggle = ttk.Button(
            ctrl_row,
            text="▶ START (F9)",
            style="Start.TButton",
            command=self.toggle_pause,
        )
        self.btn_toggle.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_stop = ttk.Button(
            ctrl_row,
            text="⛔ NOT-AUS (F10)",
            style="Danger.TButton",
            command=self.emergency,
        )
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=(0, 10))

        self.topmost_var = tk.BooleanVar(value=False)
        topmost_cb = tk.Checkbutton(
            ctrl_row,
            text="📌 Immer im Vordergrund",
            variable=self.topmost_var,
            command=self._on_topmost,
            bg=T["bg_elevated"],
            fg=T["fg_primary"],
            activebackground=T["bg_elevated"],
            activeforeground=T["fg_primary"],
            selectcolor=T["bg_input"],
            font=self.fonts["body_bold"],
            padx=10,
            pady=6,
            bd=0,
            highlightthickness=1,
            highlightbackground=T["border"],
        )
        topmost_cb.pack(side="right")

        # Module Toggle Cards (3 Columns)
        mod_row = tk.Frame(ctrl_card, bg=T["bg_card"])
        mod_row.pack(fill="x", padx=12, pady=(2, 10))

        self.module_vars: Dict[str, Any] = {}
        self._module_cards: Dict[str, Any] = {}
        self._module_badges: Dict[str, Any] = {}

        for idx, (key, title_txt, desc_txt) in enumerate(GUI_MODULES):
            val = bool(self.config.get("modules", key, default=True))
            var = tk.BooleanVar(value=val)
            self.module_vars[key] = var

            mcard = tk.Frame(
                mod_row,
                bg=T["bg_elevated"],
                highlightbackground=T["accent_green"] if val else T["border"],
                highlightthickness=1,
                padx=10,
                pady=6,
            )
            mcard.pack(side="left", fill="both", expand=True, padx=(0 if idx == 0 else 8, 0))
            self._module_cards[key] = mcard

            top_line = tk.Frame(mcard, bg=T["bg_elevated"])
            top_line.pack(fill="x")

            cb = tk.Checkbutton(
                top_line,
                text=title_txt,
                variable=var,
                command=lambda k=key: self._on_module(k),
                bg=T["bg_elevated"],
                fg=T["fg_primary"],
                activebackground=T["bg_elevated"],
                activeforeground=T["fg_primary"],
                selectcolor=T["bg_input"],
                font=self.fonts["body_bold"],
                anchor="w",
                bd=0,
                highlightthickness=0,
            )
            cb.pack(side="left", fill="x", expand=True)

            state_pill = tk.Label(
                top_line,
                text="AKTIV" if val else "AUS",
                font=self.fonts["metric_lbl"],
                bg=T["accent_green"] if val else T["bg_input"],
                fg="#052e16" if val else T["fg_muted"],
                padx=6,
                pady=1,
            )
            state_pill.pack(side="right")
            self._module_badges[key] = state_pill

            lbl_desc = tk.Label(
                mcard,
                text=desc_txt,
                font=self.fonts["small"],
                bg=T["bg_elevated"],
                fg=T["fg_secondary"],
                anchor="w",
            )
            lbl_desc.pack(fill="x", pady=(2, 0))

        # =====================================================================
        # 3. NOTEBOOK (TABS FÜR ALLE PARAMETER & KALIBRIERUNG)
        # =====================================================================
        mid_wrap = tk.Frame(main_wrap, bg=T["bg_root"])
        mid_wrap.pack(fill="both", expand=True, pady=(0, 8))

        self.notebook = ttk.Notebook(mid_wrap, style="Dark.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        self.param_vars: Dict[Tuple[str, str], Any] = {}
        self.slider_vars: Dict[Tuple[str, str], Any] = {}

        # Tab 1: Start & Tacho-Tuning
        tab_tacho = tk.Frame(self.notebook, bg=T["bg_card"])
        self.notebook.add(tab_tacho, text="🚀 Start & Tacho-Tuning")
        self._build_param_category(
            tab_tacho,
            "tacho",
            "Start-Automatik & Nadel-Regelung (Dual-Mode: Boost-Tap + Fine-Tap)",
        )

        # Tab 2: Blue Zone & Needle RGB Thresholds
        tab_rgb = tk.Frame(self.notebook, bg=T["bg_card"])
        self.notebook.add(tab_rgb, text="🎨 Blue Zone & Needle RGB")
        self._build_param_category(
            tab_rgb,
            "rgb",
            "Farb-Schwellenwerte für weiße Tachonadel & hellblaue Zielzone",
        )

        # Tab 3: Schalten & Menü-Timings
        tab_shift = tk.Frame(self.notebook, bg=T["bg_card"])
        self.notebook.add(tab_shift, text="⚡ Schalten & Menü-Timings")
        self._build_param_category(
            tab_shift,
            "shift",
            "Schalt-Erkennung (Grüner Kreis), Menü-Auto-Skip & Engine-Taktrate",
        )

        # Legacy-Aliases für ("timing", "tap_duration") und ("timing", "tap_cooldown")
        self._setup_legacy_param_aliases()

        # Tab 4: Kalibrierung & Punkte
        tab_cal = tk.Frame(self.notebook, bg=T["bg_card"])
        self.notebook.add(tab_cal, text="🎯 Kalibrierung & Punkte")
        self._build_calibration_tab(tab_cal)

        # --- Global Parameter Action Bar ---
        action_bar = self._make_card(mid_wrap, bg=T["bg_card_alt"])
        action_bar.pack(fill="x", pady=(6, 0))
        act_inner = tk.Frame(action_bar, bg=T["bg_card_alt"])
        act_inner.pack(fill="x", padx=12, pady=7)

        self.btn_save = ttk.Button(
            act_inner,
            text="💾 Alle Parameter speichern",
            style="Primary.TButton",
            command=self.save_params,
        )
        self.btn_save.pack(side="left", padx=(0, 8))

        self.btn_reload = ttk.Button(
            act_inner,
            text="↺ Neu laden",
            style="Secondary.TButton",
            command=self.reload_params,
        )
        self.btn_reload.pack(side="left", padx=(0, 8))

        self.btn_reset = ttk.Button(
            act_inner,
            text="⟲ Auf Standardwerte zurücksetzen",
            style="Secondary.TButton",
            command=self.reset_to_defaults,
        )
        self.btn_reset.pack(side="left")

        self.save_status_var = tk.StringVar(value="✓ Live-Sync aktiv (Änderungen wirken sofort)")
        tk.Label(
            act_inner,
            textvariable=self.save_status_var,
            font=self.fonts["small"],
            bg=T["bg_card_alt"],
            fg=T["accent_green"],
        ).pack(side="right")

        # =====================================================================
        # 4. LIVE LOG PANEL (Unten angedockt)
        # =====================================================================
        log_card = self._make_card(main_wrap)
        log_card.pack(fill="x")

        log_hdr = tk.Frame(log_card, bg=T["bg_card"])
        log_hdr.pack(fill="x", padx=12, pady=(6, 2))
        tk.Label(
            log_hdr,
            text="📋 LIVE-EREIGNIS-LOG",
            font=self.fonts["metric_lbl"],
            bg=T["bg_card"],
            fg=T["fg_secondary"],
        ).pack(side="left")
        ttk.Button(
            log_hdr,
            text="Log leeren",
            style="Quick.TButton",
            command=self._clear_log,
        ).pack(side="right")

        log_body = tk.Frame(log_card, bg=T["bg_card"])
        log_body.pack(fill="both", expand=True, padx=12, pady=(2, 8))

        self.log_text = tk.Text(
            log_body,
            height=6,
            state="disabled",
            font=self.fonts["mono_sm"],
            bg=T["bg_input"],
            fg=T["fg_primary"],
            insertbackground=T["fg_primary"],
            selectbackground=T["accent_blue"],
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
            wrap="word",
        )
        sb = tk.Scrollbar(log_body, command=self.log_text.yview, bg=T["bg_elevated"])
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _get_config_param(self, sec: str, key: str, default_val: Any, sync_key: Optional[Tuple[str, str]] = None) -> Any:
        val = self.config.get(sec, key, default=None)
        if val is None and sync_key is not None:
            val = self.config.get(sync_key[0], sync_key[1], default=None)
        if val is None:
            val = default_val
        return val

    def _format_val(self, val: Any, typ: type, fmt: str) -> str:
        try:
            if typ is int:
                return fmt % int(round(float(val)))
            return fmt % float(val)
        except Exception:
            return str(val)

    def _build_param_category(self, parent: Any, cat_id: str, subtitle: str) -> None:
        tk, ttk = self.tk, self.ttk
        T = GUI_THEME

        hdr = tk.Frame(parent, bg=T["bg_card"])
        hdr.pack(fill="x", padx=14, pady=(10, 6))
        tk.Label(
            hdr,
            text=subtitle,
            font=self.fonts["body_bold"],
            bg=T["bg_card"],
            fg=T["fg_secondary"],
            anchor="w",
        ).pack(side="left")
        tk.Label(
            hdr,
            text="Slider ziehen oder Wert eingeben + Enter",
            font=self.fonts["small"],
            bg=T["bg_card"],
            fg=T["fg_muted"],
            anchor="e",
        ).pack(side="right")

        grid_frame = tk.Frame(parent, bg=T["bg_card"])
        grid_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        grid_frame.columnconfigure(1, weight=1)

        specs = [s for s in GUI_PARAM_SPECS if s[0] == cat_id]
        for row_idx, spec in enumerate(specs):
            _cat, sec, key, label, typ, lo, hi, default_val, step, fmt, desc, sync_key = spec
            raw_val = self._get_config_param(sec, key, default_val, sync_key)
            formatted_init = self._format_val(raw_val, typ, fmt)

            row_bg = T["bg_elevated"] if row_idx % 2 == 0 else T["bg_card_alt"]
            row_box = tk.Frame(
                grid_frame,
                bg=row_bg,
                highlightbackground=T["border"],
                highlightthickness=1,
                padx=10,
                pady=5,
            )
            row_box.grid(row=row_idx, column=0, columnspan=4, sticky="ew", pady=2)
            row_box.columnconfigure(1, weight=1)

            # Column 0: Label & Description
            lbl_col = tk.Frame(row_box, bg=row_bg, width=260)
            lbl_col.grid(row=0, column=0, sticky="w", padx=(0, 10))
            tk.Label(
                lbl_col,
                text=label,
                font=self.fonts["body_bold"],
                bg=row_bg,
                fg=T["fg_primary"],
                anchor="w",
            ).pack(fill="x")
            tk.Label(
                lbl_col,
                text=desc,
                font=self.fonts["small"],
                bg=row_bg,
                fg=T["fg_muted"],
                anchor="w",
            ).pack(fill="x")

            # Column 1: Slider
            try:
                num_init = float(raw_val)
            except Exception:
                num_init = float(default_val)
            s_var = tk.DoubleVar(value=min(max(num_init, float(lo)), float(hi)))
            self.slider_vars[(sec, key)] = s_var

            scale = ttk.Scale(
                row_box,
                from_=float(lo),
                to=float(hi),
                orient="horizontal",
                variable=s_var,
                style="Dark.Horizontal.TScale",
                command=lambda val, s=sec, k=key: self._on_slider_move(s, k, val),
            )
            scale.grid(row=0, column=1, sticky="ew", padx=8)
            scale.bind("<ButtonRelease-1>", lambda _e, s=sec, k=key: self._on_slider_release(s, k))

            # Column 2: Spinbox / Entry
            p_var = tk.StringVar(value=formatted_init)
            self.param_vars[(sec, key)] = p_var

            spin = tk.Spinbox(
                row_box,
                from_=lo,
                to=hi,
                increment=step,
                textvariable=p_var,
                width=9,
                font=self.fonts["mono"],
                bg=T["bg_input"],
                fg=T["fg_primary"],
                insertbackground=T["fg_primary"],
                buttonbackground=T["bg_elevated"],
                relief="flat",
                highlightthickness=1,
                highlightbackground=T["border"],
                highlightcolor=T["accent_blue"],
                command=lambda s=sec, k=key: self._on_entry_commit(s, k),
            )
            spin.grid(row=0, column=2, sticky="e", padx=6)
            spin.bind("<Return>", lambda _e, s=sec, k=key: self._on_entry_commit(s, k))
            spin.bind("<FocusOut>", lambda _e, s=sec, k=key: self._on_entry_commit(s, k))

            # Column 3: Range & Default Info
            range_txt = "[%s … %s]\nStd: %s" % (
                self._format_val(lo, typ, fmt),
                self._format_val(hi, typ, fmt),
                self._format_val(default_val, typ, fmt),
            )
            tk.Label(
                row_box,
                text=range_txt,
                font=self.fonts["small"],
                bg=row_bg,
                fg=T["fg_muted"],
                justify="right",
                width=14,
                anchor="e",
            ).grid(row=0, column=3, sticky="e")

    def _setup_legacy_param_aliases(self) -> None:
        """Synchronisiert timing.tap_duration <-> timing.fine_tap_duration und
        timing.tap_cooldown <-> timing.fine_tap_cooldown für volle Rückwärtskompatibilität."""
        for primary, alias in [
            (("timing", "fine_tap_duration"), ("timing", "tap_duration")),
            (("timing", "fine_tap_cooldown"), ("timing", "tap_cooldown")),
        ]:
            p_var = self.param_vars.get(primary)
            if p_var is None:
                continue
            a_var = self.tk.StringVar(value=p_var.get())
            self.param_vars[alias] = a_var

            def _sync_p_to_a(*_args, pv=p_var, av=a_var):
                if self._syncing_vars:
                    return
                self._syncing_vars = True
                try:
                    av.set(pv.get())
                finally:
                    self._syncing_vars = False

            def _sync_a_to_p(*_args, pv=p_var, av=a_var):
                if self._syncing_vars:
                    return
                self._syncing_vars = True
                try:
                    pv.set(av.get())
                finally:
                    self._syncing_vars = False

            try:
                p_var.trace_add("write", _sync_p_to_a)
                a_var.trace_add("write", _sync_a_to_p)
            except AttributeError:
                p_var.trace("w", _sync_p_to_a)
                a_var.trace("w", _sync_a_to_p)

    def _build_calibration_tab(self, parent: Any) -> None:
        tk, ttk = self.tk, self.ttk
        T = GUI_THEME

        top_bar = tk.Frame(parent, bg=T["bg_card"])
        top_bar.pack(fill="x", padx=14, pady=(10, 6))

        tk.Label(
            top_bar,
            text="Punkt auswählen:",
            font=self.fonts["body_bold"],
            bg=T["bg_card"],
            fg=T["fg_primary"],
        ).pack(side="left", padx=(0, 6))

        names = list(POINT_NAMES)
        self.point_var = tk.StringVar(value=names[0] if names else "")
        self.point_combo = ttk.Combobox(
            top_bar,
            textvariable=self.point_var,
            values=names,
            state="readonly",
            width=18,
            style="Dark.TCombobox",
            font=self.fonts["mono"],
        )
        self.point_combo.pack(side="left", padx=(0, 8))
        self.point_combo.bind("<<ComboboxSelected>>", self._on_combo_select)

        self.btn_cal = ttk.Button(
            top_bar,
            text="🎯 Gewählten Punkt kalibrieren (3s Countdown)",
            style="Primary.TButton",
            command=self.start_calibration_countdown,
        )
        self.btn_cal.pack(side="left", padx=(0, 8))

        ttk.Button(
            top_bar,
            text="⚡ Sofort (Mauspos / F8)",
            style="Secondary.TButton",
            command=self.calibrate_selected,
        ).pack(side="left")

        # Tolerance quick-edit on right side of top_bar
        tol_box = tk.Frame(top_bar, bg=T["bg_card"])
        tol_box.pack(side="right")
        tk.Label(
            tol_box,
            text="Toleranz:",
            font=self.fonts["small"],
            bg=T["bg_card"],
            fg=T["fg_secondary"],
        ).pack(side="left", padx=(0, 4))
        self.point_tol_var = tk.StringVar(value="40")
        tol_spin = tk.Spinbox(
            tol_box,
            from_=5,
            to=150,
            increment=1,
            textvariable=self.point_tol_var,
            width=5,
            font=self.fonts["mono"],
            bg=T["bg_input"],
            fg=T["fg_primary"],
            insertbackground=T["fg_primary"],
            buttonbackground=T["bg_elevated"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=T["border"],
            command=self._on_point_tol_change,
        )
        tol_spin.pack(side="left")
        tol_spin.bind("<Return>", lambda _e: self._on_point_tol_change())
        tol_spin.bind("<FocusOut>", lambda _e: self._on_point_tol_change())

        # Quick-Calibrate Bar for key points
        quick_bar = tk.Frame(parent, bg=T["bg_card"])
        quick_bar.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(
            quick_bar,
            text="Schnell-Kalibrierung (3s):",
            font=self.fonts["small"],
            bg=T["bg_card"],
            fg=T["fg_muted"],
        ).pack(side="left", padx=(0, 6))

        for pt_name, btn_lbl in QUICK_CALIBRATE_POINTS:
            if pt_name in POINT_NAMES:
                ttk.Button(
                    quick_bar,
                    text=btn_lbl,
                    style="Quick.TButton",
                    command=lambda n=pt_name: self.start_calibration_countdown(n),
                ).pack(side="left", padx=2)

        # Info Banner
        self.cal_info = tk.StringVar(
            value="💡 Tipp: Maus im Spiel auf den gewünschten Punkt halten und F8 drücken "
            "(oder oben den 3-Sekunden-Countdown starten)."
        )
        tk.Label(
            parent,
            textvariable=self.cal_info,
            anchor="w",
            justify="left",
            font=self.fonts["small"],
            bg=T["bg_elevated"],
            fg=T["accent_cyan"],
            padx=10,
            pady=5,
        ).pack(fill="x", padx=14, pady=(0, 6))

        # Points Treeview Table
        tree_wrap = tk.Frame(parent, bg=T["bg_card"])
        tree_wrap.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        cols = ("name", "x", "y", "rgb", "tol", "desc")
        self.points_tree = ttk.Treeview(
            tree_wrap,
            columns=cols,
            show="headings",
            height=9,
            style="Dark.Treeview",
        )
        col_defs = [
            ("name", "Punkt-ID", 130, "w"),
            ("x", "Rel X", 80, "center"),
            ("y", "Rel Y", 80, "center"),
            ("rgb", "RGB-Farbe (Hex)", 165, "center"),
            ("tol", "Toleranz", 70, "center"),
            ("desc", "Funktion / Beschreibung", 280, "w"),
        ]
        for cid, heading, width, anchor in col_defs:
            self.points_tree.heading(cid, text=heading, anchor=anchor)
            self.points_tree.column(cid, width=width, anchor=anchor)

        tree_sb = tk.Scrollbar(tree_wrap, command=self.points_tree.yview, bg=T["bg_elevated"])
        self.points_tree.configure(yscrollcommand=tree_sb.set)
        tree_sb.pack(side="right", fill="y")
        self.points_tree.pack(side="left", fill="both", expand=True)
        self.points_tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    # ------------------------------------------------------ Live Slider / Spinbox
    def _apply_single_param(self, sec: str, key: str, val: Any, save_disk: bool = True, log_change: bool = False) -> None:
        spec = self._spec_map.get((sec, key))
        if spec is None:
            return
        _cat, _sec, _key, label, typ, lo, hi, _def, _step, fmt, _desc, sync_key = spec
        v = int(round(float(val))) if typ is int else float(val)
        v = min(max(v, lo), hi)
        if typ is float:
            v = float(fmt % v)

        formatted = self._format_val(v, typ, fmt)
        self._syncing_vars = True
        try:
            self.param_vars[(sec, key)].set(formatted)
            if sync_key is not None and sync_key in self.param_vars:
                self.param_vars[sync_key].set(formatted)
            if (sec, key) in self.slider_vars:
                self.slider_vars[(sec, key)].set(float(v))
        finally:
            self._syncing_vars = False

        self.config.set(sec, key, v)
        if sync_key is not None:
            self.config.set(sync_key[0], sync_key[1], v)

        if save_disk:
            try:
                self.config.save()
                self.save_status_var.set("✓ Live gespeichert: %s = %s" % (label, formatted))
            except Exception as e:
                self.log("Speicherfehler (%s.%s): %s" % (sec, key, e))
        if log_change:
            self.log("Parameter %s.%s = %s" % (sec, key, formatted))

    def _on_slider_move(self, sec: str, key: str, raw_val: Any) -> None:
        if self._syncing_vars:
            return
        try:
            self._apply_single_param(sec, key, float(raw_val), save_disk=False, log_change=False)
        except Exception:
            pass

    def _on_slider_release(self, sec: str, key: str) -> None:
        s_var = self.slider_vars.get((sec, key))
        if s_var is None:
            return
        try:
            self._apply_single_param(sec, key, float(s_var.get()), save_disk=True, log_change=True)
        except Exception:
            pass

    def _on_entry_commit(self, sec: str, key: str) -> None:
        spec = self._spec_map.get((sec, key))
        p_var = self.param_vars.get((sec, key))
        if spec is None or p_var is None:
            return
        _cat, _sec, _key, label, typ, lo, hi, _def, _step, _fmt, _desc, _sync = spec
        raw = p_var.get().strip().replace(",", ".")
        try:
            v = int(round(float(raw))) if typ is int else float(raw)
        except (ValueError, TypeError):
            self.log("Ungültige Eingabe für %s: %r" % (label, raw))
            return
        if not (lo <= v <= hi):
            v = min(max(v, lo), hi)
        self._apply_single_param(sec, key, v, save_disk=True, log_change=True)

    # ------------------------------------------------------ thread-safe API
    def log(self, msg: str) -> None:
        """Thread-safe: Nachricht ins Log-Fenster (auch vor/nach mainloop)."""
        try:
            line = "%s  %s" % (_gui_time.strftime("%H:%M:%S"), msg)
        except Exception:
            line = str(msg)
        self._log_queue.put(line)

    def request(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Thread-safe: func(*args) im Tk-Main-Thread ausführen."""
        self._req_queue.put((func, args, kwargs))

    def calibrate_selected(self) -> None:
        """Thread-safe (F8): gewählten Punkt sofort an Mausposition setzen."""
        self.request(self._do_calibrate, None)

    def toggle_pause(self) -> None:
        """Thread-safe (F9)."""
        if self._in_main():
            self._do_toggle()
        else:
            self.request(self._do_toggle)

    def emergency(self) -> None:
        """Thread-safe (F10). Ruft emergency_stop sofort auf (auch aus
        Hotkey-Threads), GUI-Update folgt per Polling."""
        try:
            self.engine.emergency_stop()
        except Exception as e:  # pragma: no cover
            self.log("Fehler bei NOT-AUS: %s" % e)
        self.log("!!! NOT-AUS ausgelöst – W/E losgelassen, Bot gestoppt.")

    def shutdown(self) -> None:
        """Thread-safe: Fenster schließen (wie X-Button)."""
        if self._in_main():
            self.close()
        else:
            self.request(self.close)

    # ------------------------------------------------------------- intern
    def _in_main(self) -> bool:
        return _gui_threading.current_thread() is self._main_thread

    def _clear_log(self) -> None:
        t = self.log_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.configure(state="disabled")

    def _do_toggle(self) -> None:
        try:
            st = _gui_state_name(self.engine.state)
            if st == "STOPPED":
                self.log("Bot war gestoppt – starte Engine neu …")
                self.engine.start_thread()
                self.engine.resume()
            else:
                self.engine.toggle_pause()
            self.log("Status: %s" % _gui_state_name(self.engine.state))
            self._update_status()
        except Exception as e:
            self.log("Fehler bei Start/Pause: %s" % e)

    def _on_topmost(self) -> None:
        try:
            self.root.attributes("-topmost", bool(self.topmost_var.get()))
        except Exception as e:
            self.log("Topmost nicht möglich: %s" % e)

    def _on_module(self, key: str) -> None:
        val = bool(self.module_vars[key].get())
        try:
            self.engine.set_module(key, val)
            self.config.set("modules", key, val)
            self.config.save()
            self._refresh_module_card_style(key, val)
            self.log("Modul %s %s" % (key, "AN" if val else "AUS"))
        except Exception as e:
            self.log("Fehler beim Setzen von %s: %s" % (key, e))

    def _refresh_module_card_style(self, key: str, val: bool) -> None:
        T = GUI_THEME
        card = self._module_cards.get(key)
        badge = self._module_badges.get(key)
        if card is not None:
            card.configure(highlightbackground=T["accent_green"] if val else T["border"])
        if badge is not None:
            badge.configure(
                text="AKTIV" if val else "AUS",
                bg=T["accent_green"] if val else T["bg_input"],
                fg="#052e16" if val else T["fg_muted"],
            )

    def validate_params(self) -> Tuple[Dict[Tuple[str, str], Any], List[str]]:
        """Gibt (values_dict, errors_list) zurück."""
        values: Dict[Tuple[str, str], Any] = {}
        errors: List[str] = []
        for _cat, sec, key, label, typ, lo, hi, _def, _step, _fmt, _desc, sync_key in GUI_PARAM_SPECS:
            raw = self.param_vars[(sec, key)].get().strip().replace(",", ".")
            try:
                if typ is int:
                    v = int(float(raw))
                    if abs(float(raw) - v) > 1e-6:
                        raise ValueError
                else:
                    v = float(raw)
            except (ValueError, TypeError):
                errors.append("%s: '%s' ist keine gültige Zahl" % (label, raw))
                continue
            if not (lo <= v <= hi):
                errors.append("%s: %s außerhalb [%s … %s]" % (label, v, lo, hi))
                continue
            values[(sec, key)] = v
            if sync_key is not None:
                values[sync_key] = v
        return values, errors

    def save_params(self) -> bool:
        values, errors = self.validate_params()
        if errors:
            for e in errors:
                self.log("Ungültig – %s" % e)
            try:
                from tkinter import messagebox

                messagebox.showerror("Ungültige Eingabe", "\n".join(errors), parent=self.root)
            except Exception:
                pass
            return False
        try:
            self._syncing_vars = True
            try:
                for _cat, sec, key, _lbl, typ, _lo, _hi, _def, _step, fmt, _desc, sync_key in GUI_PARAM_SPECS:
                    v = values[(sec, key)]
                    self.config.set(sec, key, v)
                    formatted = self._format_val(v, typ, fmt)
                    self.param_vars[(sec, key)].set(formatted)
                    if (sec, key) in self.slider_vars:
                        self.slider_vars[(sec, key)].set(float(v))
                    if sync_key is not None:
                        self.config.set(sync_key[0], sync_key[1], v)
                        if sync_key in self.param_vars:
                            self.param_vars[sync_key].set(formatted)
            finally:
                self._syncing_vars = False
            self.config.save()
            self.save_status_var.set("✓ Alle Parameter in config.json gespeichert")
            self.log("Parameter gespeichert.")
            return True
        except Exception as e:
            self.log("Fehler beim Speichern: %s" % e)
            return False

    def reload_params(self) -> None:
        try:
            self.config.load()
        except Exception:
            pass
        self._syncing_vars = True
        try:
            for _cat, sec, key, _lbl, typ, lo, hi, default_val, _step, fmt, _desc, sync_key in GUI_PARAM_SPECS:
                val = self._get_config_param(sec, key, default_val, sync_key)
                formatted = self._format_val(val, typ, fmt)
                self.param_vars[(sec, key)].set(formatted)
                if sync_key is not None and sync_key in self.param_vars:
                    self.param_vars[sync_key].set(formatted)
                if (sec, key) in self.slider_vars:
                    try:
                        self.slider_vars[(sec, key)].set(min(max(float(val), float(lo)), float(hi)))
                    except Exception:
                        self.slider_vars[(sec, key)].set(float(default_val))
        finally:
            self._syncing_vars = False

        for key, var in self.module_vars.items():
            val = bool(self.config.get("modules", key, default=True))
            var.set(val)
            self._refresh_module_card_style(key, val)
        self.refresh_points()
        self.save_status_var.set("↺ Parameter aus config.json neu geladen")
        self.log("Parameter neu geladen.")

    def reset_to_defaults(self) -> None:
        """Setzt alle Parameter, Module und Punkte auf die vorkalibrierten Standardwerte zurück."""
        try:
            if hasattr(self.config, "reset_to_defaults") and callable(self.config.reset_to_defaults):
                self.config.reset_to_defaults()
            else:
                defaults = copy.deepcopy(DEFAULT_CONFIG)
                for k, v in defaults.items():
                    self.config.set(k, v)
                for _cat, sec, key, _lbl, _typ, _lo, _hi, default_val, _step, _fmt, _desc, sync_key in GUI_PARAM_SPECS:
                    if self.config.get(sec, key, default=None) is None:
                        self.config.set(sec, key, default_val)
                    if sync_key is not None and self.config.get(sync_key[0], sync_key[1], default=None) is None:
                        self.config.set(sync_key[0], sync_key[1], default_val)
                self.config.save()
            self.reload_params()
            self.save_status_var.set("⟲ Standardwerte wiederhergestellt & gespeichert")
            self.log("Alle Einstellungen auf Standardwerte zurückgesetzt.")
        except Exception as e:
            self.log("Fehler beim Zurücksetzen auf Standardwerte: %s" % e)

    def refresh_points(self) -> None:
        tree = self.points_tree
        for item in tree.get_children():
            tree.delete(item)
        for name in POINT_NAMES:
            p = self.config.get("points", name, default=None) or {}
            try:
                x = "%.4f" % float(p.get("x", 0.0))
                y = "%.4f" % float(p.get("y", 0.0))
            except Exception:
                x, y = str(p.get("x")), str(p.get("y"))
            rgb = p.get("rgb", "")
            hex_col = _rgb_to_hex(rgb)
            if isinstance(rgb, (list, tuple)):
                rgb_str = "(%s) %s" % (", ".join(str(int(c)) for c in rgb), hex_col.upper())
            else:
                rgb_str = str(rgb)
            tol = p.get("tol", "")
            desc = POINT_DESCRIPTIONS.get(name, "")
            tree.insert("", "end", iid=name, values=(name, x, y, rgb_str, tol, desc))
        self._sync_selected_point_tol()

    def _sync_selected_point_tol(self) -> None:
        name = self.point_var.get()
        if not name:
            return
        p = self.config.get("points", name, default=None) or {}
        tol = p.get("tol", 40)
        if hasattr(self, "point_tol_var"):
            self.point_tol_var.set(str(tol))

    def _on_combo_select(self, _evt: Any = None) -> None:
        name = self.point_var.get()
        if name and name in self.points_tree.get_children():
            self.points_tree.selection_set(name)
            self.points_tree.see(name)
        self._sync_selected_point_tol()

    def _on_tree_select(self, _evt: Any = None) -> None:
        sel = self.points_tree.selection()
        if sel:
            self.point_var.set(sel[0])
            self._sync_selected_point_tol()

    def _on_point_tol_change(self) -> None:
        name = self.point_var.get()
        if not name or name not in POINT_NAMES:
            return
        try:
            tol = min(max(int(float(self.point_tol_var.get().strip())), 5), 150)
        except Exception:
            return
        p = dict(self.config.get("points", name, default={}) or {})
        p["tol"] = tol
        self.config.set("points", name, p)
        try:
            self.config.save()
        except Exception:
            pass
        self.refresh_points()
        self.log("Toleranz für %s auf %d gesetzt." % (name, tol))

    def start_calibration_countdown(self, point_name: Optional[str] = None) -> None:
        if self._countdown_active:
            return
        if point_name is not None and point_name in POINT_NAMES:
            self.point_var.set(point_name)
            self._on_combo_select()
        name = self.point_var.get()
        if not name:
            self.log("Kein Punkt gewählt.")
            return
        self._countdown_active = True
        self.btn_cal.configure(state="disabled")
        self._countdown_step(name, GUI_CALIBRATE_COUNTDOWN)

    def _countdown_step(self, name: str, remaining: int) -> None:
        if self._closed:
            return
        if remaining > 0:
            self.cal_info.set("⏳ Kalibriere '%s' in %d s … Maus im Spiel auf den Punkt halten!" % (name, remaining))
            self.root.after(1000, self._countdown_step, name, remaining - 1)
            return
        self._countdown_active = False
        self.btn_cal.configure(state="normal")
        self._do_calibrate(name)

    def _do_calibrate(self, name: Optional[str] = None) -> None:
        if name is None:
            name = self.point_var.get()
        if not name:
            self.log("Kein Punkt gewählt.")
            return
        try:
            res = self.engine.calibrate_point(name)
            self.log("Kalibriert %s: %s" % (name, res))
            self.cal_info.set("✓ '%s' erfolgreich kalibriert: x=%.4f, y=%.4f, rgb=%s" % (
                name, res.get("x", 0.0), res.get("y", 0.0), res.get("rgb")
            ))
        except Exception as e:
            self.log("Kalibrierung %s fehlgeschlagen: %s" % (name, e))
            self.cal_info.set("⚠ Fehler: %s" % e)
        self.refresh_points()

    def _draw_tacho_bar(self, dbg: Optional[dict]) -> None:
        c = self.tacho_canvas
        w = max(c.winfo_width(), 300)
        h = max(c.winfo_height(), 24)
        c.delete("all")
        T = GUI_THEME

        start_ang = float(self.config.get("needle", "start_angle_deg", default=230.0) or 230.0)
        end_ang = float(self.config.get("needle", "end_angle_deg", default=-50.0) or -50.0)
        sweep = max(abs(end_ang - start_ang), 1.0)

        def _ang_to_x(ang: float) -> float:
            prog = needle_progress(ang, start_ang, end_ang)
            frac = min(max(prog / sweep, 0.0), 1.0)
            return 6.0 + frac * (w - 12.0)

        # Scale ticks
        for frac_tick in (0.0, 0.25, 0.5, 0.75, 1.0):
            tx = 6.0 + frac_tick * (w - 12.0)
            c.create_line(tx, h - 5, tx, h, fill=T["border"], width=1)

        if not isinstance(dbg, dict):
            c.create_text(
                w / 2.0,
                h / 2.0,
                text="Tacho-Telemetrie bereit (aktiv an der Start-Ampel)",
                fill=T["fg_muted"],
                font=self.fonts["small"],
            )
            return

        # 1. Blue Zone Band
        zone_raw = dbg.get("zone")
        if isinstance(zone_raw, str) and ".." in zone_raw:
            try:
                z0_s, z1_s = zone_raw.split("..", 1)
                zx0 = _ang_to_x(float(z0_s))
                zx1 = _ang_to_x(float(z1_s))
                left_x, right_x = min(zx0, zx1), max(zx0, zx1)
                c.create_rectangle(
                    left_x,
                    2,
                    max(left_x + 4, right_x),
                    h - 2,
                    fill="#0e7490",
                    outline=T["accent_cyan"],
                    width=1,
                )
            except Exception:
                pass

        # 2. Needle Progress Bar
        needle_ang = dbg.get("needle_angle")
        target_ang = dbg.get("target_angle")
        if isinstance(needle_ang, (int, float)):
            nx = _ang_to_x(float(needle_ang))
            bar_col = T["accent_blue"]
            if isinstance(target_ang, (int, float)):
                diff = abs(float(needle_ang) - float(target_ang))
                if diff <= 5.0:
                    bar_col = T["accent_green"]
                elif needle_progress(float(needle_ang), start_ang, end_ang) > needle_progress(
                    float(target_ang), start_ang, end_ang
                ):
                    bar_col = T["accent_amber"]
            c.create_rectangle(6, 5, max(8.0, nx), h - 5, fill=bar_col, outline="")
            c.create_line(nx, 1, nx, h - 1, fill="#ffffff", width=3)

        # 3. Target Marker Line
        if isinstance(target_ang, (int, float)):
            tx = _ang_to_x(float(target_ang))
            c.create_line(tx, 1, tx, h - 1, fill=T["accent_green"], width=2)
            c.create_polygon(tx - 4, 1, tx + 4, 1, tx, 6, fill=T["accent_green"], outline="")

    def _update_status(self) -> None:
        try:
            st = _gui_state_name(self.engine.state)
        except Exception:
            st = "?"
        if st != self._last_state:
            bg, fg = GUI_STATE_COLORS.get(st, ("#334155", "#ffffff"))
            self.status_label.configure(text=st, bg=bg, fg=fg)

            if st == "STOPPED":
                r_bg, r_fg = GUI_RUN_BADGE_COLORS["STOPPED"]
                self.run_badge.configure(text="STOPPED", bg=r_bg, fg=r_fg)
            elif st == "PAUSED":
                r_bg, r_fg = GUI_RUN_BADGE_COLORS["PAUSED"]
                self.run_badge.configure(text="PAUSED", bg=r_bg, fg=r_fg)
            else:
                r_bg, r_fg = GUI_RUN_BADGE_COLORS["RUNNING"]
                self.run_badge.configure(text="RUNNING", bg=r_bg, fg=r_fg)

            self._last_state = st
            if st in ("PAUSED", "STOPPED"):
                self.btn_toggle.configure(text="▶ START (F9)", style="Start.TButton")
            else:
                self.btn_toggle.configure(text="⏸ PAUSE (F9)", style="Pause.TButton")

        try:
            dbg = getattr(self.engine, "last_debug", None)
            if isinstance(dbg, dict):
                dbg = dict(dbg)
            text = _gui_format_debug(dbg)
        except Exception:
            dbg = None
            text = "Debug: –"

        if isinstance(dbg, dict):
            na = dbg.get("needle_angle")
            ta = dbg.get("target_angle")
            zn = dbg.get("zone")
            lt = dbg.get("light_type")
            lm = dbg.get("loop_ms")
            self.telem_vars["needle"].set("%.1f°" % na if isinstance(na, (int, float)) else "–")
            self.telem_vars["target"].set("%.1f°" % ta if isinstance(ta, (int, float)) else "–")
            self.telem_vars["zone"].set("%s°" % zn if zn else "–")
            self.telem_vars["light"].set(str(lt).upper() if lt and lt != "none" else "–")
            self.telem_vars["loop"].set("%.1f ms" % lm if isinstance(lm, (int, float)) else "–")

        if text != self._last_debug_text:
            self.debug_var.set(text)
            self._last_debug_text = text
            self._draw_tacho_bar(dbg if isinstance(dbg, dict) else None)

    def _drain_requests(self) -> None:
        for _ in range(50):
            try:
                func, args, kwargs = self._req_queue.get_nowait()
            except _gui_queue.Empty:
                break
            try:
                func(*args, **kwargs)
            except Exception as e:
                self.log("GUI-Request-Fehler: %s" % e)
                _gui_traceback.print_exc()
            if self._closed:
                return

    def _drain_log(self) -> None:
        lines = []
        for _ in range(500):
            try:
                lines.append(self._log_queue.get_nowait())
            except _gui_queue.Empty:
                break
        if not lines:
            return
        t = self.log_text
        t.configure(state="normal")
        t.insert("end", "\n".join(lines) + "\n")
        n = int(float(t.index("end-1c").split(".")[0]))
        if n > GUI_MAX_LOG_LINES:
            t.delete("1.0", "%d.0" % (n - GUI_MAX_LOG_LINES))
        t.configure(state="disabled")
        t.see("end")

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            self._drain_requests()
            if self._closed:
                return
            self._drain_log()
            self._update_status()
        except Exception as e:  # pragma: no cover
            print("GUI poll error: %s" % e)
        if not self._closed:
            self.root.after(GUI_POLL_MS, self._poll)

    def bind_local_hotkeys(self, calibrate: str = "F8", toggle: str = "F9", emergency: str = "F10") -> None:
        """Fallback, falls globale Hotkeys nicht verfügbar sind (nur wenn das
        Bot-Fenster den Fokus hat)."""
        self.root.bind("<%s>" % calibrate, lambda _e: self.calibrate_selected())
        self.root.bind("<%s>" % toggle, lambda _e: self.toggle_pause())
        self.root.bind("<%s>" % emergency, lambda _e: self.emergency())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fn in ("safety_release", "emergency_stop"):
            try:
                getattr(self.engine, fn)()
            except Exception as e:
                print("Fehler bei %s: %s" % (fn, e))
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception as e:
                print("Fehler in on_close: %s" % e)
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        """Blockiert in mainloop (Main-Thread!)."""
        try:
            self.root.mainloop()
        finally:
            if not self._closed:
                self.close()


def create_gui(engine: Any, config: Any, platform_name: str, on_close: Optional[Callable[[], Any]] = None) -> BotGUI:
    """Erzeugt die GUI ohne mainloop zu starten (z.B. um vorher Hotkeys zu
    registrieren). Danach gui.run() aufrufen."""
    return BotGUI(engine, config, platform_name, on_close=on_close)


def run_gui(engine: Any, config: Any, platform_name: str, on_close: Optional[Callable[[], Any]] = None) -> BotGUI:
    """Komfort: GUI erzeugen und mainloop starten (blockiert)."""
    gui = create_gui(engine, config, platform_name, on_close=on_close)
    gui.run()
    return gui


# ---------------------------------------------------------------------------
# Test-Hilfen
# ---------------------------------------------------------------------------
class FakeScreenReader(ScreenReader):
    """ScreenReader ohne echten Bildschirm: Pixel aus Dict (+ simulierte Tacho-Nadel)."""

    def __init__(self, width: int = 1000, height: int = 800, scale: float = 2.0, background: RGB = (20, 20, 20)):
        super().__init__()
        self._fake_mon = {"left": 0, "top": 0, "width": width, "height": height}
        self._fake_scale = float(scale)
        self.background = background
        self.pixels: Dict[Tuple[int, int], RGB] = {}
        self.plock = threading.Lock()
        self.needle_deg: Optional[float] = None
        self.needle_center: Optional[Tuple[float, float]] = None  # phys
        self.needle_len = 0.0
        self.on_read: Optional[Callable[[], None]] = None

    def monitor(self) -> dict:
        return dict(self._fake_mon)

    def scale(self) -> float:
        return self._fake_scale

    def paint(self, rx: float, ry: float, rgb: RGB, radius_phys: int = 10) -> None:
        cx, cy = self.rel_to_phys(rx, ry)
        cx, cy = int(round(cx)), int(round(cy))
        with self.plock:
            for dy in range(-radius_phys, radius_phys + 1):
                for dx in range(-radius_phys, radius_phys + 1):
                    self.pixels[(cx + dx, cy + dy)] = tuple(rgb)

    def paint_phys(self, x: int, y: int, rgb: RGB) -> None:
        with self.plock:
            self.pixels[(x, y)] = tuple(rgb)

    def clear(self, rx: float, ry: float, radius_phys: int = 10) -> None:
        cx, cy = self.rel_to_phys(rx, ry)
        cx, cy = int(round(cx)), int(round(cy))
        with self.plock:
            for dy in range(-radius_phys, radius_phys + 1):
                for dx in range(-radius_phys, radius_phys + 1):
                    self.pixels.pop((cx + dx, cy + dy), None)

    def _needle_px(self, x: int, y: int) -> bool:
        if self.needle_deg is None or self.needle_center is None:
            return False
        cx, cy = self.needle_center
        a = math.radians(self.needle_deg)
        ux, uy = math.cos(a), -math.sin(a)
        vx, vy = x - cx, y - cy
        along = vx * ux + vy * uy
        perp = abs(vx * uy - vy * ux)
        return 0.3 * self.needle_len <= along <= self.needle_len and perp <= 2.5

    def _read_pixels_phys(self, ipts: List[Tuple[int, int]]) -> List[RGB]:
        if self.on_read:
            self.on_read()
        out = []
        with self.plock:
            for p in ipts:
                c = self.pixels.get(p)
                if c is None:
                    c = (245, 245, 245) if self._needle_px(*p) else self.background
                out.append(c)
        return out


def _selftest() -> int:
    import tempfile

    GREEN, ORANGE = (60, 200, 70), (240, 160, 40)
    fails: List[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("  OK   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def wait_for(fn: Callable[[], bool], timeout: float = 3.0) -> bool:
        t_end = time.time() + timeout
        while time.time() < t_end:
            if fn():
                return True
            time.sleep(0.005)
        return fn()

    print("== Unit: needle_progress / Farben")
    check(abs(needle_progress(225, 225, -45) - 0) < 1e-9, "progress start = 0")
    check(abs(needle_progress(-45, 225, -45) - 270) < 1e-9, "progress end = 270")
    check(abs(needle_progress(315, 225, -45) - 270) < 1e-9, "progress end als 315° (wrap)")
    check(abs(needle_progress(58, 225, -45) - 167) < 1e-9, "progress 58° = 167")
    check(needle_progress(230, 225, -45) < 0, "knapp vor Start = negativ")
    check(abs(needle_progress(90, -45, 225) - 135) < 1e-9, "Gegenrichtung")
    check(is_green(GREEN) and not is_orange_or_yellow(GREEN), "grün erkannt")
    check(is_orange_or_yellow(ORANGE) and not is_green(ORANGE), "orange erkannt")
    check(is_orange_or_yellow((250, 220, 40)), "gelb erkannt")
    check(is_bright_red((230, 30, 30)) and not is_bright_red((142, 57, 60)), "rot")
    check(is_white((240, 240, 235)) and not is_white((200, 150, 150)), "weiß")
    check(is_zone_blue((146, 220, 235)) and not is_zone_blue((82, 139, 149)), "zone_blue")

    tmpdir = tempfile.mkdtemp(prefix="taxiboss_test_")
    cfg_path = os.path.join(tmpdir, "config.json")
    with open(cfg_path, "w") as f:
        f.write("{kaputt")
    cm = ConfigManager(cfg_path)
    check(cm.get("timing", "tap_duration") == 0.014, "kaputte Config -> Defaults")
    check(any(n.endswith(".bak") for n in os.listdir(tmpdir)), "Backup der kaputten Config")
    cm.set("timing", "menu_click_delay", 0.99)
    cm.reset_to_defaults()
    check(cm.get("timing", "menu_click_delay") == 0.5, "reset_to_defaults")
    cm.set("timing", "menu_click_delay", 0.1)
    cm.set("timing", "button_reclick_cooldown", 0.3)
    cm.save()
    check(ConfigManager(cfg_path).get("timing", "menu_click_delay") == 0.1, "set/save/load")

    scr = FakeScreenReader()
    P = cm.get("points")
    cxp, cyp = scr.rel_to_phys(P["tacho_center"]["x"], P["tacho_center"]["y"])
    scr.needle_center = (cxp, cyp)
    scr.needle_len = cm.get("needle", "radius_rel") * 1000 * 2.0 * 1.4
    scr.needle_deg = 225.0

    phys = {"w_down": None}

    def on_event(ev):  # einfache Physik: W gehalten -> Nadel dreht im Uhrzeigersinn
        _, kind, key = ev
        if key == "w" and kind == "press":
            phys["w_down"] = time.perf_counter()
        elif key == "w" and kind == "release" and phys["w_down"] is not None:
            dt = time.perf_counter() - phys["w_down"]
            phys["w_down"] = None
            if scr.needle_deg is not None:
                scr.needle_deg = max(-45.0, scr.needle_deg - dt * 400.0)

    def on_read():  # Nadel fällt langsam zurück
        if scr.needle_deg is not None and phys["w_down"] is None:
            scr.needle_deg = min(225.0, scr.needle_deg + 0.3)

    scr.on_read = on_read
    inp = DummyInputBackend(on_event=on_event)
    logs: List[str] = []
    lock = threading.Lock()

    def log(m):
        with lock:
            logs.append(m)
        print("   [log] " + m)

    eng = BotEngine(cm, inp, scr, log=log)

    print("== Needle-Erkennung")
    for deg in (225.0, 150.0, 58.0, 0.0, -45.0):
        scr.needle_deg = deg
        a = eng.read_needle_angle()
        check(a is not None and abs(a - deg) <= 1.5, "Nadel %.0f° erkannt als %s" % (deg, a))
    scr.needle_deg = None
    check(eng.read_needle_angle() is None, "keine Nadel -> None")
    scr.needle_deg = 225.0
    target = eng._target_angle(cm.data)
    print("   Zielwinkel = %.1f°" % target)

    print("== State Machine")
    eng.start_thread()
    time.sleep(0.1)
    check(eng.state == BotState.PAUSED, "startet PAUSED")
    check(not any(e[1] == "press" for e in inp.snapshot()), "PAUSED: keine Tasten")
    eng.resume()
    check(wait_for(lambda: eng.state == BotState.WAITING, 1), "resume -> WAITING")

    # Ampel (normal)
    scr.paint(P["light_box_1"]["x"], P["light_box_1"]["y"], (60, 60, 60))
    scr.paint(P["light_box_2"]["x"], P["light_box_2"]["y"], (60, 60, 60))
    check(wait_for(lambda: eng.state == BotState.AT_LIGHT, 1), "Ampel -> AT_LIGHT")
    check(eng.last_debug.get("light_type") == "normal", "light_type normal")
    reached = wait_for(lambda: abs(scr.needle_deg - target) < 8, 5)
    taps = sum(1 for e in inp.snapshot() if e[1] == "press" and e[2] == "w")
    check(reached, "Nadel per Micro-Taps an Ziel (Taps=%d, Nadel=%.1f°)" % (taps, scr.needle_deg))
    time.sleep(0.5)
    check(abs(scr.needle_deg - target) < 12, "Nadel hält Ziel (%.1f° vs %.1f°)" % (scr.needle_deg, target))
    check(taps >= 5, "mehrere Micro-Taps")

    # GO
    scr.clear(P["light_box_1"]["x"], P["light_box_1"]["y"])
    scr.clear(P["light_box_2"]["x"], P["light_box_2"]["y"])
    check(wait_for(lambda: eng.state == BotState.RACING, 1), "Ampel weg -> RACING")
    check(wait_for(lambda: "w" in inp.held, 0.5), "W gehalten im Rennen")
    scr.needle_deg = None

    # Shift
    n_e = sum(1 for e in inp.snapshot() if e[1] == "press" and e[2] == "e")
    scr.paint(P["shift_circle"]["x"], P["shift_circle"]["y"], GREEN)
    check(
        wait_for(lambda: sum(1 for e in inp.snapshot() if e[1] == "press" and e[2] == "e") > n_e, 1),
        "Shift-Kreis grün -> E getappt",
    )
    time.sleep(0.1)
    scr.clear(P["shift_circle"]["x"], P["shift_circle"]["y"])
    check("e" not in inp.held, "E wieder losgelassen")
    shifts = sum(1 for e in inp.snapshot() if e[1] == "press" and e[2] == "e") - n_e
    check(1 <= shifts <= 2, "Shift-Cooldown wirkt (%d Shifts in 0.1s)" % shifts)

    # Grün mit Orange-Anteil -> kein Klick
    bw = P["btn_weiter"]
    scr.paint(bw["x"], bw["y"], GREEN)
    bx, by = scr.rel_to_phys(bw["x"], bw["y"])
    for dx in range(-1, 2):
        for dy in range(-1, 2):
            scr.paint_phys(int(round(bx + 6)) + dx, int(round(by)) + dy, ORANGE)
    time.sleep(0.2)
    check(not any(e[1] == "click" for e in inp.snapshot()), "Grün+Orange -> kein Klick")
    check(eng.state == BotState.RACING, "bleibt RACING")

    def clicks():
        return [e for e in inp.snapshot() if e[1] == "click"]

    def check_click(name, n_before):
        pt = P[name]
        ok = wait_for(lambda: len(clicks()) > n_before, 1.5)
        check(ok, "%s grün -> Klick" % name)
        if not ok:
            return
        ev = inp.snapshot()
        ci = ev.index(clicks()[n_before])
        lx, ly = scr.rel_to_logical(pt["x"], pt["y"])
        cx_, cy_ = ev[ci][2]
        check(abs(cx_ - lx) <= 1 and abs(cy_ - ly) <= 1, "Klickposition %s korrekt" % name)
        wev = [e for e in ev[:ci] if e[2] == "w"]
        check(bool(wev) and wev[-1][1] == "release", "W vor Klick losgelassen")
        check(wait_for(lambda: eng.state == BotState.MENU_SKIP, 0.5), "-> MENU_SKIP")

    # WEITER
    scr.paint(bw["x"], bw["y"], GREEN)
    check_click("btn_weiter", 0)
    time.sleep(0.15)
    check(len(clicks()) == 1, "kein Doppelklick innerhalb Cooldown")
    scr.clear(bw["x"], bw["y"])
    # GIVE UP (STREAK OVER): Gelb (REVIVE) darf NICHT geklickt werden, Rot (GIVE UP) wird geklickt
    bg = P["Give_Up_Button"]
    scr.paint(bg["x"], bg["y"], (244, 188, 87))
    time.sleep(0.15)
    check(len(clicks()) == 1, "REVIVE-Gelb auf Give_Up_Button -> kein Klick")
    scr.paint(bg["x"], bg["y"], (213, 87, 78))
    check_click("Give_Up_Button", 1)
    scr.clear(bg["x"], bg["y"])
    # NEXT STAGE
    bn = P["btn_next_stage"]
    scr.paint(bn["x"], bn["y"], GREEN)
    check_click("btn_next_stage", 2)
    scr.clear(bn["x"], bn["y"])
    # RENNEN
    br = P["btn_rennen"]
    scr.paint(br["x"], br["y"], GREEN)
    check_click("btn_rennen", 3)
    scr.clear(br["x"], br["y"])
    time.sleep(0.3)
    check(eng.state == BotState.MENU_SKIP, "nach RENNEN bleibt MENU_SKIP bis Ampel")

    # Boss-Ampel
    scr.needle_deg = 225.0
    scr.paint(P["light_box_1"]["x"], P["light_box_1"]["y"], (142, 57, 60))
    scr.paint(P["light_box_2"]["x"], P["light_box_2"]["y"], (142, 57, 60))
    check(wait_for(lambda: eng.state == BotState.AT_LIGHT, 1), "Boss-Ampel -> AT_LIGHT")
    check(eng.last_debug.get("light_type") == "boss", "light_type boss")
    wait_for(lambda: scr.needle_deg < 215, 2)

    # Pause bricht Micro-Taps sofort ab
    eng.pause()
    check(eng.state == BotState.PAUSED and "w" not in inp.held, "Pause -> W losgelassen")
    n_ev = len(inp.snapshot())
    time.sleep(0.2)
    check(not any(e[1] == "press" for e in inp.snapshot()[n_ev:]), "Pause: keine weiteren Tasten")

    # Modulwechsel -> release_all
    eng.resume()
    wait_for(lambda: eng.state == BotState.AT_LIGHT, 1)
    eng.set_module("auto_start", False)
    time.sleep(0.1)
    check("w" not in inp.held, "auto_start AUS -> kein W")
    eng.set_module("auto_start", True)

    # Exception im Loop -> safety_release, läuft weiter
    orig = scr._read_pixels_phys
    boom = {"n": 3}

    def failing(ipts):
        if boom["n"] > 0:
            boom["n"] -= 1
            raise RuntimeError("Testfehler")
        return orig(ipts)

    scr._read_pixels_phys = failing
    time.sleep(0.5)
    scr._read_pixels_phys = orig
    check(any("Testfehler" in m for m in logs), "Exception geloggt")
    check(eng._thread.is_alive() and eng.state == BotState.AT_LIGHT, "Engine läuft nach Exception weiter")

    # Kalibrierung
    inp.mouse = scr.rel_to_logical(0.25, 0.5)
    scr.paint(0.25, 0.5, (11, 22, 33))
    res = eng.calibrate_point("shift_circle")
    check(abs(res["x"] - 0.25) < 0.002 and res["rgb"] == [11, 22, 33], "calibrate_point -> %s" % res)
    check(ConfigManager(cfg_path).get("points", "shift_circle", "rgb") == [11, 22, 33], "Kalibrierung gespeichert")

    # NOT-AUS
    eng.emergency_stop()
    check(eng.state == BotState.STOPPED, "NOT-AUS -> STOPPED")
    check(not eng._thread.is_alive(), "Thread beendet")
    check(not inp.held, "alle Tasten losgelassen")

    print()
    if fails:
        print("SELBSTTEST FEHLGESCHLAGEN (%d):" % len(fails))
        for f_ in fails:
            print("  - " + f_)
        return 1
    print("SELBSTTEST OK")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
