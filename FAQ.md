# FAQ

### [German version below / Deutsche Version weiter unten!](#deutsch)

# ENGLISH

We try to answer some frequently asked questions and share some neat tips and tricks in this section.
⚠️ Please bear in mind, that some of these actions might void your warranty, damage your flap or interfere with several functions - any action is on your own risk and responsibility!

---
1.1) My Kittyflap disappears from my WLAN after a few hours
1.2) WIP
1.3) Why is the website background grayed out and the content disappears when I try to switch sections?

2.1) WIP
2.2) WIP
2.3) WIP
---

### My Kittyflap disappears from my WLAN after a few hours
The WLAN signal is probably too weak because the WLAN antenna is mounted on the outside of the Kittyflap and has to pass therefore an additional wall or door to reach your router.  
Make sure the distance to the router is not too great. If the WLAN signal is too weak, the Kittyflap will eventually disconnect and will only reconnect after being restarted by 
unplugging and plugging it back in (I am still investigating why this happens - I am trying to find a solution!).  
In Kittyhack version 1.2.0 and later, you can check the strength of the WLAN signal in the 'Info' section.

### Why is the website background grayed out and the content disappears when I try to switch sections?
This issue is related to power-saving features on smartphones and tablets: When your browser on your smartphone loses focus (e.g., when you switch to the home screen), communication 
with the Kittyhack page stops after a few seconds. I am still working on a solution for this problem.  
In the meantime, you can simply reload the Kittyhack page (e.g., with the refresh gesture) to make it work normally again.

---


# DEUTSCH

In diesem Abschnitt versuchen wir auf häufig gestellte Fragen einzugehen und teilen ein paar nützliche Tipps und Tricks, wie ihr eure KittyFlap ein wenig verbessern könnt.
⚠️ Bitte habt dabei stets im Kopf, dass die hier beschriebenen Schritte eure Garantie beeinträchtigen, die KittyFlap beschädigen oder andere Funktionen beeinträchtigen könnten - ihr führt die Schritte auf eigenes Risiko durch und seid ausschließlich selbst verantwortlich!

---
1.1) Meine Kittyflap verschwindet nach einigen Stunden immer wieder aus meinem WLAN
1.2) Meine KittyFlap hat schlechten WLAN-Empfang und verliert immer mal wieder die Verbindung
1.3) Warum ist der Hintergrund der Website ausgegraut und der Inhalt verschwindet, wenn ich versuche, die Sektion zu wechseln

2.1) Ich möchte meinen Empfang durch eine externe Antenne verbessern
2.2) Ich möchte meine Infrarot-Sensoren verbessern
2.3) Meine Magnete lösen wie von geisterhand aus, obwohl keine Bewegung im Klappenbereich war
---

### 1.1) Meine Kittyflap verschwindet nach einigen Stunden immer wieder aus meinem WLAN
Zunächst solltest du sichergehen, dass du im Router der KittyFlap eine feste IP-Adresse zugeteilt hast. Das reduziert schon mal mögliche Fehlerquellen. Da dies bei jedem Router anders funktioniert, musst du selbst Googeln, wie es bei deinem geht. ("[Router Modell] feste IP vergeben")
Falls das nicht hilft, kannst du ggf. mit Schritt 2 der FAQ fortfahren.

### 1.2) Meine KittyFlap hat schlechten WLAN-Empfang und verliert immer mal wieder die Verbindung
Eventuell ist das WLAN Signal zu schwach, da die WLAN-Antenne auf der Außenseite der Kittyflap angebracht ist und bis zu deinem Router somit eine zusätzliche Wand bzw. Türe durchdringen muss. Die integrierte Antenne ist zudem nicht gerade empfangsstark.  
Achte darauf, dass die Entfernung zum Router nicht zu groß ist. Wenn das WLAN-Signal zu schwach ist, meldet sich die Kittyflap irgendwann ab und wählt sich erst wieder ein,
wenn sie durch Aus- und Wiedereinstecken neu gestartet wurde (warum das so ist untersuche ich noch - ich versuche, eine Lösung dafür zu finden!)  
In Kittyhack Version ab v1.2.0 kannst du die Stärke des WLAN-Signals übrigens in der 'Info'-Sektion auslesen. 

