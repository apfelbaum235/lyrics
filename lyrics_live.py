"""
lyrics_live.py

Erkennt den gerade laufenden Song über das Mikrofon (bzw. eine System-Audio-
Schleife) und zeigt anschließend zeitgestempelte Lyrics passend zum Songverlauf an.

Ablauf:
  1. Kurzer Audio-Schnipsel wird aufgenommen (Standard: 8 Sekunden).
  2. Der Schnipsel wird über die (inoffizielle) Shazam-API erkannt.
  3. Zeitgestempelte Lyrics (LRC-Format) werden bei lrclib.net gesucht.
  4. Die Lyrics werden im Terminal passend zur geschätzten Songposition
     ausgegeben.

Hinweis zur Synchronität:
  Die Startposition wird geschätzt (Shazam-Offset + verstrichene Zeit seit
  Aufnahmebeginn). Das ist kein frame-genaues Sync, reicht aber für ein
  brauchbares "Mitlese"-Erlebnis. Bei Bedarf einfach neu erkennen lassen,
  um die Anzeige wieder anzugleichen (siehe --resync).
"""

import argparse
import asyncio
import re
import sys
import tempfile
import time

import requests
import sounddevice as sd
import soundfile as sf
from shazamio import Shazam

try:
    import msvcrt  # Windows-only, aber Ziel-Plattform ist Windows
except ImportError:
    msvcrt = None

SAMPLE_RATE = 44100
DEFAULT_RECORD_SECONDS = 8
LRCLIB_BASE = "https://lrclib.net/api"


def record_snippet(duration: float, samplerate: int = SAMPLE_RATE):
    """Nimmt einen Audio-Schnipsel vom aktuellen Standard-Eingabegerät auf."""
    print(f"🎙️  Nehme {duration:.0f}s Audio auf … (Musik sollte jetzt laufen)")
    audio = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=1, dtype="float32")
    sd.wait()
    return audio, samplerate


def save_wav(audio, samplerate: int, path: str):
    sf.write(path, audio, samplerate)


async def recognize(path: str) -> dict:
    shazam = Shazam()
    return await shazam.recognize(path)


