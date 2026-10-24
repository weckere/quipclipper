# Re-index hooks: Bazarr, Sonarr and Radarr

Dialogue search reads only indexed files, and an index entry belongs to one version
of a file's subtitles. When Bazarr downloads or upgrades a subtitle, or Sonarr or
Radarr imports, upgrades or renames a file, that file drops out of search until it is
indexed again. A hook tells quipclipper the moment that happens, and quipclipper
re-indexes just that file:

- in the background, one file at a time, at idle priority (like **Index the whole
  library**), so it never competes with playback or clipping;
- each file once, however often it is named (Bazarr calls once per subtitle);
- only if its index is out of date: a file whose index is current is left alone.

Three pieces:

1. **A hook token** on the server (`QC_HOOK_TOKEN`). It can only queue re-indexes;
   every other endpoint refuses it. Your full `QC_API_TOKEN` works too.
2. **A path map** on the server (`QC_PATH_MAP`), when the other app sees your media
   under different paths (its own container mounts).
3. **The caller**: Bazarr runs the small script
   [`contrib/hooks/quipclipper-reindex`](../contrib/hooks/quipclipper-reindex) (POSIX
   `sh` and `curl`, nothing else); Sonarr and Radarr can use a plain webhook, no
   script needed.

The endpoint itself is [`POST /api/index/file`](API.md#re-index-hooks) (CLI:
`quipclipper index.file`).

## 1. The server

Make a token and give it to quipclipper's backend:

```bash
openssl rand -hex 32
```

| Setting | Example | What it does |
|---|---|---|
| `QC_HOOK_TOKEN` (or `QC_HOOK_TOKEN_FILE`) | `9f2c…` | Token(s) that may only queue re-indexes. Comma-separate several to rotate (the backend accepts all of them; behind a password gate the `hook` login knows only the first, so callers move to the new token when it goes first). |
| `QC_PATH_MAP` | `/tv=/media/shows:/movies=/media/movies` | The other apps' paths → quipclipper's. `<theirs>=<ours>` pairs, separated by `:` or `;`; the longest matching prefix wins; a path nothing matches is used as it is. |

**Docker Compose** (`docker-compose.example.yml`): set both on the `app` service. If
the site has a password (`QC_PASSWORD`), also set `QC_HOOK_TOKEN` on the `web`
(nginx) service: it adds a `hook` login whose password is the token, so the hook
passes the password gate with the same secret.

