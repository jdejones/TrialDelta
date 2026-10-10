# TrialDelta portfolio operations

The portfolio serves a fixed, sanitized snapshot of public healthcare research.
News is frozen to stored calendar dates October 2 through October 9, 2026.
Collection workers, registration, SMTP and digest delivery are disabled.

The launch export contains 982 companies, 27,524 studies, 49,600 source snapshots,
671 changes, all 29,274 filing rows, and 1,114 news articles for 399 of the 1,094
dataset symbols. Five articles arrived after the planning inventory and before
the consistent export snapshot; all remain within the approved news window.
Existing users, credentials, profiles and operational records were excluded.
One evidence item lacked verified public provenance and was redacted/withheld;
its ID was retained and no changes referenced it. Private notes and attribution
were sanitized. The ignored export manifest records all counts and checksums.

## Deployment

- Public URL: **https://trialdelta-derekjones.duckdns.org**; HTTPS certificate is trusted and renews through Caddy.
- AWS project: `617619405138`; profile: `default`; selected Region: `us-east-2` (Ohio).
- Lightsail instance: `trialdelta-portfolio`, Availability Zone `us-east-2a`, Ubuntu 24.04, Small (`small_3_0`), 2 GB RAM / 60 GB disk.
- Static IP resource: `trialdelta-portfolio-ip`; address: `77.113.169.168`.
- Expected infrastructure usage: **$13–15/month**, including the $12 instance and approximately $1–3 for snapshots.
- Attached static IPv4 is included. DuckDNS and Caddy certificate renewal have no additional hosting charge.
- Application release lives in `/opt/trialdelta`. Docker Compose runs one app process and Caddy. Public IPv4 permits TCP 80/443; SSH 22 permits only `66.179.142.10/32`. No public IPv6 rules are enabled.
- The app is private at `172.30.68.3:8000`; Caddy is `172.30.68.2`. These container addresses are not public endpoints. MySQL is not deployed or publicly exposed.
- Named volumes preserve SQLite (`trialdelta_trialdelta-data`) and Caddy state (`trialdelta_caddy-data`, `trialdelta_caddy-config`). Never run `docker compose down -v`.

The local ignored `artifacts/portfolio` directory contains the export manifest,
database, release archive, deployment record and private `ADMIN_ACCESS.txt`.
It is restricted to the owner's Windows identity and SYSTEM. Do not publish it,
commit it, email its credentials, or upload the original workstation `.env`.
The application archive contains only allowlisted application files.

Launch verification passed 850+ test cases (four desktop environment skips),
live desktop/mobile guest browsing, administrator login and a protected unchanged
target save, registration/write rejection, HTTPS redirects and private-port checks.
The server was rebooted and automatically recovered its application, HTTPS,
administrator access and firewall rules. After reboot, 21 requests across eight
concurrent workers had a 0.86-second p95 response time. The ignored
`deployment.json` and verification reports preserve launch evidence.

## Administrator access

Use the public application's `/#login` address and the fresh credentials in
`ADMIN_ACCESS.txt`. Visitor Sign in/Create account controls are intentionally
disabled; the separate administrator login remains available. Keep credentials
in a password manager. There is no email password-recovery service in this deployment.
Review and target-edit actions still require administrator authentication.

After signing in, open **https://trialdelta-derekjones.duckdns.org/#account** and use
the Password form with your current password and a new unique password of at least
14 characters. It retains this browser's session and signs out other sessions.
Changing `ADMIN_PASSWORD` in the server `.env` does **not** rotate an existing
administrator password: those settings create the first administrator only.
If the password is lost, request a controlled recovery; email recovery is unavailable.

SSH permits the operator IPv4 `66.179.142.10/32` in **both** the Lightsail port-22
rule and the server's UFW firewall. If your ISP changes your address, add the new
`/32` to both layers while an existing session is still open, test a second SSH
connection, then remove the old rules. Changing only the Lightsail rule leaves UFW
blocking the new address. Do not use `0.0.0.0/0` or disable UFW as a shortcut.

Browser SSH is currently disabled. For a temporary recovery session, restrict the
Lightsail rule to its **Allow Lightsail browser SSH/RDP** source option and allow
the corresponding current regional browser-client source ranges in UFW for TCP 22.
Verify the sources first, remove those temporary permissions after testing the
operator `/32`, and retain an existing session until the new connection works.
The cloud browser option alone cannot bypass a UFW lockout; request recovery help
if no permitted session remains. See the [AWS firewall instructions](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-editing-firewall-rules.html).
Keep operator keys private; do not post keys or administrator passwords in chat or GitHub.

From the workstation repository, renew temporary Lightsail SSH credentials with
`.\.venv\Scripts\python.exe artifacts\portfolio\aws_ops.py access`. The local helper
writes them into the restricted artifact directory and checks the pinned server
identity. If AWS login has expired, run `aws login --profile default` and complete
the sign-in yourself. Connect from the permitted operator address in PowerShell:

```powershell
& 'C:\Program Files\Git\usr\bin\ssh.exe' `
  -i artifacts/portfolio/ssh-key `
  -o CertificateFile=artifacts/portfolio/ssh-key-cert.pub `
  -o UserKnownHostsFile=artifacts/portfolio/known_hosts `
  -o StrictHostKeyChecking=yes ubuntu@77.113.169.168
```

These temporary credentials expire; renew them for a later maintenance session.
Keep the helper's locally installed SDK directory with the private artifacts.

## Health and maintenance

Run on the server from `/opt/trialdelta`:

```sh
sudo docker compose ps
sudo docker compose logs --tail=100 app proxy
df -h /
sudo docker system df
curl --fail https://trialdelta-derekjones.duckdns.org/api/health
```

