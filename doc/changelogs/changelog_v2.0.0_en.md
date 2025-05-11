# v2.0.0

## Highlights
The time has come! With this version, you have the ability to individually train the AI for your cat. No more chance for mice 😉

> ⚠️ **ATTENTION:** This update requires downloading and installing approximately 250 MB of data from the internet. The update process will therefore take significantly longer than usual!
> The safest way is to run this version update through the setup script:
> ```bash
> sudo curl -sSL https://raw.githubusercontent.com/floppyFK/kittyhack/main/setup/kittyhack-setup.sh -o /tmp/kittyhack-setup.sh && sudo chmod +x /tmp/kittyhack-setup.sh && sudo /tmp/kittyhack-setup.sh && sudo rm /tmp/kittyhack-setup.sh
> ```
>
> You can also run the update through the web interface. However, during the update, you might see a "Connection Lost" message - **don't worry, the installation continues running in the background!**
> Please wait **at least 15 minutes** (or longer if you have a very slow internet connection), and then refresh the page. Afterwards, you'll need to restart the cat flap once more and you'll be on v2.0!

---------------

## New Features
- **AI Training**: Individual training of the AI for your cat and your environment. With the help of the built-in Label Studio Server, you can continuously improve the model used to evaluate camera images.
- **Unlocking via Image Analysis**: In addition to unlocking using your cat's RFID, there is now the option to unlock the flap based on image recognition.

## Improvements
- **Design Adjustment**: All tabs now have a uniform design.
- **Translation**: All tiles are now fully available in German.
- **Automatic Reconnect**: If the connection to Kittyhack is interrupted (for example because you minimized the browser on your smartphone), it will now be automatically restored.

## Bugfixes
- **Display on iOS**: The display of the event list on iOS devices has been fixed.

## Minor Changes
- **Logfile Download**: All relevant logs can now be downloaded at once via a button
- **Crash Detection**: The software now recognizes if unexpected crashes occurred. If there are multiple consecutive crashes, a warning message is displayed.