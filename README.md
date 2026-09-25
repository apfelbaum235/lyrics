# Live Lyrics

Erkennt den gerade laufenden Song über dein Mikrofon (oder eine System-Audio-
Schleife) und zeigt passend dazu die Lyrics im Terminal an.

## Installation

```bash
pip install -r requirements.txt
```

Falls `sounddevice` unter Linux Probleme macht, zusätzlich:
```bash
sudo apt install libportaudio2
```

## Benutzung

```bash
python lyrics_live.py
```

Optional:
```bash
python lyrics_live.py --seconds 10          # längerer Erkennungs-Schnipsel
python lyrics_live.py --device 3            # bestimmtes Audio-Gerät nutzen
```

Verfügbare Geräte auflisten:
```bash
python -m sounddevice
```

## Wichtig: Woher kommt das Audiosignal?

Standardmäßig nimmt das Skript vom **Standard-Mikrofon** auf. Das funktioniert
gut, wenn die Musik im Raum läuft (z. B. über Lautsprecher). Willst du
stattdessen den **System-Sound** direkt erkennen (z. B. Spotify am selben
Rechner), brauchst du eine virtuelle Audio-Schleife:

- **Windows:** [VB-Cable](https://vb-audio.com/Cable/) oder Stereo-Mix aktivieren
- **macOS:** [BlackHole](https://existential.audio/blackhole/) oder Loopback
- **Linux:** PulseAudio/PipeWire-Monitor-Quelle des Ausgabegeräts auswählen

Das virtuelle Gerät dann als Eingabegerät per `--device` auswählen.

## Wie funktioniert die Synchronisation?

1. Ein Audio-Schnipsel (Standard 8s) wird aufgenommen und an die
   (inoffizielle) Shazam-API geschickt.
2. Shazam liefert neben Titel/Künstler auch einen ungefähren Zeit-Offset
   innerhalb des Songs.
3. Dazu wird die seit Aufnahmebeginn verstrichene Zeit addiert, um die
   aktuelle Songposition zu schätzen.
4. Zeitgestempelte Lyrics (LRC-Format) werden bei [lrclib.net](https://lrclib.net)
   gesucht und passend zur geschätzten Position ausgegeben.

**Das ist eine Schätzung, kein frame-genauer Sync.** Über die Zeit kann es zu
leichtem Drift kommen (Netzwerklatenz, ungenauer Offset etc.). Einfach das
Skript neu starten, um neu zu synchronisieren.

## Einschränkungen

- Nicht jeder Song hat zeitgestempelte Lyrics bei lrclib.net – bei sehr
  neuen, obskuren oder nicht-englischen Songs kann die Suche leer ausgehen.
- Die Shazam-API ist inoffiziell (über `shazamio`) und kann sich ändern oder
  zeitweise limitiert sein.
- Instrumentalstücke oder sehr leise/verrauschte Aufnahmen werden oft nicht
  erkannt – am besten mit klarer, deutlich hörbarer Musik testen.
