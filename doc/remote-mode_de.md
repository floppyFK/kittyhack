# Remote-Mode (Remote-Control) — Überblick

**Sprache:** [English](remote-mode.md) | Deutsch

Der Remote-Mode ist eine **optionale, experimentelle** Möglichkeit, Kittyhack in zwei Rollen aufzuteilen:

- **Target-Mode (Kittyflap-Hardware)**: steuert Sensoren/Aktoren (PIR, Magnete/Schlösser, RFID) und stellt einen kleinen „Remote-Control“-Dienst bereit.
- **Remote-Mode (separater Linux-PC/VM)**: führt **Web-UI + Inferenz** auf leistungsfähigerer Hardware aus und steuert das Target über das Netzwerk.

Das ist nützlich, da die Kittyflap-Hardware für Inferenz sehr CPU-limitiert ist und außerdem nicht sehr robust gegenüber EMC ist (bei hoher CPU-Last und/oder viel WLAN-Traffic können z.B. Fehlalarme der PIR-Sensoren auftreten).

---

## Schemata

### Architektur (wer spricht mit wem)

![Remote-Mode Architektur](diagrams/remote-mode-architecture.svg)

## Was läuft wo

### Target-Gerät (Kittyflap)
- Läuft als systemd-Service **`kittyhack_control.service`** (WebSocket-Server auf Port **8888**).
- Läuft normalerweise als **`kittyhack.service`** (Kittyhack UI + Backend auf Port **80**).
- Sobald sich Remote-Mode verbindet und die Kontrolle übernimmt:
  - stoppt das Target **`kittyhack.service`** (Port 80 zeigt dann nicht mehr die normale UI),
  - liefert eine kleine **Info-Seite auf Port 80** aus („dieses Gerät wird remote gesteuert“),
  - behält die Hardware-Ansteuerung lokal und streamt Zustände (PIR, Schlösser, RFID) zum Remote.

### Remote-System (Linux PC/VM)
- Läuft als **`kittyhack.service`** (Web-UI + Backend/Inferenz auf Port **80**).
- Verbindet sich per WebSocket zum Target (`kittyhack_control`), um:
  - PIR- und RFID-Zustände zu lesen,
  - Magnet-/RFID-Kommandos zu senden,
  - optional beim ersten Verbindungsaufbau einen einmaligen Initial-Sync von Datenbank/Konfig/Bildern/Modellen anzustoßen.

---

## Anforderungen

### Hardware / OS (Remote-System)
- **Debian oder Ubuntu** (oder ein ähnliches Derivat)
- **AMD64 (x86_64)** Maschine (normale 64-bit Intel/AMD PCs)
- **≥ 2 GB RAM**

### Software (Remote-System)
Das Setup-Script installiert (Remote-Mode Option):
- Python-Tools (`python3`, `python3-venv`, `python3-pip`) und eine **Python 3.11** Virtualenv
- Tools: `rsync`, `git`, `curl`, `ca-certificates`
- OpenCV Runtime Libs: `libgl1`, `libglib2.0-0`

### Target-Gerät
- Kittyhack ist wie gewohnt auf der Kittyflap installiert.
- `kittyhack_control.service` muss auf dem Target laufen.

### Netzwerk
- Das Remote-System muss das Target erreichen auf:
  - **TCP 8888** (WebSocket Remote-Control)
  - **TCP 80** (optional: Info-Seite auf dem Target; für Control nicht zwingend nötig)
- Die UI auf dem Remote-System läuft auf **TCP 80**.

**Security-Hinweis:** Der Remote-Control WebSocket ist für ein **vertrauenswürdiges LAN** gedacht. Port 8888 nicht ins öffentliche Internet exponieren.

---

## Setup-Anleitung

### Control-Übernahme (was passiert beim Verbinden)

![Remote-Mode Control-Übernahme Ablauf](diagrams/remote-mode-control-takeover.svg)

### 1) Target-Mode auf der Kittyflap installieren / prüfen
Kittyhack wie gewohnt auf der Kittyflap installieren (Software-Version **2.5.0** oder höher).

