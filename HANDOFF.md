# Handoff — Security & Data-Loss Audit (LOCAL ONLY, NOT PUSHED)

Date: 2026-09-19
Auditor: AI coding session (opencode)
Reviewer: next pair of eyes (human or AI) — please read "Reviewer checklist" at the end.

## Status

- **Nothing has been committed or pushed.** All work is uncommitted working-tree changes.
- `git status --short` shows exactly 7 modified tracked files: `.gitignore`, `Dockerfile`, `README.md`, `bot.py`, `db.py`, `formatter.py`, `test_all.py`.
- No new secrets, DB files, or logs are staged; `.gitignore` covers `.env`, `.app_secret.key`, `*.db`, `*.db-wal`, `*.bak`, `*.log`, `backups/`.
- Git history was scanned: only placeholder tokens exist in old README revisions (e.g. `1234567890:ABCDef...`, `ضع_التوكن_هنا`). No real credentials were ever committed.
- `bot.log` was scanned for `password|token|gsw|liun|@`: no credential material found.
- Production SQLite DB (`zajel_users.db`, 1 registered user) is intact and still decrypts successfully after all changes.

## TL;DR of the audit

The codebase is fundamentally clean on privacy (Fernet-encrypted passwords, message deletion, direct HTTPS to `najah.edu`, no third parties). The real risks were **data-loss scenarios around the encryption key and backups**, plus a few robustness/security defects. All critical issues below were fixed and tested.

---

## 1. Critical data-loss fixes

### 1.1 Silent key loss made all stored passwords unrecoverable
- **Before:** if `APP_SECRET_KEY` was missing and `.app_secret.key` was gone (e.g. ephemeral container FS), `_resolve_fernet_key()` silently generated a new key. Every previously encrypted password became permanently undecryptable, with no warning. `get_user()` returned `None` for every user (silent logout/data loss).
- **After:** `db.py:34` `_resolve_fernet_key()` returns `(key, source)`; `db.py:260` `_verify_encryption_key()` samples up to 20 stored rows after schema init and:
  - returns normally if any row decrypts (key is correct),
  - raises `RuntimeError` with `ENCRYPTION KEY MISMATCH: N stored credential(s) cannot be decrypted...` if none decrypt,
  - only continues (with a CRITICAL log) if `ALLOW_KEY_RESET=1` is explicitly set.
- **Called from** `db.py:337` inside `init_db()`, i.e. at import time. A wrong/missing key now causes a **hard startup failure** instead of silent loss.
- **Verified:** wrong-key simulation on a copy of the real DB exits with code 1 and the exact message; `ALLOW_KEY_RESET=1` exits 0.
- **Reviewer note:** this intentionally turns silent data loss into a crash-loop. Confirm that is the desired production behavior.

### 1.2 Backups could overwrite the only good copy with a corrupt/stale one
- **Before:** `backup_sqlite_db()` fell back to `shutil.copy2(DB_PATH, DB_PATH + ".bak")` if the online backup failed. That copy can miss committed data still in `-wal` and could overwrite a good `.bak` with a corrupt DB.
- **After:** `db.py:185` `backup_sqlite_db()`:
  - uses only the SQLite online backup API,
  - runs `PRAGMA integrity_check` on the produced snapshot and deletes it if not `ok`,
  - writes timestamped files `backups/zajel_users_YYYYmmdd_HHMMSS_<micro>.db`,
  - refreshes `zajel_users.db.bak` **only from the verified snapshot** (`shutil.copy2` source is the backup, not the live DB),
  - retains the newest `BACKUP_KEEP` (default 20) snapshots via `db.py:173` `_prune_old_backups()`.
- **Reviewer note:** verify `_prune_old_backups` ordering assumption (lexicographic sort of the timestamp format) and the `keep <= 0` behavior.

### 1.3 Backups only existed at process start
- **Before:** one snapshot on startup only.
- **After:**
  - `bot.py:76` `periodic_backup_loop()` → verified backup every `BACKUP_INTERVAL = 6h` (`bot.py:142`).
  - `bot.py:97` `post_shutdown()` → final backup on graceful shutdown / rolling deploy.
  - `bot.py:92` `post_init()` starts the health server and the backup task.
  - Wired at `bot.py:908` via `.post_init(post_init).post_shutdown(post_shutdown)`.
  - `/status` now reports backup count and latest timestamp via `db.py:248` `get_backup_summary()`.
- **Reviewer note:** `post_shutdown` does not run on SIGKILL/OOM. The 6h periodic loop is the fallback; confirm the interval is acceptable.

