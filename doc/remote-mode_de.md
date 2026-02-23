### Remote-Mode (Remote-Control) - Überblick

Der Remote-Mode ist ein neues, optionales Feature. Damit kann Kittyhack auf zwei Geräte aufgeteilt werden:

- **Kittyflap**: steuert weiterhin lokal die Hardware (z. B. Sensoren, RFID, Verriegelungen).
- **Remote Gerät** (separater Linux-PC/VM): übernimmt Web-UI und KI-Auswertung.

Dadurch wird die Kittyflap entlastet und es können - abhängig von der eingesetzten Hardware - deutlich höhere Bildraten bei der Auswertung von Katze und Beute erreicht werden.
Zudem lassen sich komplexere Modelle einsetzen, die auf dem vergleichsweise leistungsschwachen Raspberry Pi 4 der Kittyflap nicht lauffähig sind.

---


#### Schemata

##### Architektur: wer spricht mit wem?

![Remote-Mode Architektur](diagrams/remote-mode-architecture.svg)

##### Control-Übernahme: was passiert beim Verbinden

![Remote-Mode Control-Übernahme Ablauf](diagrams/remote-mode-control-takeover.svg)

##### Verbindungsverlust (Recovery)

![Remote-Mode Verbindungsverlust Recovery](diagrams/remote-mode-connection-loss.svg)

---

#### Was läuft wo?

##### Auf der Kittyflap
- Läuft Kittyhack normal wie gewohnt.
- Wenn ein Remote Gerät die Steuerung übernimmt, zeigt die Kittyflap eine Info-Seite statt der normalen UI.
- Fällt die Verbindung aus, übernimmt die Kittyflap wieder selbstständig als Fallback.

##### Auf dem Remote Gerät
- Läuft die Kittyhack-Weboberfläche.
- Das Remote Gerät verbindet sich zur Kittyflap und steuert diese.

---

#### Einrichtung

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

#### Wichtiger Hinweis zur Erst-Synchronisierung

Während der Einrichtung können Daten von der Kittyflap auf das Remote Gerät übernommen werden, z. B.:

- Modelle
- Bilder
- Datenbank
- Konfiguration
- Label Studio Daten

Diese Synchronisierung ist **nur bei der Einrichtung** möglich.  
Eine **nachträgliche** Synchronisierung ist aktuell nicht vorgesehen.

---

#### Wo werden Daten gespeichert?

Wenn die Kittyflap von einem Remote Gerät gesteuert wird:

- neue Events werden **nur auf dem Remote Gerät** gespeichert
- Änderungen an Einstellungen werden **nur auf dem Remote Gerät** gespeichert

Wenn das Remote Gerät offline ist, übernimmt die Kittyflap als **fallback** automatisch wieder lokal - dann mit den **lokal auf der Kittyflap gespeicherten Einstellungen**.

Kurz gesagt: Remote-Gerät und Kittyflap werden sich nach einiger Zeit hinsichtlich ihrer Daten (Events und Konfiguration) unterscheiden!

Die Remote-Verbindung kann im Web-UI des Remote-Geräts beendet werden. Anschließend ist die Konfiguration wieder über das Web-UI der Kittyflap möglich.

---

#### Was passiert bei Verbindungsverlust?

- Die Kittyflap gibt die Fernsteuerung frei.
- Sicherheitsfunktionen werden sauber beendet.
- Danach startet die Kittyflap wieder lokal und kann autonom weiterlaufen.
- Das Remote Gerät versucht später automatisch, die Verbindung wieder aufzubauen.

---

#### Software- und Hardware-Voraussetzungen

##### Remote Gerät
- Debian oder Ubuntu (idealerweise ein System, das ausschließlich für den Betrieb von Kittyhack vorgesehen ist)
- AMD64- (x86_64) CPU mit mindestens 4 Kernen
- mindestens 2 GB RAM

##### Netzwerk
- Das Remote Gerät muss die Kittyflap im lokalen Netz erreichen können.
- Die Kittyflap muss über die Ports `80` und `8888` erreichbar sein.
- Die Verbindung ist ausschließlich für ein **vertrauenswürdiges LAN** gedacht (nicht ins öffentliche Internet freigeben).