Services auf dem Target prüfen:

```bash
sudo systemctl status kittyhack
sudo systemctl status kittyhack_control
```

### 2) Remote-Mode auf dem Linux PC/VM installieren
Setup-Script auf dem Remote-System ausführen und die **Remote-Mode** Option auswählen.

### 3) Erster Start: Remote-Mode Setup im Web-UI
Beim ersten Start im Remote-Mode muss die Remote-IP-Adresse (Kittyflap Target) konfiguriert werden.

Remote-UI im Browser öffnen:

- `http://<IP-des-Remote-Systems>/`

### 4) IP-Kamera konfigurieren
Remote-Mode benötigt aktuell eine **IP-Kamera** (der interne Kittyflap Kamera-Stream wird im Remote-Mode nicht verwendet).

Die IP-Kamera-URL in den Kittyhack-Einstellungen im Remote-UI setzen.

---

## Initial-Sync (bei erster Verbindung)

Bei der ersten Verbindung (optional) kann der Remote-Mode einen Sync vom Target anfordern.

Mögliche Inhalte vom Target:
- `kittyhack.db`
- `config.ini`
- Bilder-Verzeichnis (falls aktiviert)
- YOLO Modell-Verzeichnis (falls aktiviert)

Hinweise:
- Das kann **groß** werden, wenn Bilder mit synchronisiert werden.
- Auf dem Remote-System wird eine Marker-Datei (`<kittyhack.db>.remote_synced`) erstellt, um wiederholte Sync-Anfragen zu vermeiden.
  - Wenn du erneut synchronisieren willst, diese Marker-Datei löschen.

---

## Was passiert, wenn die Verbindung verloren geht?

![Remote-Mode Verbindungsverlust Recovery](diagrams/remote-mode-connection-loss.svg)

### Kittyflap Verhalten (Safety / Fallback)
Wenn der Remote-Controller die Verbindung trennt oder länger als das konfigurierte Timeout keine Keepalives sendet:
- die Kittyflap **gibt die Remote-Control frei**,
- Sicherheitsaktionen werden ausgeführt:
  - RFID-Read wird gestoppt
  - RFID-Feld/Power wird deaktiviert
  - Magnet-Queue wird geleert (Shutdown)
- die Kittyflap beendet die temporäre Info-Seite auf Port 80,
- die Kittyflap **startet `kittyhack.service` neu**, damit sie wieder eigenständig laufen kann.

Kurz: Remote-Mode ist so ausgelegt, dass sich die Kittyflap beim Verbindungsverlust selbst erholen kann und weiter autonom läuft.

### Remote Verhalten
- Der Remote-Client loggt den Disconnect und versucht **erneut zu verbinden** (Exponentielles Backoff).
- Bis zur Wiederverbindung können „Remote Hardware“-Zustände (PIR/RFID/Locks) stehen bleiben bzw. nicht aktualisiert werden.

---

## Troubleshooting

- **Remote kann sich nicht mit der Kittyflap verbinden**
  - Prüfen, ob das Target erreichbar ist: `ping <kittyflap-ip>`
  - Prüfen, ob `kittyhack_control` lauscht: `sudo ss -ltnp | grep 8888`
  - Firewalls zwischen Remote und Kittyflap prüfen.

- **Kittyflap zeigt eine Info-Seite statt Kittyhack UI**
  - Das bedeutet: Ein Remote-Controller hat aktuell die Kontrolle (oder hat noch nicht sauber freigegeben).
  - Timeout abwarten oder `kittyhack` auf der Kittyflap neu starten:
    - `sudo systemctl restart kittyhack`

- **Remote-Mode deaktivieren**
  - Marker-Datei `.remote-mode` im Kittyhack-Ordner löschen und den Service neu starten.

---

## Referenz: Konfig-Dateien

- `config.ini`: geteilte Einstellungen (kann beim Initial-Sync vom Target kommen)
- `config.remote.ini`: **nur lokal auf dem Remote-System** (Overlay für Host/Port/Timeout/Sync-Optionen)
