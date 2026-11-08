# Driveup

Upload big files to Google Drive from the terminal without babysitting them. Uploads survive sleep, lid-close, wifi drops and restarts, and every finished file is checked against Drive's MD5.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/nanj-dnd/driveup/main/install.sh | sh
```

Needs Python 3 (on a Mac: `xcode-select --install`, or `brew install python`; the installer picks the newest Python it finds). Or with pipx: `pipx install git+https://github.com/nanj-dnd/driveup.git`.

## Use

```sh
driveup                                                 # pick files or folders, then the Drive folder, then upload
driveup pick --to "Backups/2026"                        # same, into a chosen Drive folder
driveup add ~/Movies/big-folder --to "Backups/Movies"   # or name them yourself
driveup status                                          # what's done, partial, not started
driveup run                                             # continue anything unfinished
driveup clear                                           # forget finished uploads
```

### More tools

```sh
driveup check ~/Photos --to "Backups"     # compare with Drive, upload nothing: what's missing or different
driveup free ~/Photos                     # move files Drive has confirmed to the Trash, to free up space
driveup link ~/Photos/Trip                # view-only share link for an uploaded file or folder (copied)
driveup schedule ~/Documents --to "Backups" --every daily   # automatic backups of new and changed files
driveup schedule --list                   # see them; turn one off with --off <id>
```

When you pick files, driveup then asks which Drive folder to use: one it made before, the top of My Drive, or a new one. It only lists folders it created itself, because its Google permission (`drive.file`) lets it see nothing else in your Drive.

`free` only touches files whose upload was checked against Drive's checksum, that haven't changed since, and that are still in Drive. They go to the Trash of the disk they're on (like Finder), so you can put them back until you empty it, and freeing an external drive doesn't copy anything onto your Mac.

Scheduled backups log to `~/.driveup/logs/`, and if one can't finish (sign-in expired, Drive full, macOS blocking a folder, files that failed) you also get a notification.

The first run opens your browser once to sign in to Google. If it's interrupted (Ctrl-C, sleep, wifi, reboot), run `driveup run` and it continues from the last piece Drive received.

Driveup only asks for the `drive.file` scope, so it can see the files it uploaded and nothing else in your Drive. Files go straight from your computer to Google.

## License

MIT. See [LICENSE](LICENSE).
