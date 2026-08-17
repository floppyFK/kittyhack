# v2.7.1

## Bugfixes

- **Model download after training**: After a successful remote training, the new model is downloaded again. In v2.7.0 the background downloader was started from the wrong directory, so it failed immediately with `No module named 'src'` and the UI stayed on the error message.
