### Remote-Modus im Überblick

Im Remote-Modus läuft Kittyhack auf zwei Geräten:

- **Kittyflap**: Das Gerät steuert weiterhin seine Sensoren, den RFID-Leser und die Verriegelung der Klappe.
- **Remote-Gerät** (separater Linux-PC oder VM): Hier laufen die Kittyhack-Weboberfläche und die Erkennung.

Das entlastet die Kittyflap. Je nach Hardware kann die Erkennung von Katzen und Beute auf dem Remote-Gerät außerdem flüssiger laufen.
Das Remote-Gerät kann komplexere Modelle verwenden, die auf dem Raspberry Pi 4 in der Kittyflap nicht gut laufen.

---


#### Diagramme

##### Architektur: wer spricht mit wem?

![Architektur im Remote-Modus](diagrams/remote-mode-architecture.svg)

##### Steuerungsübernahme: Was passiert beim Verbinden?

![Steuerungsübernahme im Remote-Modus](diagrams/remote-mode-control-takeover.svg)

##### Wiederherstellung nach einem Verbindungsverlust

![Wiederherstellung nach Verbindungsverlust im Remote-Modus](diagrams/remote-mode-connection-loss.svg)

---

#### Was läuft wo?

##### Auf der Kittyflap
- Kittyhack läuft standardmäßig lokal.
- Wenn ein Remote-Gerät die Steuerung übernimmt, zeigt die Kittyflap eine Infoseite statt der üblichen Oberfläche.
- Wenn die Verbindung abbricht, kehrt die Kittyflap automatisch zum lokalen Betrieb zurück.

##### Auf dem Remote-Gerät
- Kittyhack stellt die Weboberfläche und die Erkennung bereit.
- Kittyhack verbindet sich mit der Kittyflap und steuert das Gerät aus der Ferne.

---

#### Einrichtung

1. Installiere oder aktualisiere Kittyhack wie gewohnt auf der **Kittyflap**.
2. Installiere Kittyhack auf dem **Remote-Gerät** und wähle bei der Einrichtung „Remote-Modus“.
   Einrichtungsskript:
   ```bash
   sudo curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && sudo chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh de && sudo rm /tmp/kittyhack-setup.sh
   ```
3. Trage in der Kittyhack-Weboberfläche auf dem Remote-Gerät die IP-Adresse der Kittyflap ein.

Öffne die Remote-Weboberfläche im Browser:

- `http://<IP-des-Remote-Geräts>/`

---

#### Erste Synchronisierung

Während der Einrichtung kannst du Daten von der Kittyflap auf das Remote-Gerät übertragen:

- Modelle
- Bilder
- Datenbank
- Einstellungen
- Label-Studio-Daten

Diese Synchronisierung ist **nur bei der Einrichtung** möglich. Du kannst sie später nicht erneut starten.

---

#### Wo werden Daten gespeichert?

Wenn ein Remote-Gerät die Kittyflap steuert:

- Neue Ereignisse werden **nur auf dem Remote-Gerät** gespeichert.
- Änderungen an den Einstellungen werden **nur auf dem Remote-Gerät** gespeichert.

Wenn das Remote-Gerät offline ist, kehrt die Kittyflap automatisch zum lokalen Betrieb zurück. Dabei verwendet sie die **auf der Kittyflap gespeicherten Einstellungen**.

Mit der Zeit unterscheiden sich deshalb die Ereignisse und Einstellungen auf dem Remote-Gerät und der Kittyflap.

Du kannst die Remote-Verbindung in der Kittyhack-Weboberfläche auf dem Remote-Gerät beenden. Danach kannst du die Einstellungen wieder über die Weboberfläche der Kittyflap ändern.

---

#### Was passiert bei Verbindungsverlust?

- Die Kittyflap gibt die Fernsteuerung frei.
- Laufende Sicherheitsaktionen werden sauber beendet.
- Die Kittyflap kehrt zum lokalen Betrieb zurück.
- Das Remote-Gerät versucht später automatisch, die Verbindung wiederherzustellen.

---

#### Software- und Hardwarevoraussetzungen

##### Remote-Gerät
- Debian oder Ubuntu, idealerweise auf einem eigenen System für Kittyhack
- AMD64- (x86_64) CPU mit mindestens 4 Kernen
- Mindestens 2 GB RAM

##### Netzwerk
- Das Remote-Gerät muss die Kittyflap im lokalen Netz erreichen können.
- Die Kittyflap muss über die Ports `80` und `8888` erreichbar sein.
- Die Verbindung ist ausschließlich für ein **vertrauenswürdiges LAN** gedacht (nicht ins öffentliche Internet freigeben).