Logs rotate at 10 MB with three files per service. Ubuntu security updates are
enabled. Reboot after necessary kernel updates and check the HTTPS health endpoint.
Keep at least 15 GB of working disk space. Investigate growth before deleting
data, enlarging hosting, or pruning snapshots. Keep the `collection` profile off.

## Consistent backup and restore

Lightsail automatic snapshots are enabled for **08:00 UTC daily**. The first daily
snapshot has not yet been created at launch; confirm a successful recovery point
in Lightsail before relying on this schedule. AWS retains up to seven daily points
and rotates the oldest. Snapshots contain private administrator
password hashes and sessions, so keep them private. A stopped Lightsail instance
continues accruing costs.

Before changing the application or data, obtain a transactionally consistent
SQLite backup using the online backup API. The live source is
`/var/lib/docker/volumes/trialdelta_trialdelta-data/_data/trialdelta.db`.
Run the following on the server; each backup uses a new directory and the script
refuses to overwrite an existing database:

```sh
set -eu
cd /opt/trialdelta
backup_dir="/opt/trialdelta-backups/$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -m 700 /opt/trialdelta-backups
sudo python3 - <<'PY'
from pathlib import Path
from shutil import disk_usage
size = Path('/var/lib/docker/volumes/trialdelta_trialdelta-data/_data/trialdelta.db').stat().st_size
if disk_usage('/opt/trialdelta-backups').free < size + 15 * 1024**3:
    raise SystemExit('Need room for a full backup plus 15 GiB free; move an older verified backup off-server first.')
PY
sudo mkdir -m 700 "$backup_dir"
sudo python3 deploy/sqlite_backup.py \
  /var/lib/docker/volumes/trialdelta_trialdelta-data/_data/trialdelta.db \
  "$backup_dir/trialdelta.db"
sudo chown 10001:10001 "$backup_dir" "$backup_dir/trialdelta.db"
sudo chmod 600 "$backup_dir/trialdelta.db"
sudo docker run --rm --network none --env-file .env --env PYTHONPATH=/app \
  --mount "type=bind,src=$backup_dir,dst=/app/data" \
  --mount "type=bind,src=/opt/trialdelta/deploy/verify_restore.py,dst=/verify_restore.py,readonly" \
  trialdelta-app:latest python /verify_restore.py
```

The backup script must print `Consistent SQLite backup verified`; the isolated
container must finish with `"result": "passed"` and `"network": "none"`. It verifies
record counts, integrity, foreign keys, guest browsing and protected-route denial
against the backup, without mounting the live volume or publishing ports. The
initial backup directory is `/opt/trialdelta-backups/verified-20261010`; rerun its
verification by setting `backup_dir` to that directory and running only the Docker
command above. The launch backup passed an actual network-isolated restore test,
including all expected research counts, the fresh administrator, guest browsing,
integrity and foreign keys. About 21.5 GB remained free with both database copies.
Keep private backups off the instance when practical: a second copy
on the same disk does not protect against disk loss. Check free space first because
each database copy is approximately 16 GB; retaining the initial copy may leave
insufficient room for another. The preflight check aborts before copying in that case.

To restore the live database, schedule downtime, stop the app, verify the chosen
backup, preserve the current database and its WAL/SHM together for rollback, then
install the verified backup with UID/GID 10001 and restart the app. Validate HTTPS,
record counts, calendar corrections and administrator login. Restore server-wide
snapshots through Lightsail when disk/host recovery is necessary; assign the stable
IP to the recovered server and verify DNS/certificate state. Resource replacement
or manual cleanup requires owner approval. The verification script currently checks
the launch dataset and one administrator; review its expected counts before a
deliberate data refresh. Restoring a backup also restores its password hashes and sessions.

## Application updates and data refresh

1. Review code changes and run the full tests and portfolio browser checks locally.
2. Produce a consistent backup and retain the previous application archive.
3. Build an allowlisted release with `python scripts/package_portfolio.py --output artifacts/portfolio/application.tar.gz`.
4. Transfer the release over authenticated SSH to a new staging directory. Inspect
   it before replacing application files; preserve the server `.env` and named volumes.
5. From `/opt/trialdelta`, run `sudo docker compose up -d --build app proxy`.
6. Verify health, guest browsing and admin actions. Roll back to the previous release
   and verified database backup if incompatible changes occur.

Data refresh is an explicitly requested maintenance operation. Locally run
`python scripts/export_portfolio.py --output-directory artifacts/portfolio --target-disk-gb 60`.
It creates a new sanitized artifact and manifest without changing MySQL. Its news
window is deliberately fixed; changing the snapshot date/window requires a reviewed
exporter change. Validate privacy, counts, checksums, foreign keys and the 60 GB storage
gate before uploading. Replacing cloud research data can supersede cloud admin edits;
preserve and reconcile those edits before a refresh.

## Credits and retirement

The latest inspection confirmed the **Free plan**, **$100 credits**, and expiration
on **April 9, 2027**; no Paid-plan upgrade was performed. Check actual
credits and usage in AWS Settings > Billing. Other AWS activity shares that balance.
Confirm the Region in AWS Settings > View all projects > Overview > Additional Info > Region.

Do not assume unused credits extend the Free plan. Decide before expiration whether
to approve a Paid-plan upgrade or retire the portfolio. Upgrades, larger resources,
resource deletion and manual cleanup need separate approval. Deletion, rather than
stopping, is what ends ordinary Lightsail instance billing. Snapshots and unattached
static addresses can continue accruing costs and require deliberate approved cleanup.
