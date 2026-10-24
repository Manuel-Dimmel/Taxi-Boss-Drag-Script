# Taxi Boss Drag Bot V2

Automatisiert Drag-Races in **Roblox „Taxi Boss“**: Nadel an der Ampel halten (Auto-Start), perfekter Start bei GO, automatisches Schalten (Auto-Shift) und Weiterklicken der Menüs (Auto-Skip).

| Datei | Inhalt |
|---|---|
| `core_utils.py` | Engine, Bilderkennung, Config, GUI (plattformunabhängig) |
| `mac_bot.py` | Starter für macOS (Quartz-Events, globale Hotkeys) |
| `win_bot.py` | Starter für Windows (pydirectinput, keyboard) |
| `config.json` | Punkte, Farben, Timings (wird von der GUI gespeichert) |

---


## 1. Installation

Python **3.9+** wird benötigt.

**macOS**
```bash
cd ~/PycharmProjects/Taxi-Boss-Drag-Script-V2
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 mac_bot.py
```

**Windows**
```bat
cd Taxi-Boss-Drag-Script-V2
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python win_bot.py
```

Fehlt ein Paket, meldet der Starter das beim Start mit dem passenden `pip install …`-Befehl.

---

## 2. macOS-Berechtigungen (wichtig!)

**Systemeinstellungen → Datenschutz & Sicherheit** – jeweils die App freischalten, aus der du den Bot startest (**Terminal**, **iTerm** oder **PyCharm**):

| Berechtigung | Wofür | Symptom, wenn sie fehlt |
|---|---|---|
| **Bildschirmaufnahme** | Pixel lesen (Ampel, Nadel, Buttons) | Nur Hintergrundbild / schwarze Pixel, nichts wird erkannt |
| **Bedienungshilfen** | Tasten W/E und Klicks an Roblox senden | Bot „läuft“, aber im Spiel passiert nichts |
| **Eingabeüberwachung** | Globale Hotkeys F8/F9/F10 | Hotkeys reagieren nur, wenn das Bot-Fenster aktiv ist |

> Nach dem Erteilen die App (Terminal/PyCharm) **komplett beenden und neu starten**.
> Das Log zeigt beim Start, ob Bedienungshilfen fehlen und welcher Hotkey-Modus aktiv ist.

F-Tasten auf dem Mac: Falls F8/F9/F10 Medienfunktionen auslösen, **fn** gedrückt halten oder unter *Tastatur → Tastaturkurzbefehle → Funktionstasten* „F1, F2 usw. als Standard-Funktionstasten verwenden“ aktivieren.

**Windows:** Reagieren die Hotkeys in Roblox nicht, die Konsole „Als Administrator ausführen“. Die DPI-Skalierung wird automatisch berücksichtigt.

---

## 3. Roblox vorbereiten

* Roblox im **Vollbild** auf dem **Hauptmonitor** laufen lassen.
* Immer **dieselbe Auflösung / Fenstergröße** wie beim Kalibrieren verwenden (Punkte sind relativ gespeichert, aber UI-Layouts ändern sich bei anderen Seitenverhältnissen).
* Das Bot-Fenster daneben/oben platzieren (Checkbox „Immer im Vordergrund“), es darf die Messpunkte nicht verdecken.
* Vor dem Start mit **F9** einmal ins Roblox-Fenster klicken, damit es den Tastaturfokus hat.

---

## 4. Kalibrierung

Tab **„Kalibrierung“**:

1. Punkt im Dropdown wählen (oder in der Tabelle anklicken).
2. **„Kalibrieren (3s Countdown)“** klicken und die Maus innerhalb von 3 s auf die Stelle im Spiel halten – **oder** Maus auf die Stelle legen und **F8** drücken (setzt den gewählten Punkt sofort).
3. Position (relativ) + gemessene Farbe werden in `config.json` gespeichert und in der Tabelle angezeigt.

| Punkt | Wo hinzeigen |
|---|---|
| `light_box_1`, `light_box_2` | Grauer (normal) bzw. dunkelroter (Boss) Ampelkasten |
| `light_lamp` | Rote Ampellampe |
| `tacho_center` | Mittelpunkt des Drehzahlmessers |
| `needle_target` | Ziel-Drehzahl (wohin die Nadel beim Start soll) |
| `shift_circle` | Kreis, der zum Schalten grün wird |
| `btn_weiter` | „Weiter“-Button nach dem Rennen |
| `btn_next_stage` | „Nächste Stufe“-Button |
| `btn_rennen` | „Rennen“-Button |

Tipp: Ampel/Buttons am besten kalibrieren, während sie im Spiel sichtbar sind.

### Vorkalibrierte Standardwerte

Alle Punkte sind schon aus den 6 Screenshots eingemessen (MacBook-Retina-Vollbild, 1710×1107 logisch / 3420×2214 physisch). Bei gleicher Auflösung musst du **nichts kalibrieren**.

