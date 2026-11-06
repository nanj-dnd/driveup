# Driveup

Upload big files to Google Drive without babysitting them. Uploads survive sleep, lid-close, wifi drops and restarts, and every finished file is checked against Drive's MD5.

- **`extension/`** is a Chrome extension, the main way to use it. Install and setup steps are in [`extension/SETUP.md`](extension/SETUP.md).
- **`driveup.py`** is the same idea as a command-line tool:

  ```sh
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  ./driveup add ~/Movies/big-folder --to "Backups/Movies"
  ./driveup status
  ```

  It needs an OAuth client of type *Desktop app*, saved as `~/.driveup/credentials.json`.

Driveup only asks for the `drive.file` scope, so it can see the files it uploaded and nothing else in your Drive.
