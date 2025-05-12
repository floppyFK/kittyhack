# Kittyhack

### [German version below / Deutsche Version weiter unten!](#deutsch)

---

Kittyhack is an open-source project that enables offline use of the Kittyflap cat door—completely without internet access. It was created after the manufacturer of Kittyflap filed for bankruptcy, rendering the associated app non-functional.

⚠️ **Important Notes**  
I have no connection to the manufacturer of Kittyflap. This project was developed on my own initiative to continue using my Kittyflap.

If you find any bugs or have suggestions for improvement, please report them on the GitHub issue tracker.

---

## Features

Until version `v1.1.x`, Kittyhack was merely a frontend for visualizing camera images and changing some settings.  
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
- **Show incoming/outgoing Events** 
- **AI Training** Create a custom object detection model for your cat and your environment by using your own images

---

## Installation

### Prerequisites
- Access to the Kittyflap via SSH  
  You can usually find the Kittyflap's IP address in your router.  
  - The hostname begins with `kittyflap-`
  - The MAC address should start with `d8:3a:dd`.
  ![kittyflap router configuration](doc/kittyflap-hostname.png)

### If your Kittyflap hasn't been set up yet
If you never configured your Kittyflap with the official app, it is preset to a default WiFi network.
To establish a connection, you need to temporarily adjust your router's WiFi settings.

Use one of these combinations:
- SSID: `Graewer factory`, Password: `Graewer2023`
- SSID: `Graewer Factory`, Password: `Graewer2023`
- SSID: `GEG-Gast`, Password: `GEG-1234`