### 1.4 Docker backups were on ephemeral storage
- **Before:** default `BACKUP_DIR` was `BASE_DIR/backups` = `/app/backups`, outside the `VOLUME /app/data`.
- **After:** `db.py:166` defaults `BACKUP_DIR` to `<directory of DB_PATH>/backups` → `/app/data/backups`, inside the mounted volume. `BACKUP_DIR` env var still overrides.
- **Dockerfile:** now runs as non-root user `zajel` (UID 10001), `chown -R zajel:zajel /app`, `VOLUME ["/app/data"]`.
- **Reviewer note:** bind-mounting `/app/data` from the host may break permissions for UID 10001; named volumes are fine.

### 1.5 Durability on power loss
- **Before:** `PRAGMA synchronous=NORMAL`.
- **After:** `db.py:134` `PRAGMA synchronous=FULL`. WAL + FULL means committed transactions survive power loss. Cost is small at this traffic level.

---

## 2. Security / correctness fixes

### 2.1 PostgreSQL URL parsing bug (could look like total data loss)
- **Before:** `database = parsed.path.lstrip("/")` → a URL like `.../dbname?sslmode=require` produced database name `dbname?sslmode=require` and the connection failed. Common with Supabase/Render URLs.
- **After:** `db.py:104` strips the query string; `db.py:105-106` parses `sslmode`; `sslmode=disable` disables TLS; localhost disables TLS.

### 2.2 PostgreSQL TLS verification was disabled for Render/Supabase
- **Before:** `check_hostname=False`, `CERT_NONE` for any host containing `render.com` or `supabase` (MITM exposure).
- **After:** `db.py:112-117` verifies certificates by default; verification is relaxed only for:
  - dotless internal hostnames (cannot be verified), or
  - explicit opt-out `PGSSL_NO_VERIFY=1`.
  - A warning is logged when verification is disabled.
- **Reviewer note (highest-risk change):** if the deployed Render/Supabase Postgres presents a certificate that fails default verification, the bot will no longer connect. Test against the real `DATABASE_URL` before deploying; use `PGSSL_NO_VERIFY=1` only if the provider genuinely requires it.

### 2.3 Duplicate university-number registration race
- **Before:** `is_username_registered()` check + insert had a TOCTOU window; two Telegram accounts could register the same student number.
- **After:**
  - `db.py:325-332` creates `CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique ON users(username)` (best-effort; failure is logged if pre-existing duplicates block it).
  - `bot.py:333-348` wraps `db.save_user` and returns a clear Arabic "already registered" message on `unique/integrity/duplicate` errors, and **does not create the in-memory session** if the DB write failed.
- **Reviewer note:** if the index creation fails on existing duplicate data, behavior falls back to the application-level check only. Verify production has no duplicates (SQL below).

### 2.4 Login attempts were not rate-limited
- **Before:** only feature commands were throttled; password attempts could hammer the university server.
- **After:** `bot.py:309-312` applies `check_rate_limit()` before each login attempt; the password message is still deleted first (`bot.py:303-307`).

### 2.5 Broadcast could trip Telegram flood limits and partially fail silently
- **Before:** tight loop, no delay, generic failure count.
- **After:** `bot.py:734-750` handles `RetryAfter` (sleeps `retry_after + 1s` and retries once) and sleeps `0.05s` between recipients.

### 2.6 Oversized messages could be rejected and lost
- **Before:** `formatter.split_message` could emit a chunk longer than the limit if a single line was too long, and could emit an empty first chunk.
- **After:** `formatter.py:6` hard-splits individual long lines and never emits empty/oversized chunks. Covered by test 9 (9000-char line round-trips with no lost characters).

### 2.7 Tests wrote to the production database
- **Before:** `test_all.py` imported `db` with the real `DB_PATH` and wrote/deleted test rows; it also triggered a startup backup of production.
- **After:** `test_all.py:16-21` sets `DB_PATH`, `APP_KEY_FILE`, `BACKUP_DIR` to a fresh `tempfile.mkdtemp()` and forces `DATABASE_URL=""` before importing `db`, then removes the temp dir at the end. Tests can no longer touch real data.
- **Reviewer note:** the suite still performs **live** Zajel portal calls when `ZAJEL_USER`/`ZAJEL_PASS` are present in `.env` (tests 4–8). Do not run it with production credentials unless that is intended.

### 2.8 Minor hardening
- `db.py:24-31` keyfile written with `0600` on POSIX.
- `bot.py:151` `prune_user_sessions()` evicts sessions idle > 24h (`SESSION_MAX_IDLE`, `bot.py:141`), preventing unbounded memory growth.
- `/backup` captions now warn that the original `APP_SECRET_KEY` is required to restore (`bot.py` backup_command).
- `README.md` updated to match the new backup/key behavior.

---

## 3. Verification evidence

Commands run from the repo root:

```
python -m py_compile bot.py db.py formatter.py zajel_client.py test_all.py   # OK
python test_all.py
```

Result (abridged, full pass):

