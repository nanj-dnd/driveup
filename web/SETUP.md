# Driveup

Upload big files to Google Drive from Chrome, Edge or Arc. Uploads continue after sleep, wifi drops and browser restarts, and every finished file is checked against Drive's copy.

Open https://nanj-dnd.github.io/driveup/ and click **Sign in with Google**.

## Keeping long uploads going

- **Allow pop-ups for the site.** Google sign-in lasts an hour. Driveup renews it in a small Google window; if the browser blocks that window, uploads pause until you click **Sign in**. Allow pop-ups once (the icon at the right of the address bar) and renewals happen on their own.
- **Keep the Driveup tab open.** It can be in the background. Closing the lid is fine; closing the tab pauses uploads until you open Driveup again.
- **After a browser restart**, the browser asks once for permission to read the files again (that's the **Resume uploads** button).
- Drive keeps a half-finished upload for about a week. After that, that one file starts over.

## Hosting your own copy

1. Fork this repo and turn on GitHub Pages with **Source: GitHub Actions** (Settings → Pages). The included workflow publishes `web/`.
2. Create a project at console.cloud.google.com and enable the **Google Drive API**.
3. In **Google Auth Platform**, configure the app as **External**.
4. Create an OAuth client of type **Web application**. Under **Authorised JavaScript origins**, add your site's origin, e.g. `https://<you>.github.io` (and `http://localhost:8000` for local testing).
5. Under **Audience**, press **Publish app**. Driveup only uses the `drive.file` scope (files it creates), which Google treats as non-sensitive, so no review is needed.
6. Put the client ID in `web/config.js` and push.

To try it locally: `python3 -m http.server 8000 -d web`, then open http://localhost:8000.

## What it handles

- **No "(1)" duplicates.** Files already in Drive with identical content are skipped. For the same name with different content, Settings chooses keep both or replace (Drive keeps the old version for 30 days).
- **Drive full.** Free space is checked before uploading. If Drive fills up anyway, the whole queue pauses with one clear message instead of failing file after file.
- **Google's 750 GB/day limit and rate limits.** Detected and explained; Driveup retries on its own (hourly for the daily limit).
- **Hogging your internet.** Optional speed limit in Settings.
- **Folders of small photos.** Three files upload at once (only one big file at a time, so big ones still finish in order).
- **Sending it to someone.** "Get link" on any file or uploaded folder copies an anyone-with-the-link view link.
- **Walking away.** A notification when uploads finish or need you, and a CSV report of what uploaded, what didn't, and why.
- Driveup creates its own folders in Drive. It can't see or upload into folders you made by hand.
