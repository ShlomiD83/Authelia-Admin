# Changelog

All notable changes to Authelia Admin. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Images are tagged by build date and time (for example `2026.11.20-1408`) rather than version numbers, so
entries are headed by date. Changes not yet in a published image are listed under **Unreleased**.

## Unreleased

### Changed
- Saving a user, or setting a user's password, returns to the Users list (with a message naming the
  user). A rejected password keeps you on the user's page to try again.
- The dashboard's Authelia and configuration status refresh every 15 seconds while the page is open
  (paused in a background tab), and the Authelia card follows a restart live (down, then up).
- Messages are now toasts that float over the page (top right; at the bottom on phones) instead of
  pushing it down. Success messages leave after about 6 seconds (hovering pauses that); error messages stay
  and have a × to dismiss them. The restart status is a toast too: it leaves 10 seconds after Authelia is
  back up, and stays with a × if the restart had a problem. Notices about something that still needs
  fixing (for example untrusted passkeys) stay in the page.

## 2026-11-20 (image `2026.11.20-1327`)

### Fixed
- Messages containing code labels (such as `SECRET_KEY` on the Security page) were broken into columns;
  they now read as one paragraph.

## 2026-11-20 (image `2026.11.20-1319`)

### Security
- **Signed passkeys**: each entry in `admin_passkeys.json` is signed with a key derived from `SECRET_KEY`.
  A passkey added or changed directly in the file (for example by someone with write access to the
  Authelia folder) can't be used to sign in; it shows as "not trusted" on the Security page, where a
  signed-in admin can trust or remove it. Requires a fixed `SECRET_KEY`; the Security page warns when it
  isn't set. Password sign-in stays available while no trusted passkey exists, even in passkey-only mode.

### Upgrade note
- Passkeys registered before this version are unsigned, so they show as "not trusted" after updating.
  Sign in with your password (and TOTP), check each passkey's name and date on the Security page, and
  click **Trust**.

## 2026-11-20 (image `2026.11.20-1307`)

### Added
- **Dashboard** as the home page (the logo and the new first sidebar item lead there): whether Authelia
  is up and the configuration passes validation, when the configuration last changed and whether that was
  through this app, lockouts, failed sign-ins and users without 2FA, user counts, the latest backups, and
  the most recent sign-ins and admin actions. The Users list moved to `/users`.
- A warning on the dashboard and the Activity page when the admin action log can't be written, instead of
  failing silently.

### Changed
- The Activity page shows sign-ins and admin actions from the last 30 days (set `ACTIVITY_DAYS` to change
  it) instead of the latest 200 entries; at most 2,000 rows per list.

## 2026-11-20 (image `2026.11.20-1247`)

