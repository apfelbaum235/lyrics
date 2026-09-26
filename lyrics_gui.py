"""
lyrics_gui.py

Grafische Oberfläche für Live-Lyrics im Stil von Apple Music.

- Startet die Mikrofon-Aufnahme automatisch beim Öffnen des Fensters.
- Beendet wird nur durch Schließen des Fensters (kein Button nötig).
- Erkennt automatisch den nächsten Song, sobald der aktuelle zu Ende geht
  (schnelleres Polling gegen Songende).
- Die ersten 30 Sekunden eines Songs wird die Sync-Position laufend fein
  nachjustiert; danach wird sie "eingefroren", damit z.B. Tastatur- oder
  Umgebungsgeräusche die laufende Synchronität nicht mehr durcheinander
  bringen. Ein Songwechsel wird davon unabhängig weiterhin sofort erkannt.
- Zeilenübergänge sind zeitbasiert animiert und blenden an beiden Rändern
    weich aus.
- Inaktive Zeilen werden mit versetzten Textlagen weichgezeichnet.
- Der gesamte Hintergrund ist eine weichgezeichnete, abgedunkelte Version
  des Album-Covers.

Nutzt die Erkennungs- und Lyrics-Logik aus lyrics_live.py (muss im selben
Ordner liegen).

Feinjustierung der Sync per Tastatur weiterhin möglich (nur in den ersten
30s wirksam, danach eingefroren): '+' schneller, '-' langsamer.
"""

import asyncio
import io
import math
import tempfile
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

import numpy as np
import requests
import sounddevice as sd
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

from lyrics_live import get_lrc_lyrics, parse_lrc, recognize, save_wav

WINDOW_W, WINDOW_H = 520, 880
BG_FALLBACK = "#0b0b0d"
BG_APPROX = (18, 14, 16)  # grobe Näherung an den abgedunkelten Hintergrund

FG_ACTIVE = (255, 255, 255)
FG_DIM = (150, 150, 155)

CONTEXT_LINES = 2
NUM_SLOTS = 2 * CONTEXT_LINES + 3
SNIPPET_SECONDS = 8
RESYNC_INTERVAL = 15      # normales Abstand zwischen Erkennungen
FAST_INTERVAL = 4         # kurz vor/nach Songende: öfter prüfen
END_APPROACH_SECONDS = 20 # ab wann "kurz vor Songende" gilt
SYNC_LOCK_SECONDS = 30    # danach wird die Sync-Position eingefroren
NUDGE_STEP = 0.3

ACTIVE_SIZE = 23
FAR_SIZE = 16
LINE_GAP = 42
ACTIVE_EXTRA_GAP = 26
ANIM_DURATION = 0.35      # Sekunden pro Zeilenwechsel-Animation
FRAME_MS = 16             # ~60 fps

COVER_SIZE = 230
TOP_PAD = 50
COVER_Y = TOP_PAD + COVER_SIZE // 2
TITLE_Y = COVER_Y + COVER_SIZE // 2 + 34
ARTIST_Y = TITLE_Y + 30
STATUS_Y = ARTIST_Y + 26

LYRICS_TOP = STATUS_Y + 55
LYRICS_BOTTOM = WINDOW_H - 40
LYRICS_CENTER_Y = (LYRICS_TOP + LYRICS_BOTTOM) // 2
FADE_ZONE = 70            # früheres Ein- und Ausblenden an beiden Lyrics-Rändern

BLUR_OFFSETS = ((-1.3, -1.3), (1.3, -1.3), (-1.3, 1.3), (1.3, 1.3))
BLUR_BLEND = 0.58         # wie stark die weichen Textlagen Richtung Hintergrund gehen

BG_BLUR_RADIUS = 45
BG_DARKEN = 0.5


def line_offset(relative: float) -> float:
    """Vertikaler Versatz einer Zeile relativ zur aktiven Zeile, mit Extra-
    Puffer direkt neben der aktiven (größeren) Zeile."""
    if relative == 0:
        return 0.0
    sign = 1.0 if relative > 0 else -1.0
    a = min(abs(relative), 1.0)
    rest = max(abs(relative) - 1.0, 0.0)
    return sign * (a * (LINE_GAP + ACTIVE_EXTRA_GAP) + rest * LINE_GAP)


def rounded_image(img: Image.Image, radius: int) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)
    img.putalpha(mask)
    return img


def make_background(img: Image.Image) -> Image.Image:
    bg = ImageOps.fit(img.convert("RGB"), (WINDOW_W, WINDOW_H), method=Image.LANCZOS)
    bg = bg.filter(ImageFilter.GaussianBlur(BG_BLUR_RADIUS))
    dark = Image.new("RGB", bg.size, (0, 0, 0))
    return Image.blend(bg, dark, BG_DARKEN)