```
Test 1 Passed: Secure database storage, Fernet encryption, duplicate checking
Test 2a Passed: SQLite WAL mode active
Test 2b Passed: Online atomic backup created (<temp>/backups/zajel_users_...db)
Test 2c Passed: Disaster recovery export valid (0 registered users)
Test 2d Passed: Multi-threaded concurrency stress test (zero lockups)
Test 3 Passed: Clean transcript typography
Test 4 Passed: 5-step Tomcat authentication (live)
Test 5 Passed: Moodle SSO redirect token generated (live)
Test 6 Passed: Important messages queried (4) (live)
Test 7 Passed: Full schedule retrieved (6 courses) (live)
Test 8 Passed: Full transcript (6 semesters, 108 credits) (live)
Test 9 Passed: Message splitting never exceeds limits, loses no characters
Test 10 Passed: Verified backup retention active (2 snapshots)
ALL VERIFICATIONS PASSED SUCCESSFULLY!
```

Key-guard simulation (copy of real DB + random valid Fernet key):

```
ENCRYPTION KEY MISMATCH: 1 stored credential(s) cannot be decrypted ...
RuntimeError: ... exit code: 1
```

Production DB after all changes: `users: 1`, `prod decrypt ok: True`, backups present.

---

## 4. Reviewer checklist (please double-check these specifically)

1. **Key guard correctness** (`db.py:260-296`)
   - Samples only the first 20 rows. A DB with mixed keys (some rows decrypt, some don't) passes the guard. Is that acceptable, or should it check every row / warn per row?
   - `RuntimeError` at import time crashes the process. Confirm this is preferred over serving users with an error message.
   - Is the `ALLOW_KEY_RESET` escape hatch too easy to set accidentally?
2. **PostgreSQL TLS change** (`db.py:94-127`) — the highest deployment risk. Test against the real `DATABASE_URL`. Verify `?sslmode=...` and any other query params (`pgbouncer=true`, `connect_timeout`) are handled or safely ignored.
3. **Backup semantics** (`db.py:185-245`)
   - Confirm `PRAGMA integrity_check` on the destination is a sufficient guarantee.
   - Confirm retention ordering and that a failed backup never deletes existing backups.
   - Confirm the `synchronous=FULL` performance cost is acceptable for the expected concurrency.
4. **Unique index migration** (`db.py:325-332`)
   - Check production for duplicates before deploy:
     `SELECT username, COUNT(*) FROM users GROUP BY username HAVING COUNT(*) > 1;`
   - Confirm the index actually gets created on the active engine (SQLite and PostgreSQL syntax both support `IF NOT EXISTS` here).
5. **Startup/backup lifecycle** (`bot.py:76-104`, `bot.py:908`)
   - Verify `Application.create_task` behavior in the pinned PTB version (requirements say `>=21.0`; local is 22.8). Consider pinning dependencies.
   - Confirm `post_shutdown` is invoked on your host's stop signal.
6. **Docker** — verify the non-root user can write `/app/data` (named volume) and `/app/data/backups`.
7. **Tests** — confirm the temp-DB isolation actually holds when `.env` contains `DATABASE_URL` (the suite forces it to empty before `load_dotenv`). Confirm whether live tests should be gated behind an env flag.
8. **Arabic copy** — the new/edited user-facing strings in `bot.py` (duplicate registration, save failure) should be reviewed by a native speaker.

## 5. Pre-deploy prerequisites (must-do)

1. Set `APP_SECRET_KEY` in the hosting environment to the **same** value as the local `.env`. Without it, the new guard will refuse to start (by design) rather than silently lose credentials.
2. Back up `APP_SECRET_KEY` somewhere separate from the database. Database backups are useless without it.
3. Verify `DATABASE_URL` connects with the new TLS verification (see checklist item 2).
4. Check for duplicate usernames (checklist item 4).

## 6. Known residual risks (not changed in this pass)

- No dependency pinning (`requirements.txt` uses `>=`); supply-chain/reproducibility risk.
- PostgreSQL opens a new connection per query (no pooling); performance, not data loss.
- `delete_user_by_identifier()` treats a numeric argument as `telegram_id` first, then as a student number. Ambiguous for 9–10 digit student numbers (student numbers are >= 6 digits). Documented behavior, unchanged.
- Admin `/users` intentionally exposes the roster to `ALLOWED_USER_ID` only.
- Health-check HTTP server binds `0.0.0.0:PORT` and serves static text without auth; informational only.
- Logs contain Telegram user IDs (not names or credentials) at ERROR/WARNING level.

## 7. Suggested commit split (when approved)

1. `security: fail fast on encryption key mismatch + harden PG TLS/URL handling`
2. `fix: verified timestamped backups, periodic/shutdown snapshots, FULL sync`
3. `fix: unique username index, login rate limit, broadcast throttle, message split`
4. `test: isolate suite from production DB; docs: update README + handoff`
