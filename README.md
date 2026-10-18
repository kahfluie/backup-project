# Scheduled folder backups over SCP

Run the **sender** on the machine holding your folders and the **receiver** on the target server, or run just the sender for local backups with `SEND_TO_REMOTE=false`. The scripts use Python's standard library and OpenSSH.

| Setting | Default | Where to change it |
| --- | --- | --- |
| Backup interval | Every 12 hours per folder | `.env`: `FOLDER1_INTERVAL_HOURS`, etc.; JSON fallback: `interval_hours` |
| Local retention | Last 3 backups per folder | `.env`: `FOLDER1_KEEP`, etc.; JSON fallback: `keep` |
| Send to remote | Enabled | `.env`: `SEND_TO_REMOTE=false` keeps all backups locally |
| Target retention | Last 3 backups per folder, plus all copies accepted in the last 7 days | Receiver `default_keep`, folder `keep`, and `minimum_retention_hours` |
| Upload limit | 1024 KiB/s (1 MiB/s, roughly 8.4 Mbit/s) | `.env`: `FOLDER1_BANDWIDTH_KIB_PER_SECOND`, etc.; JSON fallback: `remote.bandwidth_kib_per_second` |
| Target cleanup interval | Every 24 hours | Receiver `check_interval_hours` |
| Failed upload retry | Every 300 seconds | Sender `defaults.retry_seconds` or folder `retry_seconds` |

Each backup is a full `.tar.gz`, containing the selected folder as its top-level directory. Files use UTC timestamps and unique IDs, for example `folder1/backup-20261017T120000123456Z-a12b34c56d78.tar.gz`.

The filename records the **date and time when archive creation starts**, using `YYYYMMDDTHHMMSSffffffZ`: `20261017T120000123456Z` means **2026-10-17 at 12:00:00.123456 UTC**. `Z` indicates UTC, and the final 12-character ID prevents filename collisions. Uploads retain the same filename on the receiver; retrying an upload keeps the original backup timestamp.

## 1. Prepare the sender

Install Docker Engine with the Compose plugin, or Docker Desktop using **Linux containers**. Copy this project to both machines. Run commands from the project directory.

The Compose files use the published Docker Hub images `kahfluie/backupkit-sender:latest` and `kahfluie/backupkit-receiver:latest` (Linux amd64).

On Linux/macOS:

```sh
cp config/sender.example.json config/sender.json
cp .env.example .env
mkdir -p secrets data/local-backups
```

On Windows PowerShell:

```powershell
Copy-Item config/sender.example.json config/sender.json
Copy-Item .env.example .env
New-Item -ItemType Directory -Force secrets, data/local-backups | Out-Null
```

### Keep backups locally only

Set this in the sender's `.env` (or `.env.mysql` for the MySQL variant):

```dotenv
SEND_TO_REMOTE=false
```