def lerp_rgb(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def rgb_hex(c):
    return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"


class ContinuousRecorder:
    """Nimmt durchgehend Audio in einen Ringpuffer auf, ohne Aussetzer."""

    def __init__(self, samplerate=44100, buffer_seconds=12, device=None):
        self.samplerate = samplerate
        self.buffer_len = int(buffer_seconds * samplerate)
        self.buffer = np.zeros(self.buffer_len, dtype="float32")
        self.write_pos = 0
        self.filled = 0
        self.lock = threading.Lock()
        self.stream = sd.InputStream(
            samplerate=samplerate, channels=1, dtype="float32",
            device=device, callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status):
        data = indata[:, 0]
        n = len(data)
        with self.lock:
            end = self.write_pos + n
            if end <= self.buffer_len:
                self.buffer[self.write_pos:end] = data
            else:
                first_part = self.buffer_len - self.write_pos
                self.buffer[self.write_pos:] = data[:first_part]
                self.buffer[: end - self.buffer_len] = data[first_part:]
            self.write_pos = end % self.buffer_len
            self.filled = min(self.filled + n, self.buffer_len)

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

    def available_seconds(self):
        return self.filled / self.samplerate

    def snapshot(self, seconds):
        n = min(int(seconds * self.samplerate), self.filled)
        with self.lock:
            end = self.write_pos
            start = (end - n) % self.buffer_len
            if start < end:
                data = self.buffer[start:end].copy()
            else:
                data = np.concatenate([self.buffer[start:], self.buffer[:end]])
        end_walltime = time.time()
        start_walltime = end_walltime - (len(data) / self.samplerate)
        return data.reshape(-1, 1), start_walltime


class LyricsApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Live Lyrics")
        self.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.configure(bg=BG_FALLBACK)
        self.resizable(False, False)

        self.lines: list[tuple[float, str]] = []
        self.current_title = None
        self.current_artist = None
        self.start_time = None
        self.manual_adjust = 0.0
        self.target_index = 0
        self.scroll_pos = 0.0
        self.anim_start_time = None
        self.anim_start_value = 0.0
        self.anim_target = 0
        self.ticking = False
        self.live = False
        self.recorder = None
        self._img_refs = []
        self._font_cache = {}
        self._rendered_state = {}

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        for key in ("<plus>", "<KP_Add>"):
            self.bind(key, lambda e: self._nudge(-NUDGE_STEP))
        for key in ("<minus>", "<KP_Subtract>"):
            self.bind(key, lambda e: self._nudge(NUDGE_STEP))

        self.after(200, self._start_live)

    # ---------- Hilfsfunktionen ----------

    def _font(self, size, weight):
        key = (size, weight)
        f = self._font_cache.get(key)
        if f is None:
            f = tkfont.Font(family="Segoe UI", size=size, weight=weight)
            self._font_cache[key] = f
        return f

    # ---------- UI-Aufbau (eine einzige Canvas) ----------

    def _build_ui(self):
        self.canvas = tk.Canvas(self, width=WINDOW_W, height=WINDOW_H,
                                 bg=BG_FALLBACK, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.bg_id = self.canvas.create_rectangle(0, 0, WINDOW_W, WINDOW_H,
                                                    fill=BG_FALLBACK, outline="")

        self.cover_id = self.canvas.create_image(WINDOW_W // 2, COVER_Y, image=None)

        self.title_id = self.canvas.create_text(
            WINDOW_W // 2, TITLE_Y, text="", fill="#ffffff",
            font=self._font(17, "bold"), width=WINDOW_W - 80, justify="center",
        )
        self.artist_id = self.canvas.create_text(
            WINDOW_W // 2, ARTIST_Y, text="", fill="#c7c7ca",
            font=self._font(13, "normal"), width=WINDOW_W - 80, justify="center",
        )
        self.status_id = self.canvas.create_text(
            WINDOW_W // 2, STATUS_Y, text="🎙️ Höre zu …", fill="#c7c7ca",
            font=self._font(12, "normal"), width=WINDOW_W - 80, justify="center",
        )

        # Versetzte Textlagen erzeugen für inaktive Zeilen einen weichen Blur.
        self.blur_ids = []
        self.slot_ids = []
        for _ in range(NUM_SLOTS):
            layers = []
            for dx, dy in BLUR_OFFSETS:
                layers.append(self.canvas.create_text(
                    WINDOW_W // 2 + dx, -200 + dy, text="",
                    fill="#000000", font=self._font(FAR_SIZE, "bold"),
                    width=WINDOW_W - 64, justify="center",
                ))
            self.blur_ids.append(layers)
        for _ in range(NUM_SLOTS):
            sid = self.canvas.create_text(
                WINDOW_W // 2, -200, text="", fill="#000000",
                font=self._font(FAR_SIZE, "bold"), width=WINDOW_W - 64, justify="center",
            )
            self.slot_ids.append(sid)

    # ---------- Start/Stop der Live-Erkennung ----------

    def _start_live(self):
        self.live = True
        self.recorder = ContinuousRecorder(buffer_seconds=SNIPPET_SECONDS + 4)
        self.recorder.start()
        threading.Thread(target=self._live_loop, daemon=True).start()

    def _stop_live(self):
        self.live = False
        self.ticking = False
        if self.recorder:
            self.recorder.stop()
            self.recorder = None

    def _on_close(self):
        self._stop_live()
        self.destroy()

    def _next_interval(self):
        """Kurz vor (oder nach) Songende öfter prüfen, damit der nächste
        Song schnell erkannt wird."""
        if not self.lines or self.start_time is None:
            return FAST_INTERVAL
        last_ts = self.lines[-1][0]
        now = time.time() - self.start_time
        remaining = last_ts - now
        if remaining < END_APPROACH_SECONDS:
            return FAST_INTERVAL
        return RESYNC_INTERVAL

    def _live_loop(self):
        recorder = self.recorder
        while self.live and recorder.available_seconds() < SNIPPET_SECONDS:
            time.sleep(0.5)

        while self.live:
            data, start_walltime = recorder.snapshot(SNIPPET_SECONDS)
            self._process_snapshot(data, start_walltime)
            interval = self._next_interval()
            waited = 0.0
            while self.live and waited < interval:
                time.sleep(0.5)
                waited += 0.5

    def _process_snapshot(self, data, start_walltime):
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                save_wav(data, self.recorder.samplerate, tmp.name)
                result = asyncio.run(recognize(tmp.name))
        except Exception:
            return

        track = result.get("track")
        if not track:
            if not self.lines:
                self._set_status("🔍 Höre zu …")
            return

        title = track.get("title", "?")
        artist = track.get("subtitle", "?")

        offset = None
        try:
            offset = float(result["matches"][0]["offset"])
        except (KeyError, IndexError, TypeError, ValueError):
            offset = None

        is_new_song = (title, artist) != (self.current_title, self.current_artist)

        if is_new_song:
            self.current_title = title
            self.current_artist = artist
            self.manual_adjust = 0.0
            images = track.get("images") or {}
            cover_url = images.get("coverarthq") or images.get("coverart")
            self._set_song_info(title, artist, cover_url)
            self._set_status("📝 Suche Lyrics …")

            lrc = get_lrc_lyrics(artist, title)
            lines = parse_lrc(lrc) if lrc else []
            self.lines = lines
            self.ticking = False

            if not lines:
                self._set_status("⚠️ Keine zeitsynchronen Lyrics gefunden.")
                self.after(0, self._hide_lyrics)
                return
        else:
            # Gleicher Song: nach Ablauf des Sync-Fensters keine weiteren
            # Zeit-Korrekturen mehr vornehmen (robust gegen Störgeräusche).
            if self.start_time is not None:
                elapsed = time.time() - self.start_time
                if elapsed > SYNC_LOCK_SECONDS:
                    return

        if offset is not None and self.lines:
            self.start_time = start_walltime - offset + self.manual_adjust

            if is_new_song:
                now = time.time() - self.start_time
                idx = 0
                for i, (ts, _) in enumerate(self.lines):
                    if ts <= now:
                        idx = i
                    else:
                        break
                self.target_index = idx
                self.scroll_pos = float(idx)
                self.anim_start_time = None
                self._set_status("")

            if not self.ticking:
                self.ticking = True
                self.after(0, self._animate)

    def _set_status(self, text):
        self.after(0, lambda: self.canvas.itemconfigure(self.status_id, text=text))

    def _hide_lyrics(self):
        for sid in self.slot_ids:
            self._set_text_item(sid, "", "", None)
        for layers in self.blur_ids:
            for item_id in layers:
                self._set_text_item(item_id, "", "", None)

    def _set_song_info(self, title, artist, cover_url):
        def apply_text():
            self.canvas.itemconfigure(self.title_id, text=title)
            self.canvas.itemconfigure(self.artist_id, text=artist)
        self.after(0, apply_text)

        if not cover_url:
            return
        try:
            resp = requests.get(cover_url, timeout=10)
            base_img = Image.open(io.BytesIO(resp.content)).convert("RGBA")

            bg_img = make_background(base_img)
            cover_img = rounded_image(base_img.resize((COVER_SIZE, COVER_SIZE)), radius=24)

            bg_photo = ImageTk.PhotoImage(bg_img)
            cover_photo = ImageTk.PhotoImage(cover_img)

            def apply_images():
                self.canvas.delete(self.bg_id)
                self.bg_id = self.canvas.create_image(0, 0, anchor="nw", image=bg_photo)
                self.canvas.tag_lower(self.bg_id)
                self.canvas.itemconfigure(self.cover_id, image=cover_photo)
                self._img_refs = [bg_photo, cover_photo]

            self.after(0, apply_images)
        except Exception:
            pass

    # ---------- Lyrics-Sync & sanfte, zeitbasierte Animation ----------

    def _nudge(self, delta):
        self.manual_adjust += delta
        if self.start_time is not None:
            self.start_time += delta

    def _check_index(self):
        if not self.ticking or self.start_time is None:
            return
        now = time.time() - self.start_time
        idx = self.target_index
        for i, (ts, _) in enumerate(self.lines):
            if ts <= now:
                idx = i
            else:
                break

        if idx != self.target_index:
            self.anim_start_time = time.perf_counter()
            self.anim_start_value = self.scroll_pos
            self.anim_target = idx
            self.target_index = idx

    def _animate(self):
        if not self.ticking:
            return

        self._check_index()
        if self.anim_start_time is None:
            self.scroll_pos = float(self.target_index)
        else:
            elapsed = time.perf_counter() - self.anim_start_time
            t = min(elapsed / ANIM_DURATION, 1.0)
            eased = 1 - (1 - t) ** 3  # ease-out cubic: sanfter Auslauf
            self.scroll_pos = self.anim_start_value + (self.anim_target - self.anim_start_value) * eased
            if t >= 1.0:
                self.anim_start_time = None
                self.scroll_pos = float(self.anim_target)

        self._render_frame(self.scroll_pos)
        self.after(FRAME_MS, self._animate)

    def _render_frame(self, scroll_pos):
        center_x = WINDOW_W // 2
        base = math.floor(scroll_pos)
        half = NUM_SLOTS // 2

        for k in range(NUM_SLOTS):
            sid = self.slot_ids[k]
            layers = self.blur_ids[k]
            line_i = base - half + k

            if line_i < 0 or line_i >= len(self.lines):
                self._set_text_item(sid, "", "", None)
                for item_id in layers:
                    self._set_text_item(item_id, "", "", None)
                continue

            _, text = self.lines[line_i]
            relative = line_i - scroll_pos
            y = LYRICS_CENTER_Y + line_offset(relative)

            if not text or y < LYRICS_TOP - FADE_ZONE or y > LYRICS_BOTTOM + FADE_ZONE:
                self._set_text_item(sid, "", "", None)
                for item_id in layers:
                    self._set_text_item(item_id, "", "", None)
                self.canvas.coords(sid, center_x, y)
                continue

            dist = abs(relative)
            t = min(dist, 1.0)
            size = round(ACTIVE_SIZE - (ACTIVE_SIZE - FAR_SIZE) * t)
            base_color = lerp_rgb(FG_ACTIVE, FG_DIM, t)

            # Lyrics an beiden Kanten weich ein- und ausblenden.
            if y < LYRICS_TOP + FADE_ZONE:
                fade = max(0.0, min(1.0, (y - (LYRICS_TOP - FADE_ZONE)) / (FADE_ZONE * 2)))
                base_color = lerp_rgb(BG_APPROX, base_color, fade)
            elif y > LYRICS_BOTTOM - FADE_ZONE:
                fade = max(0.0, min(1.0, (LYRICS_BOTTOM + FADE_ZONE - y) / (FADE_ZONE * 2)))
                base_color = lerp_rgb(BG_APPROX, base_color, fade)

            color = rgb_hex(base_color)
            f = self._font(size, "bold")

            self.canvas.coords(sid, center_x, y)
            if dist <= 0.35:
                self._set_text_item(sid, text, color, f)
            else:
                self._set_text_item(sid, "", "", None)

            if dist > 0.35:
                blur_color = rgb_hex(lerp_rgb(base_color, BG_APPROX, BLUR_BLEND))
                for item_id, (dx, dy) in zip(layers, BLUR_OFFSETS):
                    self.canvas.coords(item_id, center_x + dx, y + dy)
                    self._set_text_item(item_id, text, blur_color, f)
            else:
                for item_id in layers:
                    self._set_text_item(item_id, "", "", None)

    def _set_text_item(self, item_id, text, color, font):
        state = (text, color, font)
        if self._rendered_state.get(item_id) != state:
            if text:
                self.canvas.itemconfigure(item_id, text=text, fill=color, font=font)
            else:
                self.canvas.itemconfigure(item_id, text="")
            self._rendered_state[item_id] = state


if __name__ == "__main__":
    app = LyricsApp()
    app.mainloop()
