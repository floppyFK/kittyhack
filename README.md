# Kittyhack

### German version below / Deutsche Version weiter unten!

---

Kittyhack is an open-source project that enables offline use of the Kittyflap cat door—completely without internet access. It was created after the manufacturer of Kittyflap filed for bankruptcy, rendering the associated app non-functional.

⚠️ **Important Notes**  
I have no connection to the manufacturer of Kittyflap. This project was developed on my own initiative to continue using my Kittyflap.

Additionally, this project is in a **early stage**! The planned features are not fully implemented yet, and bugs are to be expected!

---

## Features

Until version `v1.1.0`, Kittyhack was merely a frontend for visualizing camera images and changing some settings.  
From version `v1.2.0`, Kittyhack replaces the complete original Kittyflap software with extended functionality.

Current features:  
- **Toggle prey detection**
- **Configure thresholds for mouse detection**
- **Switch entry direction** between "All cats", "All chipped cats", "my cats only" or "no cats"
- **Block exit direction**
- **Display captured images** (filterable by date, prey, and cat detection)
- **Show overlay with detected objects**
- **Live camera feed**
- **Manage cats and add new cats**

### Planned Features
- **WiFi configuration**: Currently needs to be set up manually. Will be implemented in the WebGUI in a future version
- **Display of events**: A journal showing when the flap was opened in which direction, when it was locked, etc.
- **Additional filters for image display**: e.g., grouping all images of an event (cat entering / cat exiting)

---

## Installation

### Prerequisites
- Access to the Kittyflap via SSH  
  You can usually find the Kittyflap's IP address in your router.  
  - The hostname begins with `kittyflap-`
  - The MAC address should start with `d8:3a:dd`.
  ![kittyflap router configuration](doc/kittyflap-hostname.png)

### Instructions
The setup is quite simple:

1. **Establish SSH Access**  
   Open a terminal and connect via SSH:
   ```bash
   ssh pi@<IP-address-of-Kittyflap>
   ```
   Default password: `kittyflap`  

