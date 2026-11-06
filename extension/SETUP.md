# Driveup

Upload big files to Google Drive from Chrome. Uploads continue after sleep, wifi drops and Chrome restarts, and every finished file is checked against Drive's copy.

## Install (anyone)

1. Unzip `Driveup.zip`.
2. Open `chrome://extensions`, turn on **Developer mode** (top right), click **Load unpacked**, and pick the unzipped folder.
3. Click the Driveup icon in the toolbar (pin it from the puzzle-piece menu), then **Sign in with Google**.

Works in Chrome, Edge, Brave and Arc.

## One-time Google setup (only the person sharing it)

The extension's own setup screen walks through this. In short:

1. Create a project at console.cloud.google.com and enable the **Google Drive API**.
2. In **Google Auth Platform**, configure the app as **External**.
3. Create an OAuth client of type **Web application** with this redirect URI:
   `https://ieicojplapbjhmooncikdkkkgfdeailn.chromiumapp.org/`
4. Under **Audience**, press **Publish app**. Driveup only uses the `drive.file` scope (files it creates), which Google treats as non-sensitive, so no review is needed.
5. Paste the client ID into `config.js`, reload the extension, and re-zip the folder to share.

The extension ID (and so the redirect URI) stays the same on every computer because of the `key` in `manifest.json`.

## Chrome Web Store (optional, easiest for others)

Publishing to the Web Store ($5 one-time developer fee) lets people install with one click instead of Developer mode. The store assigns a new extension ID, so add `https://<store-id>.chromiumapp.org/` as a second redirect URI on the same OAuth client.

## What it handles

- **No "(1)" duplicates.** Files already in Drive with identical content are skipped. For the same name with different content, Settings chooses keep both or replace (Drive keeps the old version for 30 days).
- **Drive full.** Free space is checked before uploading. If Drive fills up anyway, the whole queue pauses with one clear message instead of failing file after file.
- **Google's 750 GB/day limit and rate limits.** Detected and explained; Driveup retries on its own (hourly for the daily limit).
- **Hogging your internet.** Optional speed limit in Settings.
- **Folders of small photos.** Three files upload at once (only one big file at a time, so big ones still finish in order).
- **Sending it to someone.** "Get link" on any file or uploaded folder copies an anyone-with-the-link view link.
- **Walking away.** A notification when uploads finish or need you, and a CSV report of what uploaded, what didn't, and why.

## Good to know

- Keep the Driveup tab open while uploading. Closing the lid is fine; closing the tab pauses uploads until you open Driveup again.
- After Chrome restarts, Chrome asks once for permission to read the files again (that's the **Resume uploads** button).
- Drive keeps a half-finished upload for about a week. After that, that one file starts over.
- Driveup creates its own folders in Drive. It can't see or upload into folders you made by hand.