def get_lrc_lyrics(artist: str, title: str, duration: float | None = None) -> str | None:
    """Fragt lrclib.net nach zeitgestempelten Lyrics an (mit Fallback auf Suche)."""
    params = {"track_name": title, "artist_name": artist}
    if duration:
        params["duration"] = int(round(duration))

    try:
        resp = requests.get(f"{LRCLIB_BASE}/get", params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("syncedLyrics"):
                return data["syncedLyrics"]
    except requests.RequestException:
        pass

    try:
        resp = requests.get(
            f"{LRCLIB_BASE}/search",
            params={"track_name": title, "artist_name": artist},
            timeout=10,
        )
        if resp.status_code == 200:
            for entry in resp.json():
                if entry.get("syncedLyrics"):
                    return entry["syncedLyrics"]
    except requests.RequestException:
        pass

    return None


LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")


def parse_lrc(lrc_text: str) -> list[tuple[float, str]]:
    """Wandelt LRC-Text in eine sortierte Liste aus (Sekunde, Zeile) um."""
    lines = []
    for raw in lrc_text.splitlines():
        match = LRC_LINE.match(raw)
        if match:
            minutes, seconds, text = match.groups()
            timestamp = int(minutes) * 60 + float(seconds)
            lines.append((timestamp, text.strip()))
    lines.sort(key=lambda entry: entry[0])
    return lines


def calibrate_start_time(lines: list[tuple[float, str]]) -> float:
    """Lässt den Nutzer per ENTER den exakten Startzeitpunkt festlegen.

    Das ist deutlich zuverlässiger als der geschätzte Shazam-Offset, weil
    Netzwerk-Latenz & Erkennungsdauer von Durchlauf zu Durchlauf schwanken.
    """
    first_idx = next((i for i, (_, text) in enumerate(lines) if text), 0)
    timestamp0, text0 = lines[first_idx]
    print(f'\n📍 Kalibrierung: Drücke ENTER genau in dem Moment, in dem diese Zeile gesungen wird:')
    print(f'   "{text0}"')
    input("   (Enter drücken, sobald es soweit ist) ")
    return time.time() - timestamp0


def play_synced_lyrics(lines: list[tuple[float, str]], start_time: float):
    """Gibt die Lyrics zeitlich passend zur Songposition aus.

    Während der Wiedergabe kann mit '+' vorgespult (Zeilen erscheinen
    früher) bzw. mit '-' verzögert werden (Zeilen erscheinen später) –
    nützlich, wenn die Sync im Laufe des Songs wegdriftet.
    """
    step = 0.3
    print("\n🎵 Live-Lyrics ('+' schneller, '-' langsamer, Strg+C zum Beenden):\n")
    try:
        for timestamp, text in lines:
            while True:
                now = time.time() - start_time
                wait = timestamp - now
                if wait <= 0:
                    break
                if msvcrt is not None and msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key in (b"+", b"="):
                        start_time -= step
                    elif key in (b"-", b"_"):
                        start_time += step
                    continue
                time.sleep(min(wait, 0.05))
            if text:
                print(text)
    except KeyboardInterrupt:
        print("\n⏹️  Beendet.")


async def main():
    parser = argparse.ArgumentParser(description="Song erkennen und Live-Lyrics anzeigen")
    parser.add_argument(
        "--seconds", type=float, default=DEFAULT_RECORD_SECONDS,
        help="Länge des Audio-Schnipsels zur Erkennung (Standard: 8s)",
    )
    parser.add_argument(
        "--device", type=int, default=None,
        help="Index des Audio-Eingabegeräts (siehe 'python -m sounddevice' zum Auflisten)",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Keine manuelle Kalibrierung per ENTER, sondern automatische Schätzung "
             "über den Shazam-Offset (weniger genau, dafür ohne Interaktion).",
    )
    parser.add_argument(
        "--adjust", type=float, default=1.0,
        help="Nur mit --auto relevant: Korrektur in Sekunden für systematischen "
             "Vor-/Nachlauf. Positiv = Lyrics erscheinen früher.",
    )
    args = parser.parse_args()

    if args.device is not None:
        sd.default.device = args.device

    audio, sr = record_snippet(args.seconds)
    capture_start = time.time() - args.seconds

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        save_wav(audio, sr, tmp.name)
        result = await recognize(tmp.name)

    track = result.get("track")
    if not track:
        print("❌ Song konnte nicht erkannt werden. Versuch's nochmal – am besten mit klarer, "
              "lauter Musik ohne viel Nebengeräusch.")
        sys.exit(1)

    title = track.get("title", "?")
    artist = track.get("subtitle", "?")
    print(f"✅ Erkannt: {artist} – {title}")

    lrc = get_lrc_lyrics(artist, title)
    if not lrc:
        print("⚠️  Keine zeitsynchronen Lyrics gefunden (lrclib.net hat für diesen Song nichts "
              "Passendes). Prüfe ggf. Künstler-/Titelschreibweise oder nutze eine andere Quelle.")
        return

    lines = parse_lrc(lrc)
    if not lines:
        print("⚠️  Lyrics gefunden, aber sie enthalten keine Zeitstempel.")
        return

    if args.auto:
        # Offset innerhalb des Songs schätzen, falls Shazam einen liefert.
        offset = 0.0
        try:
            offset = float(result["matches"][0]["offset"])
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        elapsed_since_capture_start = time.time() - capture_start
        estimated_position = offset + elapsed_since_capture_start + args.adjust
        start_time = time.time() - estimated_position
    else:
        start_time = calibrate_start_time(lines)

    play_synced_lyrics(lines, start_time)


if __name__ == "__main__":
    asyncio.run(main())