### 1.3) Warum ist der Hintergrund der Website ausgegraut und der Inhalt verschwindet, wenn ich versuche, die Sektion zu wechseln
Dieses Problem hat mit den Energiesparfunktionen auf Smartphones und Tablets zu tun: Wenn dein Browser auf deinem Smartphone den Fokus verliert (also wenn du z. B. auf den Homescreen wechselst), 
wird nach wenigen Sekunden die Kommunikation mit der Kittyhack Seite gestoppt. Ich versuche noch für dieses Problem eine Lösung zu finden.  
In der Zwischenzeit kannst du die Kittyhack Seite aber einfach neu laden (z. B. mit der Aktualisieren-Geste), damit sie wieder normal funktioniert.


### 2.1) Ich möchte meinen Empfang durch eine externe Antenne verbessern
Wir arbeiten derzeit an einer detaillierten Anleitung zur Nutzung und zum Einbau einer externen WLAN-Antenne. Bitte hab noch ein wenig Geduld...
Kaufen kann man eine passende Antenne z.B. bei Amazon ( https://www.amazon.de/Waveshare-Compatible-Raspberry-Supports-Frequency/dp/B08RRX9H2Q/ ) oder BerryBase ( https://www.berrybase.de/antennenkit-fuer-raspberry-pi-compute-module-4-5 ), wobei es nur auf den passenden Stecker ankommt - es gibt also sicher noch viele andere Bezugsquellen.
Bei einem ersten Test konnte eine deutliche Verbesserung der Signalqualität erreicht werden. Die Ergebnisse können individuell abweichen, aber es zeigt, dass eine Verbesserung mit einfachen Mitteln erreicht werden kann.
Vorher:
Link Quality: 🟡 53/70
Signal Level: -57 dBm

Nachher:
Link Quality: 🟢 60/70
Signal Level: -50 dBm

### 2.2) Ich möchte meine Infrarot-Sensoren verbessern
Stelle bei Problemen zunächst sicher, dass die Lötstellen alle in Ordnung sind. Hier wurden bereits des öfteren lose Kontakte oder sog. kalte Lötstellen festgestellt.
Ein weiterer Punkt, der überprüft werden sollte, ist die Einbaurichtung insbesondere des inneren PIR-Sensors. Aufgrund der Funktionsweise des PIR-Sensors kann es bei leichter Drehung des Sensors zu Problemen kommen. Ein Bild wie er korrekt eingebaut sein sollte, folgt in Kürze.
[Bild PIR 1
Außerdem prüfen wird derzeit verschiedene neue Sensoren auf Tauglichkeit und Erkennungsgenauigkeit. Bitte hab noch ein wenig Geduld...

### 2.3) Meine Magnete lösen wie von geisterhand aus, obwohl keine Bewegung im Klappenbereich war
Zwei Hauptauslöser haben wir bisher bereits identifizieren können: Bewegungen in großer Entfernung (z.B. Baum/Äste/Schaukel in bis zu 10-15m Entfernung!), aber eventuell auch induktionsbedingte Auslösungen der PIR-Sensoren (bzw. Fehlerströme auf den Leitungen).
Den ersten Punkt kann man oft mit Hausmitteln beheben. Ein wenig undurchsichtiges Klebeband oder anderes wasserfestes, undurchlässiges Material auf den Sensorbereich kleben, der das Problem verursacht. Im Bild siehst du den Baum (rot eingerahmter Bereich), der sich bei leichtem Wind bewegt und den Sensor auslöst.
[Bild PIR 2]
Durch ein Abkleben des oberen Bereichs des äußeren PIR-Sensors kann diese Störquelle "ausgeblendet" werden. Hier kann man durch ein wenig rumprobieren den genauen Bereich bestimmen, den man abkleben muss, sodass die Katze weiterhin den Sensor auslöst, gleichzeit aber keine anderen Bewegungen.
[Bild PIR 3]
