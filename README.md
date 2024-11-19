# Kittyhack

### German version below / Deutsche Version weiter unten!

---

Kittyhack is an open-source project that enables offline use of the Kittyflap cat door—completely without internet access. It was created after the manufacturer of Kittyflap filed for bankruptcy, rendering the associated app non-functional.

⚠️ **Important Notes**  
I have no connection to the manufacturer of Kittyflap. This project was developed on my own initiative to continue using my Kittyflap.

Additionally, this project is in a **very early stage**! The planned features are not fully implemented yet, and bugs are to be expected!

---

## Features

The Kittyflap has minimal password protection, which made it possible to gain access to the system. Kittyhack currently offers the following features, familiar from the original app:

- **Enable/Disable prey detection**
- **Enable/Disable "Accept all cats" mode**
- **View captured images** (filterable by date, prey, and cat detection)
- **Adjust thresholds for prey and cat detection** (details about how these thresholds work are not yet fully clear)

### Planned Features
The following features will be implemented soon:
- **Image Database**: Currently, images are only stored for about one day. In the next version, it will be possible to set the maximum number of stored images yourself — regardless of the age of the images.
- **Wi-Fi Configuration**: Currently not supported.
- **Teach new cats**: This feature is also planned.
- **Automatic Updates**: At the moment, there is no option for automatic updates of Kittyhack via the web interface.
- **Event Display**: A journal showing when the flap opened in which direction, when it locked, and so on.
- ...

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

2. **Run the setup script on the Kittyflap**
   ```bash
   curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh | sh
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
curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh | bash -s update
```

---


# DEUTSCH

Kittyhack ist ein Open-Source-Projekt, das die Offline-Nutzung der Kittyflap-Katzenklappe ermöglicht – ganz ohne Internetzugang. Es wurde ins Leben gerufen, nachdem der Anbieter der Kittyflap Insolvenz angemeldet hat und die zugehörige App nicht mehr funktionierte.

⚠️ **Wichtige Hinweise**  
Ich stehe in keinerlei Verbindung mit dem Hersteller der Kittyflap. Dieses Projekt wurde aus eigenem Antrieb erstellt, um meine eigene Katzenklappe weiterhin nutzen zu können.

Zudem befindet sich das Projekt noch in einem **sehr frühen Stadium**! Die geplanten Funktionen sind noch nicht alle umgesetzt und mit Bugs ist zu rechnen!

---

## Funktionsumfang

Die Kittyflap ist mit einem minimalen Passwortschutz versehen, sodass es möglich war, Zugang zum System zu erhalten. Kittyhack bietet derzeit die folgenden Funktionen, die auch von der originalen App bekannt sind:

- **Beuteerkennung ein-/ausschalten**
- **"Alle Katzen akzeptieren" ein-/ausschalten**
- **Aufgenommene Bilder anzeigen** (filterbar nach Datum, Beute und Katzenerkennung)
- **Schwellwerte für Beute- und Katzenerkennung anpassen** (die Details zur Funktionsweise der Schwellwerte sind noch nicht vollständig klar)

### Geplante Features
Folgende Features werden demnächst implementiert:
- **Bilddatenbank**: Aktuell werden die Bilder nur für ca. einen Tag vorgehalten. Mit der nächsten Version wird es möglich sein, die maximale Anzahl der gespeicherten Bilder selbst festzulegen - unabhängig vom Alter der Bilder
- **WLAN-Konfiguration**: Derzeit noch nicht unterstützt.
- **Neue Katzen anlernen**: Diese Funktion ist ebenfalls in Planung.
- **Automatische Updates**: Momentan gibt es noch keine Möglichkeit für automatische Updates von Kittyhack über das Webinterface.
- **Anzeige von Events**: Ein Journal darüber, wann die Klappe in welche Richtung geöffnet wurde, wann sie gesperrt hat usw.
- ...

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

2. **Das Setup Script auf der Kittyflap ausführen**
   ```bash
   curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh | sh
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
curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh | bash -s update
```