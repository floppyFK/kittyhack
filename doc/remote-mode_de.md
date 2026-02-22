# Remote-Mode (Remote-Control) — Überblick

Der Remote-Mode ist ein neues, optionales Feature. Damit kann Kittyhack auf zwei Geräte aufgeteilt werden:

- **Kittyflap**: steuert weiterhin lokal die Hardware (z. B. Sensoren, RFID, Schlösser).
- **Remote Gerät** (separater Linux-PC/VM): übernimmt Web-UI und KI-Auswertung.

Das hilft vor allem dann, wenn die Kittyflap entlastet werden soll.

---

## Schemata

### Architektur (wer spricht mit wem)

![Remote-Mode Architektur](diagrams/remote-mode-architecture.svg)

### Control-Übernahme (was passiert beim Verbinden)

![Remote-Mode Control-Übernahme Ablauf](diagrams/remote-mode-control-takeover.svg)

### Verbindungsverlust (Recovery)

![Remote-Mode Verbindungsverlust Recovery](diagrams/remote-mode-connection-loss.svg)

---

## Was läuft wo?

### Auf der Kittyflap
- Läuft normal wie gewohnt.
- Wenn ein Remote Gerät die Steuerung übernimmt, zeigt die Kittyflap eine Info-Seite statt der normalen UI.
- Fällt die Verbindung aus, übernimmt die Kittyflap wieder selbstständig.

### Auf dem Remote Gerät
- Läuft die Kittyhack-Weboberfläche.
- Das Remote Gerät verbindet sich zur Kittyflap und steuert diese.

---

## Einrichtung

1. Kittyhack auf der **Kittyflap** normal installieren/aktualisieren.
2. Auf dem **Remote Gerät** Kittyhack installieren und beim Setup „Remote-Mode“ wählen.
   Setup script:
   ```bash
   sudo curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && sudo chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh de && sudo rm /tmp/kittyhack-setup.sh
   ```
3. Im Web-UI des Remote Geräts die IP-Adresse der Kittyflap eintragen.

Remote-UI im Browser:

- `http://<IP-des-Remote-Geräts>/`

---

## Wichtiger Hinweis zur Erst-Synchronisierung

Während der Einrichtung können Daten von der Kittyflap auf das Remote Gerät übernommen werden, z. B.:

- Modelle
- Bilder
- Datenbank
- Konfiguration
- Label Studio Daten

Diese Synchronisierung ist **nur bei der Einrichtung** möglich.  
Eine **nachträgliche** Synchronisierung ist aktuell nicht vorgesehen.

---

## Wo werden Daten gespeichert?

Wenn die Kittyflap von einem Remote Gerät gesteuert wird:

- neue Events werden **nur auf dem Remote Gerät** gespeichert
- Änderungen an Einstellungen werden **nur auf dem Remote Gerät** gespeichert

Wenn das Remote Gerät offline ist, übernimmt die Kittyflap automatisch wieder lokal — dann mit den **lokal auf der Kittyflap gespeicherten Einstellungen**.

Kurz gesagt: Remote Gerät und Kittyflap können sich bei längerer Trennung in den Daten unterscheiden.

---

## Was passiert bei Verbindungsverlust?

- Die Kittyflap gibt die Fernsteuerung frei.
- Sicherheitsfunktionen werden sauber beendet.
- Danach startet die Kittyflap wieder lokal und kann autonom weiterlaufen.
- Das Remote Gerät versucht später automatisch, die Verbindung wieder aufzubauen.

---

## Voraussetzungen (kurz)

### Remote Gerät
- Debian/Ubuntu (oder ähnlich)
- AMD64 (x86_64)
- mindestens 2 GB RAM

### Netzwerk
- Das Remote Gerät muss die Kittyflap im lokalen Netz erreichen können.
- Die Verbindung ist ausschließlich für ein **vertrauenswürdiges LAN** gedacht (nicht ins öffentliche Internet freigeben).

---

## Troubleshooting

- **Remote Gerät verbindet nicht zur Kittyflap**
  - IP-Adresse prüfen
  - beide Geräte im gleichen Netz / Routing prüfen
  - Firewall-Regeln prüfen

- **Kittyflap zeigt nur eine Info-Seite**
  - Dann ist gerade Remote-Steuerung aktiv oder war kurz zuvor aktiv.
  - Bei Verbindungsverlust übernimmt die Kittyflap nach Timeout wieder automatisch.