**NixOS module:** `services.quipclipper-web.hookTokenFile` (a file holding just the
token; root-only is fine, it's handed over as a systemd credential) and
`services.quipclipper-web.pathMap`:

```nix
services.quipclipper-web = {
  hookTokenFile = "/run/secrets/quipclipper-hook-token";
  pathMap = {
    "/tv" = "/srv/media/shows";
    "/movies" = "/srv/media/movies";
  };
};
```

With a `passwordFile`, add a `hook` line to it with the token as the password
(`htpasswd -B <file> hook`).

**Check it** from the machine the other app runs on (its container, ideally):

```bash
curl -sS -u hook:<token> https://quip.example/api/index/file
```

`{"job_id":null,"pending":[],"recent":[],"job":null,"token":true}` means the URL, the
network and the token are right. `"token":false` means the server has no hook token set
(or none matching): the re-index requests themselves would be refused.

The login name `hook` is reserved for this: anywhere but `/api/index/file` it gets 403,
whatever the password.

## 2. Bazarr

Bazarr runs a command after each subtitle it saves (downloads, upgrades, manual
searches and uploads), with the episode or movie path as `{{episode}}` and the new
subtitle's path as `{{subtitles}}`.

1. **Put the script and the token where Bazarr can read them.** With the
   linuxserver image, its `/config` folder is the easy place (on the host, the folder
   you mount there):

   ```bash
   curl -fsSLo /path/to/bazarr/config/quipclipper-reindex \
     https://raw.githubusercontent.com/weckere/quipclipper/main/contrib/hooks/quipclipper-reindex
   chmod 755 /path/to/bazarr/config/quipclipper-reindex
   printf '%s\n' '<token>' > /path/to/bazarr/config/quipclipper-hook-token
   chmod 600 /path/to/bazarr/config/quipclipper-hook-token
   chown <bazarr's PUID>:<PGID> /path/to/bazarr/config/quipclipper-hook-token
   ```

   Bazarr's image has `curl`. Check the script from inside the container:

   ```bash
   docker exec -u abc bazarr /config/quipclipper-reindex \
     --url https://quip.example --token-file /config/quipclipper-hook-token \
     "/tv/Some Show/Season 01/Some Show - S01E01.mkv"
   ```

   It answers `quipclipper-reindex: queued 1 file(s) for re-indexing`.

2. **Settings → Subtitles → Custom Post-Processing:** switch on **Custom
   Post-Processing** and set **Command** to

   ```
   /config/quipclipper-reindex --url https://quip.example --token-file /config/quipclipper-hook-token "{{episode}}" "{{subtitles}}"
   ```

   Leave the score-threshold switches off (they would skip the hook for good
   subtitles). **Save.**

Bazarr splits the command like a shell but runs no shell, and its environment isn't
yours, so the URL and token file go in as flags. Bazarr's debug log prints the
command, which is why the token is read from a file and never written on the
command line. Anything the script writes to stderr shows in Bazarr's log as a
post-processing error, with the reason (`HTTP 401` = wrong token; `outside_roots:
/tv/…` = add that prefix to `QC_PATH_MAP`).

## 3. Sonarr and Radarr

Sonarr and Radarr need this less than Bazarr: a new or upgraded video is a new file,
so it is simply not indexed until something indexes it. The hook makes it searchable
straight away, and a rename keeps it searchable.

### A webhook (no script)

**Settings → Connect → + → Webhook:**

| Field | Value |
|---|---|
| Triggers | **On File Import**, **On File Upgrade**, **On Rename** (Sonarr v4 also: **On Import Complete**) |
| URL | `https://quip.example/api/index/file` |
| Method | `POST` |
| Username | `hook` |
| Password | the token |

**Test** sends a test event; quipclipper answers 200 when the URL and token are
right. quipclipper reads the file paths out of Sonarr's and Radarr's own webhook
body (`episodeFile`, `episodeFiles`, `movieFile`, `renamedEpisodeFiles`,
`renamedMovieFiles`), mapped through `QC_PATH_MAP`.

### Or a Custom Script

**Settings → Connect → + → Custom Script**, same triggers, **Path** the script inside
the container (for example `/config/quipclipper-reindex`). Sonarr and Radarr pass no
arguments, so set the URL and token file in the container's environment:

```yaml
environment:
  QUIPCLIPPER_URL: https://quip.example
  QUIPCLIPPER_TOKEN_FILE: /config/quipclipper-hook-token
```

The script reads the event from Sonarr's and Radarr's `*_eventtype`,
`*_episodefile_path`, `*_moviefile_path` and (rename) `*_paths` variables. Their
**Test** button checks the URL and the token.

## The script

```
quipclipper-reindex [--url URL] [--token-file FILE] [--user USER] [--force] PATH...
```

| Flag | Environment | Meaning |
|---|---|---|
| `--url` | `QUIPCLIPPER_URL` | The quipclipper server. |
| `--token-file` | `QUIPCLIPPER_TOKEN_FILE` (or `QUIPCLIPPER_TOKEN`) | A file holding the hook token (or the API token). |
| `--user` | `QUIPCLIPPER_USER` | The login sent with the token through a password gate: `hook` (default), or `api` with the full API token. |
| `--force` | | Re-read the files even when their index is current. |

Each `PATH` is a media file or a subtitle file (quipclipper finds the media file
whose name starts the subtitle's, in the same folder). It exits 0 when the server
queued the files, 1 when it refused (the reason on stderr), 2 for a setup error. The
token goes to `curl` through a pipe: it is never on a command line, in the process
list or in the output.

## What happens on the server

`quipclipper index.file-status` (or `GET /api/index/file`) shows the files waiting
and the last 50 results:

| Status | Meaning |
|---|---|
| `indexed` | Re-read; search has its current lines. |
| `current` | Its index was already up to date: nothing to do. |
| `no_subs` | It has no subtitles to index (no sidecar, no text track). |
| `missing` | The file was gone by the time its turn came. |
| `failed` | It couldn't be read (the error is kept). |
| `cancelled` | The job was cancelled (Jobs, or `quipclipper job.cancel`). |

The re-index runs in the same single-file queue as **Index the whole library**: never
two at once. While a library run is going, it takes the hooks' files between its own,
so a hook never waits for the whole library. Files still waiting when the server
restarts are dropped; the next library run (or hook) picks them up.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `HTTP 401` | The token is wrong (a server with only an API token set doesn't know the hook token). |
| `HTTP 403 Missing x-quipclipper header` (or the Test says "no hook token set") | The server has no token set at all, so it ignores the one sent: set `QC_HOOK_TOKEN` on the server. |
| `HTTP 401` with an HTML body | A password gate (nginx) refused it: add the `hook` login (Docker: `QC_HOOK_TOKEN` on the `web` service; NixOS: a `hook` line in `passwordFile`). |
| `HTTP 403 … may only queue re-indexes` | A hook token used for another endpoint: working as intended. |
| `HTTP 422`, `outside_roots: /tv/…` | The path isn't under a media root once mapped: add its prefix to `QC_PATH_MAP` (`/tv=/your/shows`). |
| `no_media: …srt` | No media file next to the subtitle has a name that starts the subtitle's. |
| `curl: (6) Could not resolve host` / `(7) Failed to connect` | The other app can't reach the URL: from inside its container, try the `curl` in step 1. |
