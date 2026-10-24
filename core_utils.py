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
        "tap_duration": 0.020,
        "tap_cooldown": 0.025,
        "shift_cooldown": 0.25,
        "menu_click_delay": 0.5,
        "loop_interval": 0.004,
        "go_confirm_frames": 3,
        "shift_tap_duration": 0.03,
        # Zusatz (nicht im Vertrag): derselbe Button wird erst nach dieser Zeit erneut geklickt
        "button_reclick_cooldown": 1.5,
    },
    "needle": {
        "start_angle_deg": 240.0,
        "end_angle_deg": -80.0,
        "radius_rel": 0.04386,  # 150 px phys bei 3420 px Breite
        "white_min": 200,
        "white_max_spread": 40,
        "target_offset_deg": 0.0,
        # Zusatz: Abtastradien als Faktor von radius_rel
        "radius_factors": [0.85, 1.0, 1.15],
        # Zusatz: blaue Zielzone live auf dem Tacho-Ring erkennen (normal breit, Boss schmal)
        "zone_detect": True,
        "zone_radius_rel": 0.08772,  # 300 px phys
        "zone_target_fraction": 0.5,  # 0 = Zonenanfang, 0.5 = Mitte, 1 = Zonenende
        "zone_refresh_s": 0.2,
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
        "cashout_orange": [245, 189, 89],
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


def is_green(rgb: Sequence[int]) -> bool:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return g > r + 40 and g > b + 30 and g > 100


def is_orange_or_yellow(rgb: Sequence[int]) -> bool:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return r > 180 and g >= 90 and b < 110 and (r - b) > 100 and r >= g - 15


def is_bright_red(rgb: Sequence[int]) -> bool:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return r > 170 and g < 90 and b < 90 and r > g + 100


def is_white(rgb: Sequence[int], min_val: int = 200, max_spread: int = 40) -> bool:
    lo = min(rgb[0], rgb[1], rgb[2])
    hi = max(rgb[0], rgb[1], rgb[2])
    return lo >= min_val and (hi - lo) <= max_spread


def is_zone_blue(rgb: Sequence[int]) -> bool:
    """Hellblaue Ziel-Zone am Tacho-Ring (~(146,220,235)); NICHT der blassblaue Rest-Ring (~(82,139,149))."""
    r, g, b = rgb[0], rgb[1], rgb[2]
    return b > 200 and g > 180 and r < 190 and b > r + 40


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
    MENU_BUTTONS = ("btn_weiter", "Give_Up_Button", "btn_next_stage", "btn_rennen")
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
                frac = min(max(float(nd.get("zone_target_fraction", 0.5)), 0.0), 1.0)
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
        step = -0.5 if end < start else 0.5
        n = int(abs(end - start) / 0.5) + 1
        angles = [start + i * step for i in range(n)]
        radii = (r * 0.96, r * 1.03)
        pts = []
        for a in angles:
            ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
            for rr in radii:
                pts.append((cx + rr * ca, cy - rr * sa))
        pix = self.screen.get_pixels_phys(pts)
        hit = [is_zone_blue(pix[2 * i]) or is_zone_blue(pix[2 * i + 1]) for i in range(n)]
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
        r = float(nd.get("radius_rel", 0.04)) * mon["width"] * s
        start = float(nd.get("start_angle_deg", 225.0))
        end = float(nd.get("end_angle_deg", -45.0))
        wmin = int(nd.get("white_min", 200))
        wspread = int(nd.get("white_max_spread", 40))
        factors = nd.get("radius_factors") or [0.85, 1.0, 1.15]
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
        ang = self._read_needle(self.config.data)
        self._dbg(needle_angle=ang)
        return ang

    def _button_samples(self, pt: dict, s: float) -> List[Tuple[float, float]]:
        cx, cy = self.screen.rel_to_phys(pt["x"], pt["y"])
        d = self.BUTTON_SAMPLE_OFFSET_LOGICAL * s
        return [(cx, cy), (cx - d, cy), (cx + d, cy), (cx, cy - d), (cx, cy + d)]

    def _is_button_green(self, rgb: RGB, cfg: dict, tol: float) -> bool:
        if is_orange_or_yellow(rgb):
            return False
        if is_green(rgb):
            return True
        c = cfg["colors"].get("button_green")
        return bool(c) and _is_set_color(c) and color_match(rgb, c, tol) and rgb[1] > rgb[0]

    def _is_give_up_red(self, rgb: RGB, cfg: dict, tol: float) -> bool:
        """Roter GIVE UP-Button im STREAK OVER-Popup (R deutlich > G und B; niemals gelber REVIVE-Button)."""
        if is_orange_or_yellow(rgb) or is_green(rgb):
            return False
        r, g, b = rgb[0], rgb[1], rgb[2]
        if not (r > 160 and g < 130 and b < 130 and r > g + 75 and r > b + 75):
            return False
        c = cfg["colors"].get("give_up_red") or cfg["points"].get("Give_Up_Button", {}).get("rgb")
        if c and _is_set_color(c):
            return color_match(rgb, c, tol)
        return True

    def _is_shift_green(self, rgb: RGB, cfg: dict, tol: float) -> bool:
        if is_green(rgb):
            return True
        c = cfg["colors"].get("shift_green")
        return bool(c) and _is_set_color(c) and color_match(rgb, c, tol) and rgb[1] > rgb[0]

    # -- Aktionen -------------------------------------------------------------
    def _micro_tap(self, timing: dict) -> None:
        if not self._io_press("w"):
            return
        self._w_held = True
        ok = self._sleep(float(timing.get("tap_duration", 0.02)))
        self._io_release("w")
        self._w_held = False
        if ok:
            self._sleep(float(timing.get("tap_cooldown", 0.025)))

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

        # ---- 1. Menü ---------------------------------------------------------
        for n in self.MENU_BUTTONS:
            if n not in btn_slices:
                continue
            a, b = btn_slices[n]
            tol = float(pts[n].get("tol", 50))
            samples = pix[a:b]
            if n == "Give_Up_Button":
                if all(self._is_give_up_red(p, cfg, tol) for p in samples) and not any(
                    is_orange_or_yellow(p) or is_green(p) for p in samples
                ):
                    self._click_button(n, cfg)
                    return
            elif all(self._is_button_green(p, cfg, tol) for p in samples) and not any(
                is_orange_or_yellow(p) for p in samples
            ):
                self._click_button(n, cfg)
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
        start = float(nd.get("start_angle_deg", 225.0))
        end = float(nd.get("end_angle_deg", -45.0))
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
        )
        if pn is None or pt is None:
            self._release_w()  # Nadel/Ziel nicht erkannt -> kein Gas
            return
        if pn < pt:
            self._micro_tap(cfg["timing"])
        else:
            self._release_w()


