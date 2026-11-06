# Driveup

Upload big files to Google Drive without babysitting them. Uploads survive sleep, lid-close, wifi drops and browser restarts, and every finished file is checked against Drive's MD5.

**Use it:** https://nanj-dnd.github.io/driveup/ (Chrome, Edge or Arc). Nothing to install.

- **`web/`** is the website. It's plain HTML and JavaScript with no build step, deployed to GitHub Pages on every push to `main`. Hosting your own copy is covered in [`web/SETUP.md`](web/SETUP.md).
- **`driveup.py`** is the same idea as a command-line tool:

  ```sh
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  ./driveup add ~/Movies/big-folder --to "Backups/Movies"
  ./driveup status
  ```

  It needs an OAuth client of type *Desktop app*, saved as `~/.driveup/credentials.json`.

Driveup only asks for the `drive.file` scope, so it can see the files it uploaded and nothing else in your Drive. Files go straight from your browser to Google; there's no Driveup server.
