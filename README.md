# Driveup

Upload big files to Google Drive from the terminal without babysitting them. Uploads survive sleep, lid-close, wifi drops and restarts, and every finished file is checked against Drive's MD5.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/nanj-dnd/driveup/main/install.sh | sh
```

Needs Python 3 (on a Mac: `xcode-select --install`). Or with pipx: `pipx install git+https://github.com/nanj-dnd/driveup.git`.

## Use

```sh
driveup                                                 # pick files or folders in a window, then upload
driveup pick --to "Backups/2026"                         # same, into a chosen Drive folder
driveup add ~/Movies/big-folder --to "Backups/Movies"   # or name them yourself
driveup status                                          # what's done, partial, not started
driveup run                                             # continue anything unfinished
driveup clear                                           # forget finished uploads
```

The first run opens your browser once to sign in to Google. If it's interrupted (Ctrl-C, sleep, wifi, reboot), run `driveup run` and it continues from the last piece Drive received.

Driveup only asks for the `drive.file` scope, so it can see the files it uploaded and nothing else in your Drive. Files go straight from your computer to Google.