# =============================================================================
# GUI (tkinter) – läuft im Main-Thread, Engine läuft in eigenem Thread.
# tkinter wird erst in create_gui() importiert, damit core_utils auch ohne Tk
# importierbar bleibt.
# =============================================================================
import queue as _gui_queue
import threading as _gui_threading
import time as _gui_time
import traceback as _gui_traceback

GUI_POLL_MS = 100
GUI_MAX_LOG_LINES = 1500
GUI_CALIBRATE_COUNTDOWN = 3

GUI_STATE_COLORS = {
    "PAUSED": ("#5a5a5a", "#ffffff"),
    "WAITING": ("#1f6fd1", "#ffffff"),
    "AT_LIGHT": ("#e0a800", "#000000"),
    "RACING": ("#1e9e3a", "#ffffff"),
    "MENU_SKIP": ("#8a3fd1", "#ffffff"),
    "STOPPED": ("#c62828", "#ffffff"),
}

GUI_MODULES = [
    ("auto_shift", "Auto-Shift (E bei grünem Kreis)"),
    ("auto_start", "Auto-Start (Nadel halten + GO)"),
    ("auto_skip", "Auto-Skip (Menü-Buttons klicken)"),
]

# (Sektion, Key, Label, Typ, Min, Max)
GUI_PARAM_FIELDS = [
    ("timing", "tap_duration", "Tap Duration (s)", float, 0.001, 1.0),
    ("timing", "tap_cooldown", "Tap Cooldown (s)", float, 0.0, 1.0),
    ("timing", "shift_cooldown", "Shift Cooldown (s)", float, 0.0, 5.0),
    ("timing", "menu_click_delay", "Menu Click Delay (s)", float, 0.0, 10.0),
    ("timing", "loop_interval", "Loop Interval (s)", float, 0.0005, 0.5),
    ("timing", "go_confirm_frames", "GO Confirm Frames", int, 1, 60),
    ("timing", "shift_tap_duration", "Shift Tap Duration (s)", float, 0.005, 0.5),
    ("needle", "target_offset_deg", "Ziel-Offset (Grad)", float, -90.0, 90.0),
    ("needle", "zone_target_fraction", "Ziel in blauer Zone (0..1)", float, 0.0, 1.0),
]