Configure your source folders and `LOCAL_BACKUP_PATH` as described below, then use the sender launcher in [step 3](#3-start-the-sender). The launchers omit the SSH secret mounts in this mode, so you can skip SSH key generation, receiver setup, and host-key verification. Local archives, schedules, and retention still work. This setting disables uploads for every folder, including MySQL and folders with `remote.enabled=true` in JSON.

When running these source changes before an updated image is published, build the sender with `docker build -t kahfluie/backupkit-sender:latest -f sender/Dockerfile .`, then start it without running `pull` over that local build.

Set `SEND_TO_REMOTE=true` (the default) to allow uploads according to your JSON remote settings, then configure the receiver and SSH files before recreating the sender. The value accepts `true` or `false`, ignoring case and surrounding whitespace; an empty or omitted value uses the default. When switching to local backups, an archive pending from a failed upload is completed locally on its next run and becomes subject to local retention; it is not queued for a later upload.

### Generate and install the transfer SSH key

The sender authenticates with an SSH key. No transfer password needs to be set in `.env`; the supplied receiver disables password login.

1. **Generate the key on the sender.** From the project directory, run this command in PowerShell or a Linux/macOS terminal:

   ```sh
   ssh-keygen -t ed25519 -C "docker-folder-backup" -f secrets/backup_ed25519
   ```

   At `Enter passphrase`, press **Enter** without typing anything. At `Enter same passphrase again`, press **Enter** again. This lets scheduled backups authenticate without a prompt. Generate the key once and reuse it for subsequent container restarts.

2. **Identify the two generated files.**

   | File | Location | Purpose |
   | --- | --- | --- |
   | `secrets/backup_ed25519` | Sender only | Private key used to authenticate transfers; keep it private. |
   | `secrets/backup_ed25519.pub` | Sender; copy to receiver | Public key that authorizes the sender. |

   On Linux/macOS, restrict access to the private key:

   ```sh
   chmod 600 secrets/backup_ed25519
   ```

   The sender container makes its own private copy with the required permissions, including when using Windows bind mounts.

3. **Install the public key on the receiver host.** Use your existing trusted connection or file-transfer method to copy **only** `backup_ed25519.pub` into the receiver project's directory. On a new receiver, install it as follows, from that project directory.

   Linux/macOS:

   ```sh
   mkdir -p secrets
   cp backup_ed25519.pub secrets/authorized_keys
   ```

   Windows PowerShell:

   ```powershell
   New-Item -ItemType Directory -Force secrets | Out-Null
   Copy-Item backup_ed25519.pub secrets/authorized_keys
   ```

   Alternatively, display the public key on the sender using `cat secrets/backup_ed25519.pub` or PowerShell's `Get-Content secrets/backup_ed25519.pub`, then paste the complete line into the receiver's `secrets/authorized_keys` as plain UTF-8 or ASCII text. The line begins with `ssh-ed25519`. Use a separate account and key file for each sender, as described below. Multiple keys in one file grant access to the same identity; use this only for key rotation on one machine. Supply bare public key lines without `authorized_keys` options, and never reuse a key across identities.

4. **Start the receiver and trust its host key.** Follow [step 2 below](#2-start-the-receiver-on-the-target-server) to start the receiver and create `secrets/known_hosts` on the sender. This is a separate file: it lets the sender verify the target server's identity. If you change `authorized_keys` after the receiver is already running, reload it with:

   ```sh
   docker compose -f compose.receiver.yml up -d --force-recreate
   ```

For remote transfers, the sender must have `secrets/backup_ed25519` and `secrets/known_hosts`, and the receiver must have `secrets/authorized_keys`. Compose mounts these files automatically. The private key does not go on the receiver.

### Select the source folders

Edit `.env` to select each host directory and how many backups to keep **locally**. The folders are called `folder1`, `folder2`, and `folder3`; their contents can be anything. Windows example:

```dotenv
FOLDER1_PATH=D:/Documents
FOLDER1_KEEP=3

FOLDER2_PATH=D:/Photos
FOLDER2_KEEP=5

FOLDER3_PATH=none
FOLDER3_KEEP=3

LOCAL_BACKUP_PATH=D:/Backups
```

The supplied Compose file connects these paths to `/sources/folder1`, `/sources/folder2`, and `/sources/folder3` read-only, and passes the keep counts to the backup script. You can configure the three folders by editing just `.env`; you do not need to repeat their host paths or keep counts in JSON. Enabled paths must exist. Keep backups outside the source folders.

Set any folder's path to `none` to skip it, as shown for folder 3 above. Use the sender launcher described in step 3: it removes disabled bind mounts before starting Docker, so no directory named `none` is required or created. The sender skips that folder's backups, uploads, and local cleanup, including pending-upload retries. Its existing local archives and pending state stay in place. Target retention continues independently. `none` is case-insensitive; a real path such as `./none` is still treated as a folder. Empty values use the sample-path fallback, so use `none` explicitly to disable a folder. If all paths are `none`, the sender stays idle and `--once` succeeds without backing up anything.

To add a folder, add its mount, JSON entry, and environment settings following the existing examples. To re-enable a folder, replace `none` with its actual host path and recreate the sender.

For remote transfers, edit `config/sender.json` and set `remote.host` to the target server's address. Configure the port and other connection settings as needed. Optional `.env` settings customize each folder's schedule and upload limit, such as `FOLDER1_INTERVAL_HOURS=12` and `FOLDER1_BANDWIDTH_KIB_PER_SECOND=1024`.

## 2. Start the receiver on the target server

Use `.env.receiver.example` for the target server's environment settings, and `config/receiver.example.json` for its retention policy and sender allowlist. The examples keep at least 3 backups for `folder1`, 5 for `folder2`, and 3 for `folder3`, checking at startup and every 24 hours. All copies accepted within the last seven days are also retained, even above those counts. Each folder must be assigned to exactly one sender; other assigned folder IDs use the default count of 3.

On Linux/macOS:

```sh
cp config/receiver.example.json config/receiver.json
cp .env.receiver.example .env
mkdir -p secrets data/remote-backups
# Place the sender's public key in secrets/authorized_keys before starting.
docker compose -f compose.receiver.yml pull
docker compose -f compose.receiver.yml up -d
docker compose -f compose.receiver.yml logs --tail 50
```

On Windows PowerShell:

```powershell
Copy-Item config/receiver.example.json config/receiver.json
Copy-Item .env.receiver.example .env
New-Item -ItemType Directory -Force secrets, data/remote-backups | Out-Null
# Place the sender's public key in secrets/authorized_keys before starting.
docker compose -f compose.receiver.yml pull
docker compose -f compose.receiver.yml up -d
docker compose -f compose.receiver.yml logs --tail 50
```

For example, this receiver `.env` stores the archives under `/srv/backup-storage` on a Linux target:

```dotenv
REMOTE_BACKUP_PATH=/srv/backup-storage
SSH_BIND_ADDRESS=0.0.0.0
SSH_PORT=2222
```

Accepted files then appear in `/srv/backup-storage/folder1`, `/srv/backup-storage/folder2`, and so on. For Docker Desktop on Windows, a host path such as `D:/Backups` works instead. Leave the sender's `remote.directory` as `/uploads` and `receipts_directory` as `/receipts`. These paths are inside its private SSH jail, separate from the protected archive storage. No sender `FOLDERn_PATH` settings are required on the receiver.

Set `REMOTE_BACKUP_PATH` in the target's `.env` to the desired storage location. Allow TCP port `2222` from the sender; change `SSH_PORT` in `.env` and the sender's `remote.port` together if needed. `SSH_BIND_ADDRESS` can restrict the listening interface.

The example permits key authentication for the `backup` identity. Each configured identity gets its own SSH chroot at `/srv/inboxes/<user>`, containing `/uploads/<folder>` and read-only `/receipts/<folder>`. The account can write only its assigned inbox folders. Interactive shells, forwarding, symlinks, and hard links over SFTP are disabled. Protected archives are mounted at `/backups` **outside every chroot**. That mount root and its configured folder directories belong to root with mode 0700; use a dedicated directory. No accepted archive is served to an SSH sender, including the machine that created it. Restore through a trusted receiver administrator.

The root receiver process polls inboxes every two seconds. After checking the upload manifest's size and SHA-256 digest, it copies bytes into a new receiver-owned inode, fsyncs it, publishes without overwriting an existing archive, then writes a receipt. Moving or changing ownership of the uploaded inode would leave open sender handles able to alter it, so publication always makes a separate copy. The sender checks the receipt's folder, filename, size, and hash before marking success or pruning local copies. Receipts confirm stored bytes; they do not prove an application-consistent or trustworthy backup from a compromised source.

### Multiple sender identities

Keep `backup` for the first machine. For another machine, choose an account such as `backup_office`, generate a different key, and add its public key as a separate Compose secret:

```yaml
# Add under services.receiver.secrets:
      - office_authorized_keys
# Add under top-level secrets:
  office_authorized_keys:
    file: ./secrets/office_authorized_keys
```

Add this entry under the receiver JSON's `senders` object:

```json
"backup_office": {
  "authorized_keys_file": "/run/secrets/office_authorized_keys",
  "folders": ["office_documents", "office_photos"]
}
```

On that machine, set `remote.user` to `backup_office` and use those folder IDs in its sender configuration. Account names are `backup` or `backup_<id>`, with lowercase letters, digits, and underscores in the suffix (up to 24 characters). Folder assignments and key files must be distinct. The receiver rejects reuse of a public key across accounts. Removing an identity from `senders` and recreating the receiver revokes its SSH access. Changing an account's allowlist stops acceptance from removed folders; old staging files may remain in the inbox volume for administrator cleanup.

### Upgrade from the shared writable storage version

This protocol requires upgrading **both** images and configurations. Legacy sender `/backups` destinations and receiver configurations without a sender allowlist fail validation.

1. Stop scheduled senders, then stop the old receiver to close all old SSH sessions. Preserve a trusted snapshot of the current archive directory and keep the host-key volume.
2. On the receiver, replace its Compose file with this version: the same `REMOTE_BACKUP_PATH` now mounts at `/backups`, and a new named `sender_inboxes` volume mounts at `/srv/inboxes`. Set receiver `backup_dir` to `/backups` and add the `senders` entries from the example. Existing host archive directories and filenames stay in place. Assign each existing folder ID to one identity; if machines previously shared an ID, the administrator must separate their history and assign unique IDs before enabling them.
3. On each sender, change `remote.directory` to `/uploads`, set `receipts_directory` to `/receipts`, and configure its assigned SSH user. Keep its local archives and pending state: retries can publish an existing pending archive using the new protocol.
4. Build the updated code on each relevant host, then recreate the receiver before restarting senders. From the project root:

   ```sh
   docker build -t kahfluie/backupkit-receiver:latest -f receiver/Dockerfile .
   docker build -t kahfluie/backupkit-sender:latest -f sender/Dockerfile .
   ```

   Receiver: `docker compose -f compose.receiver.yml up -d --force-recreate`. Windows sender: `.\sender.ps1 up -d --force-recreate`; Python launcher: `python3 sender-compose.py up -d --force-recreate`. Build only the image needed on each host. Do not pull older published images over the local builds; source changes do not automatically update Docker Hub.

Existing archives retain their previous mtimes for retention; new copies use receiver-controlled acceptance times. Minimum retention is an age floor, not an absolute storage limit. Size storage for this window, monitor free space, and use filesystem quotas or a dedicated filesystem to contain inbox flooding. Host administrators and receiver root can still delete data; use independent immutable snapshots or object-lock storage to cover receiver compromise.

Host keys are generated at first startup and persisted in a named volume. Retrieve the **public** host key directly on the target:

```sh
docker compose -f compose.receiver.yml exec -T receiver cat /host-keys/ssh_host_ed25519_key.pub
```

On the sender, create `secrets/known_hosts` with the hostname/port configured in `sender.json` and the public key printed by that command:

```text
[backup.example.com]:2222 ssh-ed25519 AAAAC3...replace-with-the-actual-public-key...
```

For port 22, use `backup.example.com` without brackets or a port suffix. Retrieve the key through your trusted target connection. Host key checking is mandatory. Do not delete the receiver's host-key volume during normal upgrades.

## 3. Start the sender

Use the sender launcher for sender commands so folders with `PATH=none` have their mounts omitted and `SEND_TO_REMOTE=false` omits SSH secrets. PowerShell needs only Docker and Compose; the cross-platform Python launcher also requires Python 3.12+ on the host. The receiver commands remain ordinary `docker compose` commands.

Windows PowerShell:

```powershell
.\sender.ps1 pull
.\sender.ps1 run --rm sender --check-config
.\sender.ps1 up -d
.\sender.ps1 logs -f
```

Linux/macOS (or Windows with Python installed):

```sh
python3 sender-compose.py pull
python3 sender-compose.py run --rm sender --check-config
python3 sender-compose.py up -d
python3 sender-compose.py logs -f
```

The launchers accept Compose commands such as `up`, `stop`, `logs`, `run`, and `down`. To select another environment file, use `.\sender.ps1 -EnvFile .env.other up -d` or `python3 sender-compose.py --env-file .env.other up -d`. Calling the base `compose.sender.yml` directly does not remove disabled mounts or SSH secrets.

The first backup runs immediately. Each folder's next backup is due its configured interval after the previous successful backup/upload completes. Schedule state is persisted beside the local archives, so restarting the container preserves the schedule. Overdue folders run when the sender next starts. Jobs and uploads run sequentially, keeping transfers within one container's speed cap; a long-running job can delay other folders.

For an immediate manual backup, stop the scheduled sender before running the one-shot command:

```powershell
.\sender.ps1 stop sender
.\sender.ps1 run --rm sender --once
.\sender.ps1 up -d
```

On Linux/macOS, replace `.\sender.ps1` with `python3 sender-compose.py` in these commands.

`--once` returns a nonzero exit status if any folder fails. Both scripts prevent concurrent writers against the same state directory.

## Optional sender with host MySQL/MariaDB backups

For Portainer's web editor, use [the Portainer MySQL stack](#portainer-mysql-stack-local-backups) below. The launcher-based example in this section requires the project files on the Docker host.

`compose.sender.mysql.yml` defines both the sender and `databack/mysql-backup:1.5.0` in one Compose file. It does not use `extends` or require `compose.sender.yml`. The database backup service connects to MySQL/MariaDB running on the Docker host, dumps immediately and then every 12 hours, and lets the sender archive the completed dumps under folder ID `mysql`. This is for MySQL/MariaDB. Microsoft SQL Server requires a different backup tool.

The MySQL environment example uses `SEND_TO_REMOTE=false`, so sender archives stay in the `mysql` subdirectory of `LOCAL_BACKUP_PATH` (`./data/local-backups/mysql` by default). It needs no receiver, SSH keys, or `known_hosts`. The launcher removes the SSH secret mounts automatically. Pull the published sender image to get local-only support.

PowerShell:

```powershell
Copy-Item .env.sender.mysql.example .env.mysql
Copy-Item config/sender.mysql.example.json config/sender.mysql.json
# Edit .env.mysql: set MYSQL_HOST, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASES,
# and LOCAL_BACKUP_PATH. Leave SEND_TO_REMOTE=false for local backups.
.\sender.ps1 -EnvFile .env.mysql -ComposeFile compose.sender.mysql.yml pull
.\sender.ps1 -EnvFile .env.mysql -ComposeFile compose.sender.mysql.yml up -d
.\sender.ps1 -EnvFile .env.mysql -ComposeFile compose.sender.mysql.yml logs -f
```

Linux/macOS:

```sh
cp .env.sender.mysql.example .env.mysql
cp config/sender.mysql.example.json config/sender.mysql.json
# Edit .env.mysql with your database connection and local backup path.
# Leave SEND_TO_REMOTE=false for local backups.
python3 sender-compose.py --env-file .env.mysql --compose-file compose.sender.mysql.yml pull
python3 sender-compose.py --env-file .env.mysql --compose-file compose.sender.mysql.yml up -d
python3 sender-compose.py --env-file .env.mysql --compose-file compose.sender.mysql.yml logs -f
```

For remote copies, set `SEND_TO_REMOTE=true` in `.env.mysql`, configure `remote.host` and `remote.port` in `config/sender.mysql.json`, and prepare the receiver, sender SSH key, and verified `known_hosts` as described above. Include `mysql` in the receiver identity's allowed folders, then recreate the sender with the same launcher options and `up -d --force-recreate`. This enables uploads for all configured folders according to their JSON remote settings.

Use these launcher options for subsequent commands so `FOLDERn_PATH=none` removes unused source mounts and `SEND_TO_REMOTE=false` omits SSH secrets. The MySQL environment example disables the three ordinary folders; set their paths to include normal folder backups too. This variant uses the same Compose project and local backup directory as the ordinary sender, so run one sender variant at a time.

| Setting in `.env.mysql` | Default / purpose |
| --- | --- |
| `SEND_TO_REMOTE` | `false` in this example; set `true` to upload to a configured receiver |
| `LOCAL_BACKUP_PATH` | `./data/local-backups`; MySQL sender archives appear in its `mysql` subdirectory |
| `MYSQL_HOST`, `MYSQL_PORT` | `host.docker.internal`, `3306`; the host database address |
| `MYSQL_USER`, `MYSQL_PASSWORD` | Your database backup account and password |
| `MYSQL_DATABASES` | Comma-separated names; empty selects all non-system databases |
| `MYSQL_DUMP_FREQUENCY_MINUTES` | `720`; SQL dump interval, starting immediately |
| `MYSQL_DUMP_KEEP` | `3`; completed dump bundles retained in the shared Docker volume |
| `MYSQL_INTERVAL_HOURS` | `12`; sender archive interval for the `mysql` folder, including uploads when enabled |
| `MYSQL_KEEP` | `3`; local sender archives retained for `mysql` |
| `MYSQL_BANDWIDTH_KIB_PER_SECOND` | `1024`; SQL archive upload limit when remote transfer is enabled |

The database must accept TCP connections from Docker, and the backup account must be allowed to connect from that source address with privileges to read the selected databases, routines, and triggers. `localhost` inside the backup container refers to that container. The supplied host-gateway mapping supports a database on the Docker host; change `MYSQL_HOST` for another server. Passwords containing `$` can be single-quoted in `.env.mysql` to prevent interpolation. Keep that file private; it is ignored by Git.

Completed dumps are gzip-compressed tar bundles containing one `.sql` file per database, rather than plain gzipped SQL. The post-backup hook stages a complete bundle outside the sender's source directory, then publishes it with an atomic rename. The sender waits for the first completed bundle before startup. Dumps remain in the `mysql_dumps` named volume; do not use `down --volumes` when preserving them. The producer runs as root inside its container to initialize that volume. Its target cache and published bundles each retain up to the configured count (with a temporary extra copy during a dump).

The dump and sender schedules run independently. A later database connection failure leaves earlier completed dumps available, so monitor `mysql-backup` logs for freshness; a healthy container indicates an existing completed dump. The receiver applies its default retention to `mysql` unless you add a `mysql` override in its retention configuration. To restore, extract a sender archive, extract the selected `db_backup_*.tgz` tar bundle, then import the SQL into a separate database for verification.

The image settings follow the [upstream 1.5.0 configuration reference](https://github.com/databacker/mysql-backup/blob/v1.5.0/docs/configuration.md).

### Portainer MySQL stack: local backups

Paste the complete contents of [`compose.sender.mysql.portainer.yml`](compose.sender.mysql.portainer.yml) into a Portainer **Docker Standalone** stack's web editor. This example embeds the sender configuration and dump publication hook, so it requires no `extends`, project-file bind mounts, SSH files, or launcher. It uses the published images.

Set the stack's environment variables using the database settings above and these folder settings:

```dotenv
SEND_TO_REMOTE=false
FOLDER1_PATH=/usr/local/centovacast/var/vhosts
FOLDER1_KEEP=1
FOLDER1_INTERVAL_HOURS=24
LOCAL_BACKUP_PATH=/data/local-backups
```

Keep your actual `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASES` in Portainer's environment settings. The source directory must exist on the Docker host. Sender archives appear under `/data/local-backups/folder1` and `/data/local-backups/mysql`. The MySQL service still uses `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DUMP_FREQUENCY_MINUTES`, and `MYSQL_DUMP_KEEP`; the sender uses `MYSQL_INTERVAL_HOURS` and `MYSQL_KEEP`.

This stack includes `folder1` and MySQL only. Leave `FOLDER2_PATH=none` and `FOLDER3_PATH=none` if those variables are already present; they have no mounts or jobs in this variant. Upload bandwidth settings have no effect because this stack requires `SEND_TO_REMOTE=false`. To add another source folder, add its read-only mount, job in the embedded sender configuration, and environment overrides. For remote transfers, use the launcher-based example above.

The error `bind source path does not exist: /data/compose/1/scripts/publish-mysql-dump.sh` comes from using the launcher-based file in a stack without that script on the Docker host. The relative JSON config and SSH mounts in that file also need host files, and `PATH=none` mounts are removed only by the launchers. Use the Portainer stack instead of creating placeholder files. See [Portainer's relative-path documentation](https://docs.portainer.io/advanced/relative-paths).

## Configuration details

Sender folder example:

```json
{
  "id": "folder3",
  "path": "/sources/folder3",
  "interval_hours": 6,
  "keep": 7,
  "compression_level": 6,
  "retry_seconds": 120,
  "remote": {
    "bandwidth_kib_per_second": 512
  }
}
```

Use a unique folder `id` across every sender writing to the same target: prefix it with the source machine's name. IDs use lowercase letters, digits, `_`, and `-`. Add each ID to its identity's receiver `senders.<user>.folders` allowlist. The receiver rejects duplicate assignments, and SFTP accounts cannot create additional top-level upload folders.

For the three supplied folders, nonempty `.env` keep, interval, and bandwidth settings override their JSON values. Folder 1 defaults to 3 local backups every 12 hours at 1024 KiB/s; folder 2 to 5 every 24 hours at 512 KiB/s; folder 3 to 3 every 12 hours at 1024 KiB/s. Omit an environment setting or leave it empty to use the folder's JSON value, then the shared default when no folder override exists. Keep counts must be integers of at least 1; invalid settings prevent startup.

Environment variable names use the uppercase folder ID: `folder1` uses `FOLDER1_KEEP`, for example. If you change a folder ID, update the environment variable names passed through `compose.sender.yml` to match. The script reads environment variables supplied by Compose; when running Python directly, export these variables yourself.

Sender `remote` settings apply to all folders. A folder can override individual remote settings using its own `remote` object, or set `"remote": false` to back up locally only. Set `SEND_TO_REMOTE=false` in the sender environment to disable all uploads and have the launchers omit SSH secrets automatically. The JSON-only alternative is global `remote.enabled=false` (unless a folder overrides it); SSH secret mounts are omitted automatically only with `SEND_TO_REMOTE=false`.

`bandwidth_kib_per_second` accepts nonnegative numbers; `0` disables throttling. The limit applies to archive uploads. SSH setup and small directory/rename operations are not bandwidth limited. Different sender containers each have their own limit. Increase `transfer_timeout_seconds` for archives that need more than the default 24 hours to upload at your selected speed.

Receiver example for independent retention:

```json
{
  "backup_dir": "/backups",
  "default_keep": 3,
  "check_interval_hours": 24,
  "stale_partial_hours": 168,
  "minimum_retention_hours": 168,
  "publish_interval_seconds": 2,
  "senders": {
    "backup": {
      "authorized_keys_file": "/run/secrets/authorized_keys",
      "folders": ["folder1", "folder2", "folder3", "mysql"]
    }
  },
  "max_total_backups": null,
  "folders": {
    "folder1": {"keep": 3},
    "folder2": {"keep": 5},
    "folder3": {"keep": 2}
  }
}
```

Sender and receiver retention settings are independent; set the receiver override separately if you want it to match a sender override. Assigned folder IDs receive `default_keep` unless listed in the receiver's `folders`. Keep counts must be integers of at least 1. Receiver retention orders new archives by receiver-controlled acceptance time, ignoring client filename dates. `minimum_retention_hours` defaults to 168 and must be at least 1; count-based cleanup cannot remove copies younger than this window. This prevents an upload flood from immediately displacing recovery history. An optional `max_total_backups` adds a server-wide count target **after** per-folder pruning, still subject to the age floor, and can leave old folders with fewer copies; leave it `null` for per-folder retention only. Copies that age out of the protection window remain subject to trusted retention policy. Removing sender/folder assignments does not erase accepted archives; administrators still control their retention configuration.

Configuration loads at startup. After changing it, recreate the affected service so file bind mounts are refreshed:

```powershell
.\sender.ps1 up -d --force-recreate
```

On Linux/macOS use `python3 sender-compose.py up -d --force-recreate`. On the receiver host, use `docker compose -f compose.receiver.yml up -d --force-recreate`. To update the images, run `.\sender.ps1 pull` (or `python3 sender-compose.py pull`) on the sender and `docker compose -f compose.receiver.yml pull` on the receiver, then recreate the affected service. For local Python code or Dockerfile changes, build the corresponding image explicitly with `docker build -t kahfluie/backupkit-sender:latest -f sender/Dockerfile .` or `docker build -t kahfluie/backupkit-receiver:latest -f receiver/Dockerfile .` before recreating it.

## Failure handling and retention

- Local archives are written to `.partial` files and renamed only after compression succeeds.
- Uploads use `scp` to a private inbox `.partial` filename, then SFTP to rename the completed upload and publish a `.ready` size/hash manifest. Modern OpenSSH SCP uses SFTP by default. This setup does not use the legacy `scp -O` protocol. The receiver makes the protected copy and receipt; successful SCP alone does not count as acceptance.
- Failed uploads retain one pending local archive and retry the same archive from the beginning. While a folder has a pending upload, it does not create additional snapshots; other folders continue after the failed attempt finishes.
- Local pruning runs after a successful archive/upload. During an upload failure, one extra pending archive can temporarily exceed the configured local count. Existing completed backups are preserved.
- Remote pruning runs at startup and every 24 hours by default. Counts can exceed the configured targets between checks and throughout the minimum retention window.
- The sender waits up to `remote.acceptance_timeout_seconds` (default 300) for a matching receipt after transfer. Missing/mismatching receipts keep the local archive pending and do not advance the success schedule. A matching receipt on retry avoids another transfer. Interrupted publication recovers an already-published identical archive without overwriting it.
- Retention only touches files named by this tool in protected immediate folder-ID directories. Symlink files/directories and unrelated files are ignored. Staging cleanup examines only assigned inbox folders using no-follow directory handles. Generated protocol files older than seven days are removed; active uploads normally refresh their mtime. Increase `stale_partial_hours` if your environment can pause uploads for longer. Sender-controlled inbox mtimes never determine accepted archive age.
- Logs go to Docker logs, with rotation configured in Compose. Disk space must accommodate a full new compressed archive locally and remotely before old copies are pruned.

Preview target cleanup while the receiver is running:

```sh
docker compose -f compose.receiver.yml exec receiver python -m backupkit.receiver --dry-run
```

To run actual cleanup immediately, stop the scheduled receiver first:

```sh
docker compose -f compose.receiver.yml stop receiver
docker compose -f compose.receiver.yml run --rm receiver --once
docker compose -f compose.receiver.yml up -d
```

The backup is a filesystem copy of files as they are read, not a coordinated application snapshot. Dump databases or pause applications before backing up their live data. Symlinks are stored as links without following their targets. Extended attributes and ACLs are not preserved. A changing or unreadable source can fail a backup; review logs and periodically test restores.

## Restore

Select an archive from the local or target storage and inspect it, then extract into a separate directory:

```sh
tar -tzf /path/to/backup-20261017T120000123456Z-a12b34c56d78.tar.gz
mkdir -p restored
tar -xzf /path/to/backup-20261017T120000123456Z-a12b34c56d78.tar.gz -C restored
```

On Windows, the built-in `tar` accepts the same `-tzf` and `-xzf` arguments. Restore into an empty directory and verify the contents before replacing live files. The root folder name inside the archive is the source mount basename, such as `folder1`.

## Development and checks

Python 3.12+ is sufficient for the logic tests:

```sh
python -m unittest discover -s tests -v
```

Run the real Docker transfer test on a Linux Docker host:

```sh
sh tests/integration.sh
```

On Windows, run `.\tests\integration.ps1` instead. The integration checks build separate test images, generate disposable keys/data, send real SCP uploads from two identities, attempt archive deletion/replacement, cross-account access, receipt tampering and link creation, and check age-gated retention and archive contents. They remove test containers, volumes, and keys. They do not use production configuration or keys. GitHub Actions runs the Linux integration and unit checks through `.github/workflows/test.yml` when this project is hosted there.

Protocol details: [OpenSSH SCP](https://man.openbsd.org/scp.1), [SFTP batch commands](https://man.openbsd.org/sftp.1), [SSH server restrictions](https://man.openbsd.org/sshd_config.5). Container secret handling follows [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).