### Fixed
- Sign-in and lockout times on the Activity and Users pages were 2 hours ahead (by the offset of
  Authelia's time zone) when Authelia's container has `TZ` set: the time-zone offset Authelia stores with
  each time is now taken into account.

## 2026-11-20 (image `2026.11.20-1234`)

### Added
- **Code editor for `configuration.yml`** (CodeMirror): YAML highlighting, search (Ctrl+F), folding, and
  switches for line numbers, line wrapping and indent guides, remembered per browser.
- **Problems marked on the line itself**: YAML syntax errors while you type, and Authelia's
  `validate-config` errors and warnings mapped to the line they refer to after Validate or Save. A
  problems counter opens the full list.
- **Autocomplete and hover docs from Authelia's own configuration schema**, copied from the same Authelia
  release that is built into the image: option names, allowed values, defaults and descriptions, plus live
  warnings for unknown options and invalid values. Ctrl+Space shows suggestions.
- **Ctrl+S** runs Validate & save.
- **Unsaved edits are kept in the browser**, so they survive an expired session or a closed tab; the
  editor offers to restore them.
- **"Modified" marker** and a "Leave site?" prompt while edits are unsaved.
- **Protection against changes made elsewhere**: if `configuration.yml` changed on disk (for example over
  SSH) after the page was opened, Save stops, shows the difference, and lets you load the current file or
  overwrite it (with a backup).
- **Activity page**: recent sign-ins from Authelia's database (time, user, result, step, IP), counts for
  the last 24 hours, and who is locked out now (users and IPs) with an **Unban** button.
- **Users page** shows each user's 2FA methods (TOTP, number of passkeys), last sign-in, and a "locked
  out" tag.
- **Notice while editing** when `configuration.yml` is changed elsewhere, checked on opening, every 20
  seconds and when you return to the tab, with "Show difference" and "Load the current file".
- **Hide secrets on screen**: values such as `jwt_secret`, `encryption_key`, `secret`, client secrets and
  private keys are blurred in the editor and shown while the cursor is in them; a switch turns this off.
- **Open a configuration backup in the editor**: loads that version as unsaved changes, so it can be
  reviewed, adjusted and saved with the usual validation ("Restore now" still writes it back at once).
- **Admin action log** (`admin_audit.log` in the Authelia folder, shown on the Activity page): admin
  sign-ins and failed attempts, user changes (with what changed), passwords set, 2FA resets, configuration
  saves (lines changed, backup taken, overwrites), backup restores and deletions, restarts and unbans, with
  time, IP and sign-in method. Capped at about 1 MB plus one older file.
- **Live restart status**: after Restart, the page reports when the host picked up the request and, with
  the new optional `AUTHELIA_URL` variable, when Authelia is back up, or that it didn't come back.

### Changed
- An expired session or out-of-date page now shows a clear message instead of "Bad Request: Bad CSRF
  token".
- 2FA reset messages are short: the Authelia CLI's help text is removed, "nothing registered" no longer
  counts as a failure, and an Authelia database without tables gets a plain explanation.
- The output of Authelia's validation no longer includes the CLI's usage text.
- Dependabot also watches the editor's npm packages; patch and minor updates are merged and published
  automatically.

### Fixed
- Two backups of the same file taken within the same second no longer overwrite each other (the second
  gets a `-2` suffix).
- Two changes saved at the same moment (for example from two tabs) no longer undo each other; changes are
  now applied one at a time.
- A file that can't be read or written (for example a backups folder the container's user can't write)
  no longer causes "Internal Server Error": nothing is changed, the page explains which file and how to fix
  the permissions, and configuration edits are kept for restore.

## 2026-11-19

### Added
- Major Dependabot updates are labelled `major-update`, and the repository owner is mentioned so GitHub
  sends a notification.

## 2026-11-18

### Added
- Weekly CVE check (Monday 02:30 Israel time): scans the published image with Docker Scout and rebuilds
  and publishes it when fixable vulnerabilities are found.
- Dependabot for Python packages and GitHub Actions; patch and minor updates are merged automatically
  after a test build, and Python updates are published as a new image.
- Every image build gets its own permanent date-and-time tag in addition to `latest`.

### Changed
- GitHub Actions and Python dependencies updated to their latest versions (gunicorn 27, tzdata 2026.6,
  cbor2 6.3).

## 2026-11-12

### Added
- Shield logo in the README and the web UI.

## 2026-11-11

First public version.

### Added
- Web UI to manage Authelia's file user backend: add, edit, disable and delete users, set passwords
  (argon2id, compatible with Authelia), and reset a user's 2FA (TOTP and WebAuthn).
- `configuration.yml` editor with a YAML check and `authelia validate-config` before saving, and a diff of
  the changes.
- Automatic backups before every change, grouped by file, with view, diff and restore.
- Admin login with password, optional TOTP second step and passkeys, login lockout and CSRF protection.
- Optional Restart Authelia button, using a flag file and a host systemd unit (no Docker socket needed),
  or the Docker socket.
- Light and dark modern dashboard, `TZ` support for backup names, and a health check.
- Multi-arch image (amd64 and arm64) on Docker Hub, built on demand, on a distroless Chainguard base; the
  Docker Hub description is synced from the README.
