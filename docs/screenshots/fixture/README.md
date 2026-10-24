# Screenshot fixture: "Large Locutus Leporid (2016)"

The README screenshots are made on a local dev server with openly licensed media
only, never a real library.

**The footage** is *Big Buck Bunny*, © 2008 Blender Foundation,
[www.bigbuckbunny.org](https://peach.blender.org/), licensed
[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). It is a 100 s section
(0:25 to 2:05) of the Blender Foundation's own upload
([YouTube aqz-KE-bpKQ](https://www.youtube.com/watch?v=aqz-KE-bpKQ)), scaled to 720p.
**The subtitles** ([`Large Locutus Leporid (2016).srt`](Large%20Locutus%20Leporid%20%282016%29.srt))
are a few Borg lines for dialogue search to find.

## Remaking the screenshots

1. Build the fixture (needs `ffmpeg` and `yt-dlp`):

   ```bash
   docs/screenshots/fixture/make-fixture.sh ~/qc-fixture
   ```

   It makes two media roots: `Movies/Large Locutus Leporid (2016)/` (the whole
   section) and `Shows/The Next Leporation/Season 01/` (three short "episodes" of it,
   each with its share of the subtitles).

2. Start a fake Jellyfin with two paused clients (for the Now playing card), the titles
   and the posters and stills (cut from the fixture's own frames into `~/qc-fixture/art`):

   ```bash
   FIXTURE=~/qc-fixture python3 docs/screenshots/fixture/fake_jellyfin.py
   ```

3. Start the web app on port 8017 against the fixture:

   ```bash
   QC_MEDIA_ROOTS=~/qc-fixture/Movies:~/qc-fixture/Shows QC_PORT=8017 \
   QC_STATE_DIR=~/qc-fixture/state QC_CLIPS_DIR=~/qc-fixture/clips \
   QC_PUBLIC_URL=https://quipclipper.example.net \
   QC_JELLYFIN_URL=http://127.0.0.1:8097 QC_JELLYFIN_API_KEY=fixture \
   QC_JELLYFIN_PATH_MAP=/data/movies=$HOME/qc-fixture/Movies:/data/shows=$HOME/qc-fixture/Shows \
   python -m quipclipper_web
   ```

4. Shoot (needs `pip install playwright` and Google Chrome), then compress:

   ```bash
   FIXTURE=~/qc-fixture OUT=/tmp/shots python docs/screenshots/fixture/shoot.py
   for f in /tmp/shots/*.png; do pngquant --quality 70-92 --strip --force \
     --output "docs/screenshots/$(basename "$f")" "$f"; done
   ```

   The search shot is cropped to 1600 × 740 (above the footer) (`ffmpeg -vf crop=1600:850:0:0`).
   The library shot bumps one episode's subtitle date for the shot (so it shows the
   **changed** chip and banner) and puts it back after. The pairing shots (`pairing`,
   `connected-apps`) ask for access as "LeadStinger on studio-macbook" through the API, with
   "LeadStinger on booth-mini" already let in, and deny the request afterwards; the address
   on the banner is the fixture server's own `127.0.0.1`. Transcription stays off (no
   `QC_TRANSCRIBE`), so none of it shows.
   The workspace shots wait for the waveform's peaks; the phone shot scrolls the waveform
   strip up to just above the pinned selection. Look at every image before committing it.
