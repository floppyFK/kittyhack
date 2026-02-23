### Remote mode (remote-control) - Overview

Remote mode is a new, optional feature. It splits Kittyhack across two devices:

- **Kittyflap**: still handles the local hardware (for example sensors, RFID, locks).
- **Remote device** (separate Linux PC/VM): runs the Web UI and AI inference.

This reduces the load on the Kittyflap and - depending on the hardware used - allows for significantly higher frame rates during cat and prey detection.
In addition, more complex models can be used that are not capable of running on the comparatively low-performance Raspberry Pi 4 inside the Kittyflap.

---

#### Schematics

##### Architecture: who talks to whom?

![Remote-mode architecture](diagrams/remote-mode-architecture.svg)

##### Control takeover flow: what happens when the remote connects

![Remote-mode control takeover flow](diagrams/remote-mode-control-takeover.svg)

##### Connection loss recovery

![Remote-mode connection loss recovery](diagrams/remote-mode-connection-loss.svg)

---

#### What runs where?

##### On the Kittyflap
- Kittyhack runs normally by default.
- When a remote device takes control, the Kittyflap shows an info page instead of the regular UI.
- If the connection is lost, the Kittyflap automatically takes over again as fallback.

##### On the remote device
- Runs the Kittyhack Web UI.
- Connects to the Kittyflap and controls it remotely.

---

#### Setup

1. Install/update Kittyhack on the **Kittyflap** as usual.
2. Install Kittyhack on the **remote device** and choose “remote-mode” in setup.
   Setup script:
   ```bash
   sudo curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && sudo chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh && sudo rm /tmp/kittyhack-setup.sh
   ```
3. In the remote device Web UI, enter the Kittyflap IP address.

Open remote UI in browser:

- `http://<IP-of-remote-device>/`

---

#### Important note about initial sync

During setup, you can transfer data from Kittyflap to the remote device, for example:

- models
- pictures
- database
- configuration
- Label Studio data

This sync is available **only during setup**.  
A later/manual re-sync is currently **not** available.

---

#### Where is data stored?

When Kittyflap is controlled by a remote device:

- new events are saved **only on the remote device**
- settings changes are saved **only on the remote device**

If the remote device goes offline, Kittyflap takes over as **fallback** locally again - using the settings stored **on the Kittyflap itself**.

In short: After some time, the remote device and the Kittyflap will differ in their data (events and configuration)!

The remote connection can be terminated via the web UI of the remote device. After that, configuration is once again possible through the Kittyflap’s web UI.

---

#### What happens if the connection is lost?

- Kittyflap releases remote control.
- Safety actions are completed cleanly.
- Kittyflap returns to local autonomous operation.
- The remote device will try to reconnect automatically later.

---

#### Software and Hardware Requirements

##### Remote device
- Debian or Ubuntu (ideally a system dedicated exclusively to running Kittyhack)
- AMD64 (x86_64) CPU with at least 4 cores
- At least 2 GB RAM

##### Network
- The remote device must be able to reach the Kittyflap in your local network.
- The kittyflap must be reachable at port `80` and `8888`
- This feature is designed only for a **trusted LAN** (do not expose it to the public internet).