2. **Check available disk space**
   If your cat flap was still active for an extended period after the Kittyflap servers were shut down, the file system might be full.
   In this case, you need to free up space before installing Kittyhack.

   Check available disk space:
   ```bash
   df -h
   ```
   For `/dev/mmcblk0p2`, there should be **at least** 1 GB of free space available.  
   
   If less space is available, perform the following steps:

   Login as root:
   ```bash
   sudo su
   ```
   
   Stop Kittyflap processes and delete the database (Warning: This will delete your cat's configuration. You can easily re-train it after the installation):
   ```bash
   systemctl stop kwork
   systemctl stop manager
   rm /root/kittyflap.db
   ```

   After this, `/dev/mmcblk0p2` should have significantly more free space available.

3. **Run the setup script on the Kittyflap**
    > **IMPORTANT:** Before starting the installation, please ensure that the WiFi connection of the cat flap is stable. During installation, several hundred MB of data will be downloaded!  
    > Since the antenna is mounted on the outside of the flap, the signal strength can be significantly weakened by e.g. a metal door.  
    You can check the strength of the WiFi signal with this command:
    ```bash
    iwconfig wlan0
    ```
    Run the installation:
   ```bash
   curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh && rm /tmp/kittyhack-setup.sh
   ```
   You can choose between two options:
   - **install**: Runs the full setup and disables unwanted services on the kittyflap (recommended)
   - **update**: Runs only the update (or the initial installation, if not yet done) of the KittyHack application. No system configuration will be changed.

   That's it!

### Language Settings
By default, the language is set to English. You can adjust the configuration in the web interface or pre-load the German configuration file:
```bash
sudo cp /root/kittyhack/config.ini.sample_DE /root/kittyhack/config.ini
```

### Access the Kittyhack Web Interface
Open the Kittyflap's IP address in your browser:
`http://<IP-address-of-Kittyflap>`

>#### Note
>⚠️ Since the connection is unencrypted, the browser will display a warning. This connection is generally safe within the local network, as long as you don't enable remote access to the Kittyflap via your router. For a secure connection, additional measures like setting up a reverse proxy can be taken.

>⚠️ To ensure Kittyhack is always reachable at the same IP address, it is recommended to assign a static IP address in your router.

### Updates
To check for updates just run the setup script again, as described above.  
You can also start it with the argument `update` to directly run the update:
```bash
curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh | sudo bash -s update
```

---


# DEUTSCH

Kittyhack ist ein Open-Source-Projekt, das die Offline-Nutzung der Kittyflap-Katzenklappe ermöglicht – ganz ohne Internetzugang. Es wurde ins Leben gerufen, nachdem der Anbieter der Kittyflap Insolvenz angemeldet hat und die zugehörige App nicht mehr funktionierte.

⚠️ **Wichtige Hinweise**  
Ich stehe in keinerlei Verbindung mit dem Hersteller der Kittyflap. Dieses Projekt wurde aus eigenem Antrieb erstellt, um meine eigene Katzenklappe weiterhin nutzen zu können.

Zudem befindet sich das Projekt noch in einem **frühen Stadium**! Die geplanten Funktionen sind noch nicht alle umgesetzt und mit Bugs ist zu rechnen!

---

## Funktionsumfang

Bis Version `v1.1.0` war Kittyhack lediglich ein Frontend zur Visualisierung der Kamerabilder und zum Ändern einiger Einstellungen.  
Ab Version `v1.2.0` ersetzt Kittyhack die komplette Originalsoftware der Kittyflap mit einem erweiterten Funktionsumfang.

Aktuelle Features:  
- **Beuteerkennung ein-/ausschalten**
- **Schwellwerte für Mauserkennung konfigurieren**
- **Eingangsrichtung umschalten** zwischen "Alle Katzen", "Alle gechippten Katzen", "nur meine Katzen" oder "keine Katzen"
- **Ausgangsrichtung blockieren**
- **Aufgenommene Bilder anzeigen** (filterbar nach Datum, Beute und Katzenerkennung)
- **Overlay mit erkannten Objekten anzeigen**
- **Live-Bild der Kamera**
- **Katzen verwalten und neue Katzen hinzufügen**

### Geplante Features
- **WLAN-Konfiguration**: Muss aktuell noch manuell eingerichtet werden. Wird in einer zukünftigen Version in der WebGUI implementiert
- **Anzeige von Events**: Ein Journal darüber, wann die Klappe in welche Richtung geöffnet wurde, wann sie gesperrt hat usw.
- **Weitere Filter für die Bildanzeige**: z.B. alle Bilder eines Events (Katze kommt rein / Katze geht raus) zusammenfassen

---

## Installation

### Voraussetzungen
- Zugriff auf die Kittyflap per SSH  
  Die IP-Adresse der Kittyflap kann üblicherweise im Router ausgelesen werden.  
  - Der Hostname beginnt mit `kittyflap-`
  - Die MAC-Adresse sollte mit `d8:3a:dd` beginnen.
  ![kittyflap router configuration](doc/kittyflap-hostname.png)

### Anleitung
Die Installation ist kinderleicht:

1. **SSH-Zugriff herstellen**  
   Öffne ein Terminal und verbinde dich per SSH:
   ```bash
   ssh pi@<IP-Adresse-der-Kittyflap>
   ```
   Standardpasswort: `kittyflap`  

2. **Freien Speicherplatz überprüfen**
   Falls deine Katzenklappe nach der Abschaltung der Kittyflap-Server noch längere Zeit aktiv war, kann es sein, dass das Dateisystem vollgeschrieben ist.
   In diesem Fall musst du vor der Installation von Kittyhack erst Platz schaffen.

   Vorhandenen Speicherplatz überprüfen:
   ```bash
   df -h
   ```
   Für `/dev/mmcblk0p2` sollte **mindestens** 1 GB freier Speicherplatz zur Verfügung stehen.  
   
   Falls weniger Speicherplatz verfügbar ist, führe folgende Schritte aus:

   Als root einloggen:
   ```bash
   sudo su
   ```
   
   Kittyflap-Prozesse stoppen und die Datenbank löschen (Achtung: Dabei geht die Konfiguration deiner Katze verloren. Du kannst sie nach der Installation aber einfach wieder neu anlernen):
   ```bash
   systemctl stop kwork
   systemctl stop manager
   rm /root/kittyflap.db
   ```

   Danach sollte für `/dev/mmcblk0p2` deutlich mehr freier Speicherplatz verfügbar sein.


3. **Das Setup Script auf der Kittyflap ausführen**
   > **WICHTIG:** Bitte stelle vor dem Start der Installation sicher, dass die WLAN-Verbindung der Katzenklappe stabil ist. Während der Installation werden mehrere hundert MB an Daten heruntergeladen!  
   > Da die Antenne auf der Außenseite der Klappe angebracht ist, kann die Signalstärke durch z.B. eine Metalltür stark abgeschwächt werden.  
   Mit diesem Befehl kannst du die Stärke des WLAN-Signals überprüfen:
   ```bash
   iwconfig wlan0
   ```
   Installation ausführen:
   ```bash
   curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh && rm /tmp/kittyhack-setup.sh
   ```
   Du hast die Auswahl zwischen zwei Optionen:
   - **install**: Führt das komplette Setup aus, inklusive stoppen und entfernen von ungewollten Services auf der Kittyflap (empfohlen)
   - **update**: Führt nur das Update (oder die initiale Installation, falls noch nicht geschehen) der KittyHack Applikation aus. An der bestehenden Systemkonfiguration wird nichts geändert.

   Das war's!

### Spracheinstellungen
Standardmäßig ist die Sprache auf Englisch eingestellt. Du kannst die Konfiguration entweder im Webinterface anpassen oder die deutsche Konfigurationsdatei vorab laden:
```bash
sudo cp /root/kittyhack/config.ini.sample_DE /root/kittyhack/config.ini
```

### Zugriff auf das Kittyhack Webinterface
Rufe die IP-Adresse der Kittyflap in deinem Browser auf:
`http://<IP-Adresse-der-Kittyflap>`

>#### Hinweis
>⚠️ Da die Verbindung nicht verschlüsselt ist, wird der Browser eine Warnung anzeigen. Diese Verbindung ist innerhalb des lokalen Netzwerks in der Regel sicher, solange du keinen Fernzugriff auf die Kittyflap über deinen Router freigibst. Für eine sichere Verbindung können zusätzliche Maßnahmen wie ein Reverse-Proxy eingerichtet werden.

>⚠️ Damit Kittyhack immer unter der selben IP Adresse erreichbar ist, empfiehlt es sich, im Router eine statische IP Adresse zu vergeben.

### Updates
Um nach Updates zu suchen, führe einfach das Setup Script wie oben beschrieben nochmal aus.  
Du kannst das Script auch mit dem Argument `update` starten, um direkt das Update auszuführen:
```bash
curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh | sudo bash -s update
```