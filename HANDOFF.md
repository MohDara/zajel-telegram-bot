# Security & Reliability Audit — Zajel Telegram Bot

Audit date: 2026-09-19
Scope: application code (`bot.py`, `db.py`, `formatter.py`, `zajel_client.py`), storage, backups, and deployment configuration.

This document records the security and data-integrity review performed for the public release of this repository. It is published so that any student, developer, or security reviewer can verify how the bot handles their data.

---

## 1. Privacy guarantees

| Guarantee | Implementation |
| --- | --- |
| Password removed from chat immediately | `receive_password()` deletes the Telegram message before any processing (`bot.py`). |
| Passwords never stored in plaintext | Fernet (AES-128-CBC + HMAC-SHA256) encryption at rest (`save_user()` / `get_user()` in `db.py`). |
| No third parties or proxies | Direct HTTPS only to `https://zajeles.najah.edu` and `https://moodle.najah.edu` (`zajel_client.py`). |
| No credential logging | Logs record connection status and errors only; passwords are never logged (`bot.py` logging setup). |
| Session isolation | Academic data is cached in RAM per Telegram user with a 5-minute TTL and pruned after 24h idle. |
| Zero-Knowledge Admin | Student names are never stored in the database. `/users`, `/backup`, and `/delete_user` are completely removed from the codebase so no rosters or student identities can ever be queried or dumped. `/status` provides aggregate counts only. |

Git history verification: the repository contains source, documentation, and configuration templates only. No `.env`, encryption key, database file, backup, or log file has ever been committed. The live secrets were also checked against every object in git history and are not present.

---

## 2. Hardening applied

### 2.1 Encryption key integrity (zero silent data loss)

- `_resolve_fernet_key()` resolves the key in priority order: `APP_SECRET_KEY` environment variable, persistent `.app_secret.key` keyfile, then generation with persistence.
- `_verify_encryption_key()` samples stored credentials at startup. If none of them decrypt with the active key, the bot refuses to start (`RuntimeError`) instead of silently serving unusable data. The only override is the explicit `ALLOW_KEY_RESET=1`.
- The keyfile is written with `0600` permissions on POSIX.

### 2.2 Verified, timestamped backups

- Backups use the SQLite online backup API (consistent snapshot even while the bot is running), followed by `PRAGMA integrity_check`.
- Snapshots are timestamped under `backups/`, and the newest `BACKUP_KEEP` (default 20) are retained.
- `zajel_users.db.bak` is refreshed only from a snapshot that passed the integrity check; a failed backup never replaces a known-good copy.
- Backups run at startup, every 6 hours, and on graceful shutdown.
- In Docker, backups live inside the mounted `/app/data` volume.

### 2.3 Storage durability

- SQLite runs in WAL mode with `synchronous=FULL`, so committed transactions survive power loss.
- Cloud PostgreSQL (`DATABASE_URL`) is supported for deployments with ephemeral filesystems.

### 2.4 PostgreSQL handling

- `DATABASE_URL` parsing strips query parameters (e.g. `?sslmode=require`) so provider URLs connect correctly.
- TLS certificate verification is enabled by default. It is relaxed only for dotless internal hostnames or an explicit `PGSSL_NO_VERIFY=1`, and a warning is logged when it is disabled.

### 2.5 Registration integrity

- A unique index on the student number (`idx_users_username_unique`) closes the check-then-insert race, so one university number cannot be linked to two Telegram accounts.
- `save_user()` failures are handled explicitly; a session is never created if the database write failed.

### 2.6 Abuse and flood protection

- Per-user cooldowns on feature requests, login attempts, and manual refreshes protect both the bot and the university servers.
- Broadcasts handle Telegram `RetryAfter` limits and throttle between recipients.
- Message splitting guarantees no output exceeds Telegram limits and no characters are lost.

### 2.7 Test isolation

- `test_all.py` redirects `DB_PATH`, `APP_KEY_FILE`, and `BACKUP_DIR` to a temporary directory before importing `db`, so tests can never read or write production data.
- Live Zajel portal tests run only when `ZAJEL_USER` and `ZAJEL_PASS` are present in the environment.

### 2.8 Build and deployment hardening

- `.gitignore` excludes `.env`, `.app_secret.key`, `*.key`, databases, WAL/SHM files, backups, and logs.
- `.dockerignore` excludes the same secret and data files, so a local `docker build` cannot bake the encryption key or encrypted database backups into an image.
- The Docker image runs as a non-root user and declares `/app/data` as a persistent volume.
- Dependencies are pinned to the exact tested versions in `requirements.txt` for reproducible builds.

---

## 3. Verification evidence

```
python -m py_compile bot.py db.py formatter.py zajel_client.py test_all.py   # OK
python test_all.py
```

Result (abridged, full pass):

```
Test 1  Passed: Secure database storage, Fernet encryption, duplicate checking
Test 2a Passed: SQLite WAL mode active
Test 2b Passed: Online atomic backup created
Test 2c Passed: Disaster recovery export valid
Test 2d Passed: Multi-threaded concurrency stress test (zero lockups)
Test 3  Passed: Clean transcript typography
Test 4-8 Passed: Live Zajel authentication, Moodle SSO, messages, schedule, transcript
Test 9  Passed: Message splitting never exceeds limits, loses no characters
Test 10 Passed: Verified backup retention active
ALL VERIFICATIONS PASSED SUCCESSFULLY!
```

Encryption key guard simulation (copy of a real database with a random valid Fernet key):

```
ENCRYPTION KEY MISMATCH: N stored credential(s) cannot be decrypted ...
RuntimeError: ... exit code: 1
```

---

## 4. Residual risks and operational notes

- **Server compromise:** the encryption key and the encrypted database live on the same host. An attacker with full server access could decrypt stored credentials. Use `APP_SECRET_KEY` from the hosting provider's secret store and keep an off-site copy.
- **Telegram account security:** a user's Telegram account controls access to that user's data. Telegram chats with the bot are not end-to-end encrypted; stored passwords remain Fernet-encrypted, but live responses are visible to anyone with access to the account.
- **Zero-knowledge roster:** the admin has zero ability to query, list, or export student users or names from Telegram. Only aggregate counts are reported via `/status`.
- **Health-check endpoint:** the optional HTTP server binds `0.0.0.0:PORT` and serves a static string without authentication; it exposes no application data.
- **Logs:** ERROR/WARNING entries may contain Telegram user IDs. They do not contain names, passwords, or academic records.
- **Supply chain:** dependencies are pinned to tested versions; review updates deliberately before bumping.

---

## 5. Deployment checklist

1. Set `APP_SECRET_KEY` in the hosting environment to the same value used when the data was encrypted. Without it, the startup guard refuses to run rather than lose credentials.
2. Store a copy of `APP_SECRET_KEY` separately from the database. Database backups cannot be decrypted without it.
3. Set `ALLOWED_USER_ID` to the administrator's numeric Telegram ID. Leave it unset to disable all admin commands.
4. Verify `DATABASE_URL` connects with TLS verification enabled; use `PGSSL_NO_VERIFY=1` only if the provider genuinely requires it.
5. Before enabling the unique index on an existing database, check for duplicates:
   `SELECT username, COUNT(*) FROM users GROUP BY username HAVING COUNT(*) > 1;`
6. Mount a persistent volume at `/app/data` (Docker) so the database, keyfile, and backups survive restarts.

---

## 6. Reporting a security issue

Please open a private report through GitHub Security Advisories on this repository, or open an issue if the matter is not sensitive. Do not include real credentials or student data in reports.
