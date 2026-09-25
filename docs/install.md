# Installation

Sieve needs Python 3.11 or newer and about 200 MB of disk for a typical
catalogue. It has five runtime dependencies, no build step, no compiler, and no
second daemon.

It does **not** need an [Invidious](https://github.com/iv-org/invidious)
instance: it can read YouTube directly. If you do run Invidious, Sieve uses it
while it answers and falls back to YouTube when it does not — see
[Getting videos](configuration.md#getting-videos). Please do not point it at
someone else's public instance: warming the catalogue makes a lot of requests.

## Contents

- [Try it without an instance](#try-it-without-an-instance)
- [From source](#from-source)
- [With Docker](#with-docker)
- [Connecting to Invidious](#connecting-to-invidious)
- [Importing your data](#importing-your-data)
- [Running it as a service](#running-it-as-a-service)
- [Upgrading](#upgrading)
- [Uninstalling](#uninstalling)

---

## Try it without an instance

No Invidious, no account, no export file. Sieve pulls real videos for a few
topics from YouTube's public feeds and ranks them:

```bash
git clone https://github.com/whereixuezugi/sieve
cd sieve
pip install -e '.[youtube]'   # yt-dlp is optional; it adds durations and search

sieve fetch --topics science,history,programming
sieve serve
```

Open <http://127.0.0.1:8377>. The videos are real and play in Sieve's own
player. Or skip the `fetch` line and press **Fetch** in the page header once
you have looked at the controls — nothing is fetched until you do.

With no network at all, `sieve demo --synthetic` builds 400 invented videos
across ten made-up channels, with a plausible watch history, so every control
can still be tried. Those cannot be played anywhere, and real videos replace
them automatically on the first successful pull, or `sieve demo remove` clears
them now.

## From source

```bash
git clone https://github.com/whereixuezugi/sieve
cd sieve
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

For development, add the test and lint tools:

```bash
pip install -e '.[dev]'
ruff check .
pytest -q
```

<details>
<summary>Optional extras, and what they cost</summary>

Neither is needed, and neither is installed by default.

| Extra | Install | What it adds | What it costs |
|---|---|---|---|
| `vision` | `pip install -e '.[vision]'` | Thumbnail NSFW scoring via OpenNSFW2, folded into the `nsfw` score | A TensorFlow dependency, several hundred MB, and one HTTP fetch plus one inference per video. Run it with `sieve vision` rather than on the request path. |
| `embeddings` | `pip install -e '.[embeddings]'` | ONNX runtime, if you want to host an embedding model in-process rather than through Ollama | ~100 MB, plus whatever the model weighs |

The `embeddings` extra is not required to use a real embedding model. Setting
`embed_provider = "ollama"` talks to an Ollama you are already running and needs
nothing installed here.

</details>

---

## With Docker

```bash
docker compose up -d
```

That is the whole setup: no Invidious needed. The image includes yt-dlp and
runs as an unprivileged user, and the compose file keeps your data in the
`sieve-data` volume. Open <http://127.0.0.1:8377> and import your
subscriptions on the Controls page.

To use your own Invidious as well, uncomment `SIEVE_INSTANCES` in
`docker-compose.yml`. Updating, backups, running without Compose and every
environment variable are covered in the README's
[Running with Docker](../README.md#running-with-docker).

CLI commands run inside the container:

```bash
docker compose exec sieve sieve doctor
docker compose exec sieve sieve sync
```

> [!IMPORTANT]
> The published port is bound to `127.0.0.1` on purpose. Sieve has no
> authentication of any kind — see [Exposure](#a-note-on-exposure) below.

---

## Connecting to Invidious

Copy the example config and point it at your instance:

```bash
mkdir -p ~/.config/sieve
cp config.example.toml ~/.config/sieve/config.toml
```

The two settings that matter:

```toml
instances  = ["http://127.0.0.1:3000"]
watch_base = "http://127.0.0.1:3000/watch?v="
```

You only need this if you run Invidious; without it, Sieve reads YouTube
directly. `instances` is a failover list, not a load-balancing pool. Sieve uses the first
one that answers and stays on it until it stops answering. Extra entries are
insurance, not throughput.

Check the connection before going further:

```bash
sieve doctor
```

### Serving Sieve as the homepage

Sieve owns `/`; Invidious owns everything else. Three lines of nginx:

```nginx
server {
    listen 443 ssl;
    server_name video.example.com;

    # Sieve: the homepage and its own pages
    location / {
        proxy_pass http://127.0.0.1:8377;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Invidious: the player, the API, thumbnails, everything else
    location ~ ^/(watch|embed|vi|ggpht|api|channel|playlist|search|feed|latest_version|videoplayback) {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

<details>
<summary>Caddy</summary>

```caddyfile
video.example.com {
    @invidious path_regexp ^/(watch|embed|vi|ggpht|api|channel|playlist|search|feed|latest_version|videoplayback)
    reverse_proxy @invidious 127.0.0.1:3000
    reverse_proxy 127.0.0.1:8377
}
```

</details>

Set `watch_base` to the public URL once you do this, so the Watch links point
somewhere your browser can reach:

```toml
watch_base = "https://video.example.com/watch?v="
```

### A note on exposure

Sieve has no login, no sessions and no multi-user support. It is a single-user
tool that assumes whoever can reach it is you. Anyone who can open it can read
your watch history and change your settings.

Put it behind a VPN, an SSH tunnel, or your reverse proxy's authentication. Do
not put it on the open internet.

---

## Importing your data

Sieve starts empty and stays useless until it knows something about you. Two
imports fix that.

### Subscriptions

Any of these export formats work:

| Source | Where to get it |
|---|---|
| Invidious | Settings, then Import/Export, then "Export data as JSON" |
| NewPipe | Settings, then Content, then Export database |
| FreeTube | Settings, then Data Settings, then Export Subscriptions |

```bash
sieve import subs ~/Downloads/invidious-data.json
```

Or drop the file on the Controls page.

### Watch history

Invidious exports it alongside subscriptions. Google Takeout works too — select
YouTube, then "history", and use `watch-history.json`.

```bash
sieve import history ~/Downloads/watch-history.json
```

> [!NOTE]
> Takeout gives timestamps but no completion percentage, so imported rows are
> recorded at a conservative 0.6. They inform the model without pretending to a
> precision the data does not have. Real completion arrives once you wire up
> [watch progress](player.md).

### Playlists

Paste a playlist id or URL into the Playlists panel on the Controls page, or:

```bash
sieve import playlist PLxxxxxxxxxxxxxxxx
```

Only public and unlisted playlists can be read. YouTube's own Watch Later is
private to your Google account and not reachable through Invidious — keep a
public or unlisted playlist on your Invidious account for that purpose instead.

### Choosing where videos open

Out of the box, videos open at an Invidious on `127.0.0.1:3000`. If that is not
where yours runs, pick a provider under **Controls, Playback** — Invidious or
Piped at an address you give, YouTube, YouTube's no-cookie domain, the FreeTube
app, or your own URL template. **Open a test video** in that panel checks the
choice in one click. `sieve doctor` flags the unconfirmed default, and its full
check (`GET /api/doctor?quick=false`) tests whether anything answers there.

### First run

```bash
sieve sync      # pull recent videos for every channel you follow
sieve score     # score them; this is the slow part
sieve affinity  # derive per-channel affinity from watch time
sieve quality   # score channels from their own catalogues
sieve serve
```

Scoring runs at roughly 35 videos a second without transcripts and 4 a second
with them, so the first pass over a large subscription list takes a while. After
that the background worker keeps up on its own and you never run these by hand
again.

---

## Running it as a service

<details>
<summary>systemd user unit</summary>

`~/.config/systemd/user/sieve.service`:

```ini
[Unit]
Description=Sieve
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/sieve/.venv/bin/sieve serve
Restart=on-failure
RestartSec=10

# Sieve only needs its own data directory.
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/sieve
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now sieve
loginctl enable-linger "$USER"   # so it survives logout
journalctl --user -u sieve -f
```

</details>

<details>
<summary>Scheduled maintenance</summary>

The background worker handles syncing and scoring while the server runs. If you
prefer to run the server with `--no-worker` and drive it from cron:

```cron
*/30 * * * *  cd ~/sieve && .venv/bin/sieve sync && .venv/bin/sieve score
0    4 * * *  cd ~/sieve && .venv/bin/sieve affinity && .venv/bin/sieve quality
0    5 * * 0  cd ~/sieve && .venv/bin/sieve prune
```

</details>

---

## Upgrading

```bash
git pull
pip install -e .
sieve doctor
```

Schema changes are applied on open, from an explicit column list in `db.py`, so
upgrading in place does not need a dump and reload. Your database is not
touched otherwise.

One case needs a manual step. If you change `embed_provider`, every stored
vector was produced by the old backend and means nothing to the new one:

```bash
sieve score --all
```

---

## Starting over

**Controls, Danger zone** has two resets, and both write a backup first:

| | Deletes | Keeps |
|---|---|---|
| **Delete all videos** | the catalogue, scores and cached lookups | subscriptions, history, channel settings, interests, playlists, controls, profiles |
| **Factory reset** | everything | nothing |

Both ask you to type `reset`. From the command line:

```bash
sieve reset catalogue
sieve reset everything
```

Backups are written to `backups/` inside the data directory, and the newest
five are kept. To restore one, stop Sieve and copy it over `sieve.db`:

```bash
cp ~/.local/share/sieve/backups/sieve-20260921-224703-123.db ~/.local/share/sieve/sieve.db
```

## Uninstalling

Everything Sieve owns lives in one directory:

```bash
rm -rf ~/.local/share/sieve ~/.config/sieve
pip uninstall sieve
```

There is nothing else — no system files, no registered services you did not
create yourself, and nothing on anyone else's machine.