def _gui_state_name(state):
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


def _gui_format_debug(dbg):
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


class BotGUI(object):
    """tkinter-Oberfläche für die BotEngine.

    Thread-safe Methoden (aus Hotkey-/Engine-Threads aufrufbar):
        log(msg), request(callable), calibrate_selected(),
        toggle_pause(), emergency(), shutdown()
    Alle anderen Methoden nur im Tk-Main-Thread benutzen.
    """

    def __init__(self, engine, config, platform_name, on_close=None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.engine = engine
        self.config = config
        self.platform_name = platform_name
        self.on_close = on_close

        self._log_queue = _gui_queue.Queue()
        self._req_queue = _gui_queue.Queue()
        self._closed = False
        self._countdown_active = False
        self._last_state = None
        self._last_debug_text = None
        self._main_thread = _gui_threading.current_thread()

        self.root = tk.Tk()
        self.root.title("Taxi Boss Drag Bot V2 – %s" % platform_name)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.root.minsize(560, 640)
        except Exception:
            pass

        self._build()
        self.refresh_points()
        self.root.after(GUI_POLL_MS, self._poll)

    # ------------------------------------------------------------------ UI
    def _build(self):
        tk, ttk = self.tk, self.ttk
        root = self.root
        pad = {"padx": 6, "pady": 3}

        # --- Status
        top = tk.Frame(root)
        top.pack(fill="x", **pad)
        self.status_label = tk.Label(
            top, text="…", font=("Helvetica", 22, "bold"),
            bg="#5a5a5a", fg="#ffffff", pady=8)
        self.status_label.pack(fill="x")
        self.debug_var = tk.StringVar(value="Debug: –")
        tk.Label(top, textvariable=self.debug_var, anchor="w",
                 font=("Courier", 11), justify="left",
                 wraplength=640).pack(fill="x")

        # --- Steuer-Buttons
        ctrl = tk.Frame(root)
        ctrl.pack(fill="x", **pad)
        self.btn_toggle = tk.Button(ctrl, text="Start / Pause (F9)",
                                    command=self.toggle_pause, height=2)
        self.btn_toggle.pack(side="left", expand=True, fill="x", padx=2)
        self.btn_stop = tk.Button(ctrl, text="NOT-AUS (F10)", fg="#c62828",
                                  font=("Helvetica", 13, "bold"),
                                  command=self.emergency, height=2)
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=2)
        self.topmost_var = tk.BooleanVar(value=False)
        tk.Checkbutton(ctrl, text="Immer im Vordergrund",
                       variable=self.topmost_var,
                       command=self._on_topmost).pack(side="left", padx=4)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, **pad)

        # --- Tab: Module & Parameter
        tab_main = tk.Frame(nb)
        nb.add(tab_main, text="Module & Parameter")

        mod_frame = tk.LabelFrame(tab_main, text="Module (sofort aktiv)")
        mod_frame.pack(fill="x", **pad)
        self.module_vars = {}
        for key, label in GUI_MODULES:
            val = bool(self.config.get("modules", key, default=True))
            var = tk.BooleanVar(value=val)
            self.module_vars[key] = var
            tk.Checkbutton(mod_frame, text=label, variable=var, anchor="w",
                           command=lambda k=key: self._on_module(k)
                           ).pack(fill="x")

        par_frame = tk.LabelFrame(tab_main, text="Parameter")
        par_frame.pack(fill="x", **pad)
        self.param_vars = {}
        for row, (sec, key, label, typ, lo, hi) in enumerate(GUI_PARAM_FIELDS):
            tk.Label(par_frame, text=label, anchor="w").grid(
                row=row, column=0, sticky="w", padx=4, pady=1)
            var = tk.StringVar(value=str(self.config.get(sec, key, default="")))
            self.param_vars[(sec, key)] = var
            tk.Entry(par_frame, textvariable=var, width=10).grid(
                row=row, column=1, sticky="w", padx=4, pady=1)
            tk.Label(par_frame, text="[%s … %s]" % (lo, hi), fg="#777777"
                     ).grid(row=row, column=2, sticky="w", padx=4)
        btns = tk.Frame(par_frame)
        btns.grid(row=len(GUI_PARAM_FIELDS), column=0, columnspan=3,
                  sticky="we", pady=4)
        tk.Button(btns, text="Speichern", command=self.save_params
                  ).pack(side="left", padx=4)
        tk.Button(btns, text="Neu laden", command=self.reload_params
                  ).pack(side="left", padx=4)

        # --- Tab: Kalibrierung
        tab_cal = tk.Frame(nb)
        nb.add(tab_cal, text="Kalibrierung")
        cal_top = tk.Frame(tab_cal)
        cal_top.pack(fill="x", **pad)
        names = list(POINT_NAMES)
        self.point_var = tk.StringVar(value=names[0] if names else "")
        self.point_combo = ttk.Combobox(cal_top, textvariable=self.point_var,
                                        values=names, state="readonly",
                                        width=20)
        self.point_combo.pack(side="left", padx=4)
        self.btn_cal = tk.Button(cal_top, text="Kalibrieren (3s Countdown)",
                                 command=self.start_calibration_countdown)
        self.btn_cal.pack(side="left", padx=4)
        self.cal_info = tk.StringVar(
            value="Maus auf den Punkt halten – oder F8 = gewählten Punkt "
                  "sofort an aktueller Mausposition setzen.")
        tk.Label(tab_cal, textvariable=self.cal_info, anchor="w",
                 justify="left", wraplength=620).pack(fill="x", **pad)

        cols = ("name", "x", "y", "rgb", "tol")
        self.points_tree = ttk.Treeview(tab_cal, columns=cols,
                                        show="headings", height=10)
        for c, w in zip(cols, (140, 80, 80, 140, 50)):
            self.points_tree.heading(c, text=c)
            self.points_tree.column(c, width=w, anchor="w")
        self.points_tree.pack(fill="both", expand=True, **pad)
        self.points_tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # --- Log
        log_frame = tk.LabelFrame(root, text="Log")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=10, state="disabled",
                                font=("Courier", 10), wrap="word")
        sb = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------ thread-safe API
    def log(self, msg):
        """Thread-safe: Nachricht ins Log-Fenster (auch vor/nach mainloop)."""
        try:
            line = "%s  %s" % (_gui_time.strftime("%H:%M:%S"), msg)
        except Exception:
            line = str(msg)
        self._log_queue.put(line)

    def request(self, func, *args, **kwargs):
        """Thread-safe: func(*args) im Tk-Main-Thread ausführen."""
        self._req_queue.put((func, args, kwargs))

    def calibrate_selected(self):
        """Thread-safe (F8): gewählten Punkt sofort an Mausposition setzen."""
        self.request(self._do_calibrate, None)

    def toggle_pause(self):
        """Thread-safe (F9)."""
        if self._in_main():
            self._do_toggle()
        else:
            self.request(self._do_toggle)

    def emergency(self):
        """Thread-safe (F10). Ruft emergency_stop sofort auf (auch aus
        Hotkey-Threads), GUI-Update folgt per Polling."""
        try:
            self.engine.emergency_stop()
        except Exception as e:  # pragma: no cover
            self.log("Fehler bei NOT-AUS: %s" % e)
        self.log("!!! NOT-AUS ausgelöst – W/E losgelassen, Bot gestoppt.")

    def shutdown(self):
        """Thread-safe: Fenster schließen (wie X-Button)."""
        if self._in_main():
            self.close()
        else:
            self.request(self.close)

    # ------------------------------------------------------------- intern
    def _in_main(self):
        return _gui_threading.current_thread() is self._main_thread

    def _do_toggle(self):
        try:
            st = _gui_state_name(self.engine.state)
            if st == "STOPPED":
                # Nach NOT-AUS: Thread neu starten (startet PAUSED) + fortsetzen
                self.log("Bot war gestoppt – starte Engine neu …")
                self.engine.start_thread()
                self.engine.resume()
            else:
                self.engine.toggle_pause()
            self.log("Status: %s" % _gui_state_name(self.engine.state))
        except Exception as e:
            self.log("Fehler bei Start/Pause: %s" % e)

    def _on_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.topmost_var.get()))
        except Exception as e:
            self.log("Topmost nicht möglich: %s" % e)

    def _on_module(self, key):
        val = bool(self.module_vars[key].get())
        try:
            self.engine.set_module(key, val)
            self.config.set("modules", key, val)
            self.config.save()
            self.log("Modul %s %s" % (key, "AN" if val else "AUS"))
        except Exception as e:
            self.log("Fehler beim Setzen von %s: %s" % (key, e))

    def validate_params(self):
        """Gibt (values_dict, errors_list) zurück."""
        values, errors = {}, []
        for sec, key, label, typ, lo, hi in GUI_PARAM_FIELDS:
            raw = self.param_vars[(sec, key)].get().strip().replace(",", ".")
            try:
                if typ is int:
                    v = int(float(raw))
                    if float(raw) != v:
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
        return values, errors

    def save_params(self):
        values, errors = self.validate_params()
        if errors:
            for e in errors:
                self.log("Ungültig – %s" % e)
            try:
                from tkinter import messagebox
                messagebox.showerror("Ungültige Eingabe", "\n".join(errors),
                                     parent=self.root)
            except Exception:
                pass
            return False
        try:
            for (sec, key), v in values.items():
                self.config.set(sec, key, v)
                self.param_vars[(sec, key)].set(str(v))
            self.config.save()
            self.log("Parameter gespeichert.")
            return True
        except Exception as e:
            self.log("Fehler beim Speichern: %s" % e)
            return False

    def reload_params(self):
        for sec, key, _l, _t, _lo, _hi in GUI_PARAM_FIELDS:
            self.param_vars[(sec, key)].set(
                str(self.config.get(sec, key, default="")))
        for key, var in self.module_vars.items():
            var.set(bool(self.config.get("modules", key, default=True)))
        self.refresh_points()

    def refresh_points(self):
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
            if isinstance(rgb, (list, tuple)):
                rgb = "(%s)" % ", ".join(str(int(c)) for c in rgb)
            tree.insert("", "end", iid=name,
                        values=(name, x, y, rgb, p.get("tol", "")))

    def _on_tree_select(self, _evt=None):
        sel = self.points_tree.selection()
        if sel:
            self.point_var.set(sel[0])

    def start_calibration_countdown(self):
        if self._countdown_active:
            return
        name = self.point_var.get()
        if not name:
            self.log("Kein Punkt gewählt.")
            return
        self._countdown_active = True
        self.btn_cal.configure(state="disabled")
        self._countdown_step(name, GUI_CALIBRATE_COUNTDOWN)

    def _countdown_step(self, name, remaining):
        if self._closed:
            return
        if remaining > 0:
            self.cal_info.set("Kalibriere '%s' in %d … Maus auf den Punkt!"
                              % (name, remaining))
            self.root.after(1000, self._countdown_step, name, remaining - 1)
            return
        self._countdown_active = False
        self.btn_cal.configure(state="normal")
        self._do_calibrate(name)

    def _do_calibrate(self, name=None):
        if name is None:
            name = self.point_var.get()
        if not name:
            self.log("Kein Punkt gewählt.")
            return
        try:
            res = self.engine.calibrate_point(name)
            self.log("Kalibriert %s: %s" % (name, res))
            self.cal_info.set("'%s' gesetzt: %s" % (name, res))
        except Exception as e:
            self.log("Kalibrierung %s fehlgeschlagen: %s" % (name, e))
            self.cal_info.set("Fehler: %s" % e)
        self.refresh_points()

    def _update_status(self):
        try:
            st = _gui_state_name(self.engine.state)
        except Exception:
            st = "?"
        if st != self._last_state:
            bg, fg = GUI_STATE_COLORS.get(st, ("#333333", "#ffffff"))
            self.status_label.configure(text=st, bg=bg, fg=fg)
            self._last_state = st
            if st in ("PAUSED", "STOPPED"):
                self.btn_toggle.configure(text="▶ Start (F9)")
            else:
                self.btn_toggle.configure(text="⏸ Pause (F9)")
        try:
            dbg = getattr(self.engine, "last_debug", None)
            if isinstance(dbg, dict):
                dbg = dict(dbg)
            text = _gui_format_debug(dbg)
        except Exception:
            text = "Debug: –"
        if text != self._last_debug_text:
            self.debug_var.set(text)
            self._last_debug_text = text

    def _drain_requests(self):
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

    def _drain_log(self):
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

    def _poll(self):
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

    def bind_local_hotkeys(self, calibrate="F8", toggle="F9", emergency="F10"):
        """Fallback, falls globale Hotkeys nicht verfügbar sind (nur wenn das
        Bot-Fenster den Fokus hat)."""
        self.root.bind("<%s>" % calibrate, lambda e: self.calibrate_selected())
        self.root.bind("<%s>" % toggle, lambda e: self.toggle_pause())
        self.root.bind("<%s>" % emergency, lambda e: self.emergency())

    def close(self):
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

    def run(self):
        """Blockiert in mainloop (Main-Thread!)."""
        try:
            self.root.mainloop()
        finally:
            if not self._closed:
                self.close()


def create_gui(engine, config, platform_name, on_close=None):
    """Erzeugt die GUI ohne mainloop zu starten (z.B. um vorher Hotkeys zu
    registrieren). Danach gui.run() aufrufen."""
    return BotGUI(engine, config, platform_name, on_close=on_close)


def run_gui(engine, config, platform_name, on_close=None):
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

    tmpdir = tempfile.mkdtemp(prefix="taxiboss_test_")
    cfg_path = os.path.join(tmpdir, "config.json")
    with open(cfg_path, "w") as f:
        f.write("{kaputt")
    cm = ConfigManager(cfg_path)
    check(cm.get("timing", "tap_duration") == 0.02, "kaputte Config -> Defaults")
    check(any(n.endswith(".bak") for n in os.listdir(tmpdir)), "Backup der kaputten Config")
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
