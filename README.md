# Authelia Admin

A small, self-hosted web UI for managing an [Authelia](https://www.authelia.com/) instance that uses the
**file user backend** and **SQLite storage**: users, passwords, `configuration.yml`, backups and 2FA resets, from your browser (phone included).

Runs as a single hardened container on amd64 and arm64 (Raspberry Pi friendly), with a distroless runtime image and no pinned Authelia version.

## Features

**Users**
- Create, edit (display name, email, groups, disabled) and delete users in `users_database.yml`
- Set or generate passwords, hashed with argon2id in Authelia's own format (t=3, m=64 MiB, p=4); plain passwords are never stored
- Per-user **Reset 2FA**: removes TOTP and WebAuthn registrations from Authelia's SQLite database through the Authelia CLI
- Search, stats and avatars on the Users page

**Configuration**
- Edit `configuration.yml` in the browser; YAML syntax check plus `authelia validate-config` before saving
- **Show changes**: diff of your edit against the current file before you save
- Optional **Restart Authelia** button (no Docker socket needed, see [Restart button](#restart-authelia-from-the-ui))

**Backups**
- Automatic timestamped backup on every save, restore and delete, grouped by category (Configuration, Users, Database)
- "Back up database now": consistent online SQLite backup, with download and delete
- Backup view shows exactly what a restore would change; old backups are pruned (`MAX_BACKUPS`)

**Security**
- Admin login with CSRF protection, login rate limit (5 attempts, 15 minutes), strict cookies and a strict CSP
- **Passkeys** (WebAuthn) for the admin login, managed on the Security page
- **TOTP** second step for password login, with QR-code setup
- `/healthz` endpoint and a Docker `HEALTHCHECK`

**Interface**
- Sidebar dashboard layout, automatic dark mode with a manual toggle, mobile-friendly

## Requirements

- Docker with Compose
- An Authelia instance using the file backend (`users_database.yml`) and, for Reset 2FA and database backups, SQLite storage
- Access to the **same folder** Authelia uses for its configuration (`configuration.yml`, `users_database.yml`, `db.sqlite3`)
- HTTPS in front of the UI if you want passkeys (browsers only allow them on HTTPS)

## Quick start

1. Clone the repository and generate a session secret and an admin password hash (see [How-to](#how-to)):

   ```bash
   openssl rand -hex 32
   ```

2. Edit `docker-compose.yml`: set the config folder, `user:`, `ADMIN_USER`, `SECRET_KEY` and the password.
3. Build and start:

   ```bash
   docker compose up -d --build
   ```

4. Open `http://127.0.0.1:8080` (or your reverse proxy address) and sign in.

Minimal compose file:

```yaml
services:
  authelia-admin:
    build: .
    image: authelia-admin:latest
    container_name: authelia-admin
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"
    environment:
      ADMIN_USER: admin
      ADMIN_PASSWORD_HASH: '$$argon2id$$v=19$$m=65536,t=3,p=4$$...'   # every $ doubled; see How-to
      SECRET_KEY: "output-of-openssl-rand-hex-32"
      COOKIE_SECURE: "true"          # "false" only for plain-HTTP testing
      TZ: UTC                        # e.g. Europe/London, used for backup file names
    volumes:
      - /path/to/authelia:/config    # the folder Authelia itself uses
    user: "1001:100"                 # a UID:GID that can write that folder
```

The container user must be able to **write** the config folder. Check the owner with `ls -ln /path/to/authelia` and set `user:` to match.

## Environment variables

### Admin login

| Variable | Default | Description |
|---|---|---|
| `ADMIN_USER` | `admin` | Admin username. |
| `ADMIN_PASSWORD` | – | Admin password in plain text. Simple, but the password sits in your compose file. |
| `ADMIN_PASSWORD_HASH` | – | Admin password as an argon2id hash. Takes precedence over `ADMIN_PASSWORD`. In compose YAML every `$` must be written as `$$`. |
| `ADMIN_PASSWORD_HASH_FILE` | – | Path to a file containing the hash (no `$` escaping needed). Overrides `ADMIN_PASSWORD_HASH`. |
| `SECRET_KEY` | random per start | Signs the session cookie. **Set it**, otherwise every restart logs you out. Use a long random value. |
| `COOKIE_SECURE` | `false` | Set to `true` when served over HTTPS. With `true` over plain HTTP, login fails with "Bad CSRF token". |
| `TZ` | `UTC` | Time zone (IANA name) for backup file names and timestamps. |

One of `ADMIN_PASSWORD`, `ADMIN_PASSWORD_HASH` or `ADMIN_PASSWORD_HASH_FILE` is required; the container refuses to start without one. A hash that does not start with `$argon2` also stops the start (usually a sign of missing `$$` escaping).

### Paths

| Variable | Default | Description |
|---|---|---|
| `AUTHELIA_CONFIG_DIR` | `/config` | Where the Authelia folder is mounted inside the container. |
| `AUTHELIA_CONFIG_FILE` | `configuration.yml` | Authelia configuration file name, relative to the config dir. |
| `AUTHELIA_USERS_FILE` | `users_database.yml` | Users file name, relative to the config dir. |
| `AUTHELIA_DB_FILE` | `<config dir>/db.sqlite3` | SQLite file, used only if `storage.local.path` is not set in the configuration. |
| `MAX_BACKUPS` | `30` | Backups kept per file; older ones are deleted. |

Backups are stored in `<config dir>/backups`.

### Reset 2FA and validation

Any other `AUTHELIA_*` variable you set on this container (for example `AUTHELIA_STORAGE_ENCRYPTION_KEY` or `AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE`) is passed to the Authelia CLI. Reset 2FA needs the same storage encryption key Authelia uses, if the key is not written in `configuration.yml`, and `db.sqlite3` reachable at `storage.local.path`. The app's own `AUTHELIA_*` variables above are hidden from the CLI so it does not warn about them.

### Passkeys

| Variable | Default | Description |
|---|---|---|
| `ADMIN_RP_ID` | – | Your site's domain, for example `admin.example.com`. Passkeys are bound to it. |
| `ADMIN_ORIGIN` | – | Full origin, for example `https://admin.example.com`. |
| `ADMIN_RP_NAME` | `Authelia Admin` | Name shown in the passkey prompt and authenticator apps. |
| `PASSKEY_FILE` | `<config dir>/admin_passkeys.json` | Where public keys and counters are stored (private keys never leave your device). |
| `ADMIN_PASSKEY_ONLY` | `false` | `true` turns password login off while at least one passkey is registered. |

Passkeys are enabled when both `ADMIN_RP_ID` and `ADMIN_ORIGIN` are set.

### TOTP

| Variable | Default | Description |
|---|---|---|
| `ADMIN_TOTP_SECRET` | – | Base32 secret. When set, password login also asks for a 6-digit code. Passkey login skips it. |

### Restart button

| Variable | Default | Description |
|---|---|---|
| `RESTART_FLAG_FILE` | – | Enables the Restart button in flag-file mode (recommended): the button creates this file, for example `/config/.restart-request`, and a host service restarts Authelia. |
| `AUTHELIA_CONTAINER` | – | Name of the Authelia container. Enables the Restart button in Docker-socket mode. If both are set, the flag file is used. |

### Reverse proxy

| Variable | Default | Description |
|---|---|---|
| `TRUST_PROXY` | `0` | Number of reverse proxies in front of the app. Makes the login lockout count real client addresses from `X-Forwarded-For`. Enable only if the proxy is the **only** way to reach the container. |

## How-to

### Generate an admin password hash

```bash
read -rs -p "Admin password: " PW; echo
docker run --rm --entrypoint /usr/local/bin/authelia authelia-admin:latest crypto hash generate argon2 --password "$PW" | sed 's/^Digest: //; s/\$/$$/g'; unset PW
```

Run it after `docker compose build` (the image is named `authelia-admin:latest` in the example compose file; adjust if you renamed it). The output already has every `$` doubled; paste it in single quotes as `ADMIN_PASSWORD_HASH`.

To avoid escaping altogether, write the hash (without the `Digest: ` prefix) into a file, mount it, and set `ADMIN_PASSWORD_HASH_FILE`.

### Enable TOTP

1. Sign in, open **Security**, click **Set up authenticator**.
2. Scan the QR code (or type the key), enter the code your app shows, click **Verify code**.
3. Add the shown line to the environment and recreate the container:

   ```yaml
   ADMIN_TOTP_SECRET: "THE-SECRET-SHOWN"
   ```

To turn it off, remove the variable and recreate the container.

### Enable passkeys

1. Serve the UI over HTTPS on a real domain and set `ADMIN_RP_ID` and `ADMIN_ORIGIN` to it. Changing the domain later invalidates existing passkeys.
2. Recreate the container, open **Security**, click **Add passkey** and follow the prompt.
3. Register at least two (for example a phone and a security key).
4. Optional: set `ADMIN_PASSKEY_ONLY: "true"` to switch off password login. This only applies while a passkey exists, so deleting all passkeys never locks you out.

### Restart Authelia from the UI

**Flag file (recommended, no Docker access for the container).** Set `RESTART_FLAG_FILE: /config/.restart-request`. On the host, create a systemd path unit that reacts to the file (adjust the path to your Authelia folder and the container name):

`/etc/systemd/system/authelia-restart.path`

```ini
[Unit]
Description=Watch for Authelia restart requests
[Path]
PathExists=/path/to/authelia/.restart-request
Unit=authelia-restart.service
[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/authelia-restart.service`

```ini
[Unit]
Description=Restart Authelia container
[Service]
Type=oneshot
ExecStartPre=/bin/rm -f /path/to/authelia/.restart-request
ExecStart=/usr/bin/docker restart authelia
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now authelia-restart.path
```

**Docker socket (not recommended).** Set `AUTHELIA_CONTAINER`, mount `/var/run/docker.sock` and add `group_add` with the socket's group id (`stat -c %g /var/run/docker.sock`). This gives the container root-equivalent access to the host.

### Restore from a backup

- **Configuration and users:** Backups page, open the file, review "Changes if restored", click **Restore**. The current file is backed up first.
- **Database:** download the backup, stop Authelia, replace the SQLite file, start Authelia. Database backups contain 2FA secrets (encrypted with your storage encryption key); keep that key separately.

### Put it behind a reverse proxy

Terminate TLS at your proxy and forward to the container (`http://authelia-admin:8080` on a shared Docker network, or `127.0.0.1:8080`). Set `COOKIE_SECURE: "true"`. If the proxy is the only path to the container, you can set `TRUST_PROXY: "1"` for a per-client login lockout.

## Updating

```bash
git pull
docker compose build --pull
docker compose up -d --force-recreate
```

The image compiles the Authelia CLI from the latest `v4.x` release tag at build time (no version is pinned), so a rebuild picks up new Authelia releases and base-image patches. Use `--no-cache` to force a fresh Authelia build. On a Raspberry Pi the first build is slow because of the Go toolchain; later builds reuse the cache.

Multi-architecture build and push:

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t youruser/authelia-admin:latest --push .
```

## Prebuilt image (GitHub Actions)

`.github/workflows/docker.yml` builds the image for amd64 (on an amd64 runner) and arm64 (on a native arm64 runner) and publishes one multi-arch tag to Docker Hub:

- a version tag such as `v1.2.3` publishes `latest`, `1.2.3` and `1.2`
- a weekly run (Mondays) rebuilds `latest`, so new Authelia releases and base-image security patches are picked up without any change to the repository
- the **Run workflow** button on the Actions tab rebuilds on demand

To use it in your own fork, add two repository secrets (Settings > Secrets and variables > Actions): `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token with Read and Write scope). The image is published as `<your user>/authelia-admin`. Then use it in compose instead of building:

```yaml
    image: youruser/authelia-admin:latest   # remove the "build: ." line
```

The free native arm64 runner is available for public repositories. In a private repository, switch the arm64 runner to one you have access to, or use QEMU emulation (much slower).

## Security notes

- This UI can reset any user's password and edit your Authelia configuration. Keep it on a trusted network (LAN or VPN) behind HTTPS; do not expose it directly to the internet.
- Prefer passkeys and/or TOTP, and a strong `SECRET_KEY`.
- The runtime image is Chainguard's distroless Python (no shell, no package manager). The app runs as the non-root user you set with `user:`.
- Authelia's file backend watches `users_database.yml` (`authentication_backend.file.watch: true`), so user changes apply immediately. Configuration changes need an Authelia restart.
- `authelia validate-config` may report secrets that Authelia receives through environment variables or files as missing, because this container does not have them. Use "Save anyway" if that is the only complaint.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "Bad request: Bad CSRF token" | `COOKIE_SECURE: "true"` while using plain HTTP. Use HTTPS or set it to `false`. |
| "Invalid credentials" with a hash | The `$` signs were not doubled (`$$`) in compose. Use the command above or `ADMIN_PASSWORD_HASH_FILE`. |
| Everyone is logged out after a restart | `SECRET_KEY` is not set. |
| `TemplateNotFound` / Internal Server Error | Source folders are not readable by the container user (for example mode `0500`). Run `chmod -R u+rwX,go+rX app` and rebuild. |
| Passkey button missing or fails | Needs `ADMIN_RP_ID` and `ADMIN_ORIGIN`, HTTPS, and the exact domain in the address bar. |
| "Restart failed" | Docker-socket mode without the socket mounted. Use the flag-file mode instead. |
| Reset 2FA fails | `db.sqlite3` not at `storage.local.path`, or the storage encryption key is not available to this container. |

## Project layout

```
Dockerfile            multi-stage build (Authelia CLI, Python deps, distroless runtime)
docker-compose.yml    example deployment
requirements.txt      pinned Python dependencies
app/app.py            Flask application
app/templates/        Jinja templates
app/static/app.js     all browser-side code (the CSP forbids inline scripts)
```

## License

[MIT](LICENSE)