After changing the router SSID:
1. Restart the Kittyflap
2. Wait until it appears as a client in your router
3. Connect [via SSH](#ssh_access_en) (User: `pi`, Password: `kittyflap`)

Now proceed with the installation. You can add your own WLAN configuration later on in the Kittyhack Web interface.

### Instructions
The setup is quite simple:
<a id="ssh_access_en"></a>

1. **Establish SSH Access**  
   Open a terminal (on Windows, for example, with the key combination `[WIN]`+`[R]`, then enter `cmd` and execute) and connect to your Kittyflap via SSH with the following command:
   ```bash
   ssh pi@<IP-address-of-Kittyflap>
   ```
   Username: `pi`
   Default password: `kittyflap`  
   > **NOTE:** You have to enter the password "blindly", as no characters will be displayed while typing.

2. **Check available disk space**
   If your cat flap was still active for an extended period after the Kittyflap servers were shut down, the file system might be full.
   In this case, you need to free up space before installing Kittyhack.

   Check available disk space:
   ```bash
   df -h
   ```
   For `/dev/mmcblk0p2`, there should be **at least** 1 GB of free space available:  
   ![free disk space](doc/free-disk-space-example.jpg)
   
   #### If less storage space is available, follow these steps - otherwise, proceed with the [Setup Script](#setup_en):
   
      1. Stop Kittyflap processes:
         ```bash
         sudo systemctl stop kwork
         sudo systemctl stop manager
         ```

      2. Release magnetic switches:
         **ATTENTION:** If one of the magnetic switches is still active at this point (i.e., the flap is unlocked), they will not be automatically deactivated until the end of the installation.  
            Please make sure to deactivate them now with these commands to avoid overloading the electromagnets:
         ```bash
         # Export GPIOs
         echo 525 > /sys/class/gpio/export 2>/dev/null
         echo 524 > /sys/class/gpio/export 2>/dev/null
         
         # Configure GPIO directions
         echo out > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio525/direction
         echo out > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio524/direction
         
         # Set default output values for GPIOs
         echo 0 > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio525/value
         sleep 1
         echo 0 > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio524/value
         ```

      3. Reduce the size of the swap file (by default, 6GB are reserved for this):
         ```bash
         # Turn off and remove the current swapfile
         sudo swapoff /swapfile
         sudo rm /swapfile

         # Create a new 2GB swapfile
         sudo fallocate -l 2G /swapfile
         sudo chmod 600 /swapfile
         sudo mkswap /swapfile
         sudo swapon /swapfile
         ```

         As confirmation, you will receive the size of the new swap file. The result should look something like this:
         ![free disk space](doc/swap-resize.jpg)

      After this, `/dev/mmcblk0p2` should have significantly more free space available.

<a id="setup_en"></a>

3. **Run the setup script on the Kittyflap**
    > **IMPORTANT:** Before starting the installation, please ensure that the WiFi connection of the cat flap is stable. During installation, several hundred MB of data will be downloaded!  
    > Since the antenna is mounted on the outside of the flap, the signal strength can be significantly weakened by e.g. a metal door.  

    You can check the strength of the WiFi signal with this command:
    ```bash
    iwconfig wlan0
    ```
    Run the installation:
   ```bash
   sudo curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && sudo chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh && sudo rm /tmp/kittyhack-setup.sh
   ```
   You can choose between the following options:
   - **Initial installation**: Performs the complete setup, including stopping and removing unwanted services on the Kittyflap (only to be executed once)
   - **Reinstall camera drivers**: This will reinstall the necessary device drivers in the system. Only to be executed if there are problems with the camera image. This installation is also possible directly through the web interface.
   - **Update to the latest version**: If a first-time installation of Kittyhack has already been performed, this option is sufficient to update to the latest version. The existing system configuration will not be changed.

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
>⚠️ Since the connection is unencrypted, the browser will display a warning. This connection is generally safe within the local network, as long as you don't enable remote access 
to the Kittyflap via your router. For a secure connection, additional measures like setting up a reverse proxy can be taken.

>⚠️ To ensure Kittyhack is always reachable at the same IP address, it is recommended to assign a static IP address in your router.

### Updates
Updates for Kittyhack are available directly in the WebGUI in the 'Info' section starting from version v1.2.0.  
If you are using Kittyhack version v1.1.x, simply run the [setup script](#setup_en) on the Kittyflap again to perform an update.

### Switching between v1.1.x and newer versions
The [setup script](#setup_en) allows you to switch between versions. Simply run it again and choose the appropriate option.


## FAQ

### My Kittyflap disappears from my WLAN after a few hours
The WLAN signal is probably too weak because the WLAN antenna is mounted on the outside of the Kittyflap and has to pass therefore an additional wall or door to reach your router.  
Make sure the distance to the router is not too great. If the WLAN signal is too weak, the Kittyflap will eventually disconnect and will only reconnect after being restarted by 
unplugging and plugging it back in (I am still investigating why this happens - I am trying to find a solution!).  
In Kittyhack version 1.2.0 and later, you can check the strength of the WLAN signal in the 'Info' section.

### Why is the website background grayed out and the content disappears when I try to switch sections?
This issue is related to power-saving features on smartphones and tablets: When your browser on your smartphone loses focus (e.g., when you switch to the home screen), communication 
with the Kittyhack page stops after a few seconds. I am still working on a solution for this problem.  
In the meantime, you can simply reload the Kittyhack page (e.g., with the refresh gesture) to make it work normally again.

### After updating Kittyhack from v1.1 to v1.2, the detection area for 'Mouse' or 'No Mouse' is not marked in the images in the image view, even though I have activated the *Show detection overlay* function
For old images imported from version v1.1, these areas were not saved. This function is only available from version v1.2. For all newly captured images, this area will be saved correctly.

### I have successfully installed Kittyhack v1.2. Shouldn't the night light be activated when it gets too dark?
Please restart the Kittyflap once after installation (in the 'System' section -> 'Restart Kittyflap'), then it should work.

---


# DEUTSCH

Kittyhack ist ein Open-Source-Projekt, das die Offline-Nutzung der Kittyflap-Katzenklappe ermöglicht – ganz ohne Internetzugang. Es wurde ins Leben gerufen, nachdem der Anbieter der Kittyflap Insolvenz angemeldet hat und die zugehörige App nicht mehr funktionierte.

⚠️ **Wichtige Hinweise**  
Ich stehe in keinerlei Verbindung mit dem Hersteller der Kittyflap. Dieses Projekt wurde aus eigenem Antrieb erstellt, um meine eigene Katzenklappe weiterhin nutzen zu können.

Wenn du Bugs findest oder Verbesserungsvorschläge hast, melde sie bitte im Issue Tracker dieses GitHub Projekts.

---

## Funktionsumfang

Bis Version `v1.1.x` war Kittyhack lediglich ein Frontend zur Visualisierung der Kamerabilder und zum Ändern einiger Einstellungen.  
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
- **Ereignisse von ankommenden/rausgehenden Katzen anzeigen**
- **"KI" Modell Training** Erstelle ein individuelles Objekterkennungsmodell für deine Katze und deine Umgebung anhand der eigenen Bilder

---

## Installation

### Voraussetzungen
- Zugriff auf die Kittyflap per SSH  
  Die IP-Adresse der Kittyflap kann üblicherweise im Router ausgelesen werden.  
  - Der Hostname beginnt mit `kittyflap-`
  - Die MAC-Adresse sollte mit `d8:3a:dd` beginnen.
  ![kittyflap router configuration](doc/kittyflap-hostname.png)

### Wenn deine Kittyflap noch nicht eingerichtet wurde
Falls du deine Kittyflap nie mit der offiziellen App konfiguriert hast, ist sie auf ein Standard-WLAN voreingestellt.
Um eine Verbindung herzustellen, musst du vorübergehend die WLAN-Einstellungen deines Routers anpassen.

Verwende eine dieser Kombinationen:
- SSID: `Graewer factory`, Passwort: `Graewer2023`
- SSID: `Graewer Factory`, Passwort: `Graewer2023`
- SSID: `GEG-Gast`, Passwort: `GEG-1234`

Nach Änderung der Router-SSID:
1. Starte die Kittyflap neu
2. Warte bis sie im Router als Client erscheint
3. Verbinde dich [per SSH](#ssh_access_de) (Benutzer: `pi`, Passwort: `kittyflap`)

Fahre jetzt mit der Installation fort. Du kannst dein eigenes WLAN später im Web Interface von Kittyhack konfigurieren.

### Anleitung
Die Installation ist kinderleicht:
<a id="ssh_access_de"></a>

1. **SSH-Zugriff herstellen**  
   Öffne ein Terminal (unter Windows z.B. mit der Tastenkombination `[WIN]`+`[R]`, dann `cmd` eingeben und ausführen) und verbinde dich mit dem folgenden Kommando per SSH zu deiner Kittyflap:
   ```bash
   ssh pi@<IP-Adresse-der-Kittyflap>
   ```
   Benutzername: `pi`
   Standardpasswort: `kittyflap`  
   > **HINWEIS:** Du musst das Passwort "blind" eingeben, da beim Tippen keine Zeichen angezeigt werden.

2. **Freien Speicherplatz überprüfen**
   Falls deine Katzenklappe nach der Abschaltung der Kittyflap-Server noch längere Zeit aktiv war, kann es sein, dass das Dateisystem vollgeschrieben ist.
   In diesem Fall musst du vor der Installation von Kittyhack erst Platz schaffen.

   Vorhandenen Speicherplatz überprüfen:
   ```bash
   df -h
   ```
   Für `/dev/mmcblk0p2` sollte **mindestens** 1 GB freier Speicherplatz zur Verfügung stehen:  
   ![free disk space](doc/free-disk-space-example.jpg)
   
   #### Falls weniger Speicherplatz verfügbar ist, führe folgende Schritte aus - ansonsten fahre fort mit dem [Setup Script](#setup_de):
   
      1. Kittyflap-Prozesse stoppen:
         ```bash
         sudo systemctl stop kwork
         sudo systemctl stop manager
         ```

      2. Magnetschalter deaktivieren:
         **ACHTUNG:** Falls zu diesem Zeitpunkt noch einer der Magnetschalter aktiv ist (also die Klappe entriegelt ist), werden diese bis zum Ende der Installation nicht mehr automatisch deaktiviert.  
         Bitte deaktiviere sie unbedingt jetzt mit diesen Kommandos, um die Elektromagneten nicht zu überlasten:
         ```bash
         # Export GPIOs
         echo 525 > /sys/class/gpio/export 2>/dev/null
         echo 524 > /sys/class/gpio/export 2>/dev/null
         
         # Configure GPIO directions
         echo out > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio525/direction
         echo out > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio524/direction
         
         # Set default output values for GPIOs
         echo 0 > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio525/value
         sleep 1
         echo 0 > /sys/devices/platform/soc/fe200000.gpio/gpiochip0/gpio/gpio524/value
         ```


      3. Größe der Swap-Datei reduzieren (standardmäßig sind hierfür 6GB reserviert):
         ```bash
         # Turn off and remove the current swapfile
         sudo swapoff /swapfile
         sudo rm /swapfile

         # Create a new 2GB swapfile
         sudo fallocate -l 2G /swapfile
         sudo chmod 600 /swapfile
         sudo mkswap /swapfile
         sudo swapon /swapfile
         ```

         Als Bestätigung bekommst du die Größe des neuen Swap-Datei zurückgemeldet. Das Ergebnis sollte etwa so aussehen:
         ![free disk space](doc/swap-resize.jpg)  

      Danach sollte für `/dev/mmcblk0p2` deutlich mehr freier Speicherplatz verfügbar sein.

<a id="setup_de"></a>

3. **Das Setup Script auf der Kittyflap ausführen**
   > **WICHTIG:** Bitte stelle vor dem Start der Installation sicher, dass die WLAN-Verbindung der Katzenklappe stabil ist. Während der Installation werden mehrere hundert MB an Daten heruntergeladen!  
   > Da die Antenne auf der Außenseite der Klappe angebracht ist, kann die Signalstärke durch z.B. eine Metalltür stark abgeschwächt werden.  
   
   Mit diesem Befehl kannst du die Stärke des WLAN-Signals überprüfen:
   ```bash
   iwconfig wlan0
   ```
   Installation ausführen:
   ```bash
   sudo curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && sudo chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh de && sudo rm /tmp/kittyhack-setup.sh
   ```
   Du hast die Auswahl zwischen folgenden Optionen:
   - **Erstmalige Installation**: Führt das komplette Setup aus, inklusive stoppen und entfernen von ungewollten Services auf der Kittyflap (nur erstmalig auszuführen)
   - **Kameratreiber erneut installieren**: Damit werden die erforderlichen Gerätetreiber im System erneut installiert. Nur auszuführen, wenn es Probleme mit dem Kamerabild geben sollte. Diese Installation ist auch direkt über das Web-Interface möglich.
   - **Update auf die neueste Version**: Wenn bereits eine erstmalige Installation von Kittyhack ausgeführt wurde, reicht diese Option, um auf den aktuellsten Stand zu aktualisieren. An der bestehenden Systemkonfiguration wird nichts geändert.

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
>⚠️ Da die Verbindung nicht verschlüsselt ist, wird der Browser eine Warnung anzeigen. Diese Verbindung ist innerhalb des lokalen Netzwerks in der Regel sicher, solange du keinen Fernzugriff 
auf die Kittyflap über deinen Router freigibst. Für eine sichere Verbindung können zusätzliche Maßnahmen wie ein Reverse-Proxy eingerichtet werden.

>⚠️ Damit Kittyhack immer unter der selben IP Adresse erreichbar ist, empfiehlt es sich, im Router eine statische IP Adresse zu vergeben.

### Updates
Updates von Kittyhack sind ab der Version v1.2.0 direkt in der WebGUI in der Sektion 'Info' möglich.
Wenn du die Version v1.1.x verwendest, führe für ein Update einfach das [Setup Script](#setup_de) auf der Kittyflap nochmal aus.

### Wechsel zwischen v1.1.x und neueren Version
Das [Setup Script](#setup_de) bietet die Möglichkeit zwischen den Versionen zu wechseln. Führe es dazu einfach erneut aus und wähle die entsprechende Option. 


## FAQ

### Meine Kittyflap verschwindet nach einigen Stunden immer wieder aus meinem WLAN
Wahrscheinlich ist das WLAN Signal zu schwach, da die WLAN-Antenne auf der Außenseite der Kittyflap angebracht ist und bis zu deinem Router somit eine zusätzliche Wand bzw. Türe durchdringen muss.  
Achte darauf, dass die Entfernung zum Router nicht zu groß ist. Wenn das WLAN-Signal zu schwach ist, meldet sich die Kittyflap irgendwann ab und wählt sich erst wieder ein,
wenn sie durch Aus- und Wiedereinstecken neu gestartet wurde (warum das so ist untersuche ich noch - ich versuche, eine Lösung dafür zu finden!)  
In Kittyhack Version ab v1.2.0 kannst du die Stärke des WLAN-Signals übrigens in der 'Info'-Sektion auslesen.  

### Warum ist der Hintergrund der Website ausgegraut und der Inhalt verschwindet, wenn ich versuche, die Sektion wechsle?
Dieses Problem hat mit den Energiesparfunktionen auf Smartphones und Tablets zu tun: Wenn dein Browser auf deinem Smartphone den Fokus verliert (also wenn du z. B. auf den Homescreen wechselst), 
wird nach wenigen Sekunden die Kommunikation mit der Kittyhack Seite gestoppt. Ich versuche noch für dieses Problem eine Lösung zu finden.  
In der Zwischenzeit kannst du die Kittyhack Seite aber einfach neu laden (z. B. mit der Aktualisieren-Geste), damit sie wieder normal funktioniert.

### Nach dem Update von Kittyhack von v1.1 auf v1.2 ist in den Bildern in der Bilderansicht kein Erkennungsbereich für 'Maus' bzw. 'Keine Maus' eingezeichnet, obwohl ich die Funktion *Overlay in Bildern anzeigen* aktiviert habe
Bei alten Bildern, die aus der Version v1.1 importiert wurden, wurden diese Bereiche noch nicht abgespeichert. Diese Funktion ist erst ab Version v1.2 verfügbar. Bei allen neu aufgenommenen Bildern wird dieser Bereich korrekt gespeichert.

### Ich habe Kittyhack v1.2 erfolgreich installiert. Sollte damit nicht das Nachtlicht aktiviert werden, wenn es zu dunkel ist?
Bitte starte die Kittyflap nach der Installation einmal neu (in der Sektion 'System' -> 'Kittyflap Neustarten'), dann sollte es funktionieren.
