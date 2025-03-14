# v1.5.0

## Highlights
With version 1.5.0, the first preparations begin to allow you to adapt the "AI" to your cat and your environment!
First, a lot of data needs to be collected to train the "AI" later.

What does this mean in concrete terms?
For reliable object detection, **at least** 100 images are required later for the category *No Mouse* and at least 100 images for the category *Mouse*. (Birds and other animals are simply classified as a mouse 😉).
In the event view, there is now a download button that allows you to download all images of an event as a *.zip* file.  
You should collect as many different images as possible. The greater the variance later, the better – so collect photos during the day, at night, in bright sunlight with strong shadows, and so on.  

> **Note:** When downloading images as a *.zip* file, your browser (e.g., Google Chrome) may display a warning that the connection is not secure and block the download. This message is harmless as long as you are accessing the Kittyflap only from your home Wi-Fi. If necessary, you may need to confirm that you want to keep the file anyway.  
> A secure connection via HTTPS can help here. You can find guides online – one possible approach is a *Reverse Proxy*, such as the [NGINX Proxy Manager](https://nginxproxymanager.com/).

Additionally, a new model variant for object detection has been selected as the default. This should reduce false detections of supposed mice on patio furniture or similar objects. Whether this model performs better in all situations cannot be stated universally – therefore, you can switch between the new and the old variant.

---------------

## New Features
- **Image Download**: Images can now be downloaded per event.
- **Better Performance**: Object detection now works significantly faster. Previously, about 3 images per second were analyzed; with this version, it's 5-6 images per second!
- **Choose between different models**: It is now possible to choose between different variants of the object detection models
- **Configurable Locking on Detected Prey**: In the configuration menu, a lock time for the flap can now be set when prey is detected. If the lock time is active, a corresponding notice is displayed on the homepage. This lock can be lifted early via a button.

## Improvements
- **Saving Events on Motion**: An event is now only created if the external motion sensor detects movement and in at least one of the images the value for the `Minimum Detection Threshold` (*Mouse* or *No Mouse*) has been exceeded. In this case, **all** associated images during the event are saved.
- **Buffered Images**: Since the external motion sensor takes about 2-3 seconds to report movement, camera images are now buffered for several seconds. This should ensure that even very fast cats are reliably captured in the evaluated images.
- **New Configuration Menu**: The *Configuration* panel has been redesigned for better clarity.

## Bugfixes
- **Continuous Evaluation**: If the minimum number of images to analyze is reached and a mouse is only detected in one of the subsequent images, the flap will now still be locked again.
- **Outgoing Events**: A bug was fixed that prevented events from being saved when a cat went outside.

## Note on Storage Management
Since significantly more images are now stored than in previous versions, it is recommended to increase the `Maximum Number of Images in the Database`.
Depending on the specifications of your Kittyflap (16 GB or 32 GB storage), you can adjust the value accordingly.
**8000** images should be easily possible in both variants.
It is best to monitor the available storage space in the **Info** panel. As a guideline: **200 MB** of storage space is required for every **1000 images** (100MB database + 100MB backup).