* **Ampel:** Normal dunkelgrau `(54,55,59)`, Boss dunkelrot `(142,57,60)`, zusätzlich die leuchtend rote Lampe `(197,66,61)`. An denselben Punkten ist im GO-Bild Himmel → GO wird erkannt.
* **Blaue Zielzone:** Wird **live** auf dem Tacho-Ring erkannt (normal ≈ 87°–140°, Boss nur ≈ 93°–104°). Die Nadel wird auf die Mitte der erkannten Zone geregelt. Wird keine Zone gefunden, nutzt der Bot `needle_target` (99°, liegt in beiden Zonen).
* **WEITER-Punkt:** Liegt absichtlich links unterhalb des Schriftzugs. Der Tacho-Ring ist im Rennen nämlich **genauso grün** wie der Button (`(74,143,52)` gegenüber `(82,146,50)`). Diesen Punkt nicht auf den Tacho-Ring legen!
* **CASH OUT** `(245,189,89)` wird über `is_orange_or_yellow` ausgeschlossen. Geklickt wird nur, wenn alle 5 Probepixel grün sind und keines orange ist.

---

## 5. Hotkeys

| Taste | Funktion |
|---|---|
| **F8** | Gewählten Kalibrierpunkt an aktueller Mausposition setzen |
| **F9** | Start / Pause (nach NOT-AUS: Engine neu starten) |
| **F10** | **NOT-AUS** – lässt W/E sofort los und stoppt den Bot |

Fenster schließen oder `Strg+C` im Terminal lassen ebenfalls alle Tasten los.

---

## 6. Module

| Modul | Funktion |
|---|---|
| **Auto-Start** | An der Ampel: Nadel per Micro-Taps auf `W` exakt am Ziel halten; bei GO `W` durchdrücken. |
| **Auto-Shift** | Im Rennen `E` drücken, sobald `shift_circle` grün wird. |
| **Auto-Skip** | Grüne Buttons (Weiter / Nächste Stufe / Rennen) automatisch klicken; orange Cash-Out-Buttons werden ignoriert. |

Module wirken sofort beim An-/Abhaken und werden gespeichert.

---

## 7. Parameter-Tipps

| Parameter | Standard | Bedeutung / Tipp |
|---|---|---|
| Tap Duration | 0.020 s | Wie lange `W` pro Micro-Tap gedrückt wird. **Boss-Rennen: kürzer** (z.B. 0.010–0.015), da die Nadel dort schneller steigt. |
| Tap Cooldown | 0.025 s | Pause zwischen Taps. Größer = Nadel fällt mehr zurück. |
| Shift Cooldown | 0.25 s | Mindestabstand zwischen zwei Schaltvorgängen (verhindert Doppelschalten). |
| Menu Click Delay | 0.5 s | Wartezeit nach einem Menü-Klick. Bei langsamen Ladezeiten erhöhen. |
| Loop Interval | 0.004 s | Schleifentakt. Kleiner = reaktiver, mehr CPU. |
| GO Confirm Frames | 3 | So viele Frames muss die Ampel weg sein, bevor GO erkannt wird. Fehlstarts → erhöhen; zu später Start → verringern. |
| Shift Tap Duration | 0.03 s | Wie lange `E` gedrückt wird. |
| Ziel-Offset | 0.0° | Feinjustierung des Nadelziels in Grad (positiv/negativ ausprobieren). |
| Ziel in blauer Zone | 0.5 | 0 = Zonenanfang, 0.5 = Mitte, 1 = Zonenende (Richtung höhere Drehzahl). |

Nach Änderungen **„Speichern“** klicken – ungültige Werte werden rot gemeldet und nicht übernommen.

---

## 8. Fehlerbehebung

| Problem | Lösung |
|---|---|
| Nichts wird erkannt / Debug-Zeile leer | macOS: Bildschirmaufnahme-Recht; Punkte neu kalibrieren; gleiche Auflösung wie beim Kalibrieren. |
| Erkennung ok, aber keine Tasten im Spiel | macOS: Bedienungshilfen-Recht; Roblox muss Fokus haben. Windows: als Administrator starten. |
| Hotkeys gehen nicht | macOS: Eingabeüberwachung + `fn`-Taste; Windows: Administrator. Notfalls Buttons in der GUI nutzen. |
| Klicks landen daneben | Neu kalibrieren; Windows: Anzeigeskalierung nicht während des Betriebs ändern; macOS: Roblox auf Hauptmonitor. |
| Nadel überschießt | Tap Duration verkleinern, Tap Cooldown erhöhen oder „Ziel in blauer Zone“ verkleinern (z.B. 0.3). |
| Nadel bleibt zu niedrig | Tap Duration erhöhen bzw. Tap Cooldown verkleinern. |
| Fehlstart | GO Confirm Frames erhöhen, `light_box`/`light_lamp` neu kalibrieren. |
| `W` bleibt hängen | **F10** (NOT-AUS) oder Fenster schließen – alle Tasten werden losgelassen. |
| `config.json` kaputt | Wird automatisch durch Standardwerte ersetzt (Backup bleibt erhalten). |
| `ModuleNotFoundError` | venv aktivieren und `pip install -r requirements.txt`. |

Status-Farben: **PAUSED** grau · **WAITING** blau · **AT_LIGHT** gelb · **RACING** grün · **MENU_SKIP** lila · **STOPPED** rot.
