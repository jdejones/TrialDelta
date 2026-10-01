# Host TrialDelta on AWS

The included deployment targets a small, single-instance customer pilot. Nothing has been deployed to AWS. Review the resources against your credits and budget before creating them; credits do not imply every service is free.

## EC2 with Docker Compose

1. Create an EC2 instance with a supported Linux distribution and install Docker Engine plus the Docker Compose plugin using their official instructions. The application is CPU-only. Persistent trial history and logs need disk space; size the instance after observing your actual workload.
2. Allocate persistent storage and a stable public address. Point your chosen domain's DNS record at that address. Limit SSH to your own IP. Allow inbound TCP 80/443 for the HTTPS proxy; do not expose port 8000 or MySQL publicly.
3. Copy this complete repository to the instance. Copy `deploy/hosted.env.example` to `.env`, set a real `DOMAIN`, a matching HTTPS `APP_ORIGIN`, and unique `ADMIN_EMAIL`/`ADMIN_PASSWORD`. Restrict access to `.env` (`chmod 600 .env`) and keep it out of source control. Use AWS Secrets Manager or an equivalent secret-injection mechanism as the deployment grows.
4. Use the persistent SQLite volume for a small single-instance pilot, or set `DATABASE_URL` to a MySQL database named `trialdelta`. Do not copy your workstation root credential to AWS. Create a dedicated database user scoped only to `trialdelta` and use encrypted connections for remote MySQL. For RDS, obtain the current AWS RDS CA bundle from the official AWS documentation, mount it read-only at `/app/certs/global-bundle.pem` in both app and worker, and use the TLS verification options shown in `deploy/hosted.env.example`. The CA mount is intentionally not included until you supply the trusted bundle. Verify certificate/hostname validation against your chosen RDS endpoint before launch.
5. Build and start:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app
```

Caddy terminates HTTPS and forwards only to the private app network. The app trusts proxy headers only from Caddy’s fixed private address (172.30.68.2), so login throttling sees the actual client address. If this subnet conflicts with another Docker network, update the subnet, Caddy address, and trusted address together. Its domain must resolve to the instance and ports 80/443 must be reachable for certificate issuance. The app health check must pass before the proxy or an explicitly enabled worker starts. The hosted template sets PUBLIC_BROWSING_ENABLED=false, so browsing remains gated by default. Local development allows anonymous research browsing; deciding to expose hosted research to visitors requires explicitly enabling that setting. Personal features and all writes always require sign-in. Customers can select Create account to register as readers. Set REGISTRATION_ENABLED=false to close self-registration; existing users can still sign in. The initial hosted administrator is separate from customer signup.

Default Compose startup serves saved database records and does not collect sources or seed the company directory. Initialize and populate the hosted database from an administrator process before expecting live coverage. To run one import on the server:

```bash
docker compose run --rm worker python -m trialdelta.worker --once
```

For an optional continuous daily collector, explicitly enable the `collection` profile:

```bash
docker compose --profile collection up -d worker
docker compose logs --tail=100 worker
```

Once deliberately enabled, the worker uses its configured restart policy until an operator stops it. A browser request cannot start collection; ingestion, source-inbox, job-history, and database-summary HTTP routes are absent.

The Tkinter administrator console runs on a desktop, outside the web container. Run `.\admin.ps1` on Windows or `python -m trialdelta.admin_app` on a desktop with Tk support, and configure the same owner-managed database as the hosted server. For MySQL, use private connectivity or an authenticated tunnel and verified TLS where appropriate. A workstation SQLite file cannot act as the server's SQLite volume: operate that database using the server-side worker or an administrator desktop on the server host with access to the same file. The console opens without starting imports and provides selected-company/all-company imports, database metrics, source candidates, and review controls.

The source inbox bridge is optional and is not enabled in the cloud example because your local news database is not automatically reachable from AWS. Add a secure, read-only source connection and the worker's `--import-news` option if you deliberately make that pipeline accessible. Only imported candidate metadata is written to `trialdelta`; the source database receives SELECT queries only. Do not expose your local database to the public internet.

Configure SMTP_HOST, SMTP_PORT, SMTP_FROM, and provider authentication to enable account verification and password-recovery emails. These are requested by customers and do not need a worker. Daily digest delivery is separate: recipients must verify their email and opt in, and the worker must explicitly include `--send-email`. Validate verification, recovery, and digest delivery with a controlled recipient before launch. See [account operations](ACCOUNTS.md).

## Persistence and operations

- SQLite history is in the `trialdelta-data` volume; HTTPS state is in Caddy's two volumes. `docker compose down` keeps named volumes; avoid `down -v` unless intentionally deleting them.
- Stop application and worker writes before copying SQLite files, or use SQLite's online backup API. Restore and verify backups periodically. If using RDS, configure and test database backups independently of instance backups.
- Keep the application behind the proxy, apply operating-system security updates, and monitor disk usage, app health, failed collection jobs, and mail delivery reservations.
- Use a single application process for this pilot. Authentication throttles and sessions are stored in the shared database. Review database contention, move email delivery and jobs into a queue, and load-test before scaling horizontally. SQLite should not be used as a multi-host shared database.
- Updates: back up the database, pull/review source, run the test suite, then `docker compose up -d --build`. This account upgrade adds missing account tables and indexes without changing existing columns. Future column changes need explicit migrations.
- When credits expire or the pilot ends, stop/delete the resources you no longer need, including any unattached volumes, addresses, databases, and snapshots that continue accruing charges.

## References

- [AWS: security groups for your VPC](https://docs.aws.amazon.com/vpc/latest/userguide/vpc-security-groups.html)
- [AWS: Amazon RDS security](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.html)
- [FastAPI: building a Docker image](https://fastapi.tiangolo.com/deployment/docker/)
- [Docker: install the Compose plugin](https://docs.docker.com/compose/install/linux/)
- [Caddy: automatic HTTPS](https://caddyserver.com/docs/automatic-https)

The Docker/AWS path is supplied for later deployment. Local API/browser tests do not certify a provisioned AWS environment; verify the built container, DNS, HTTPS, database connectivity, backup restoration, and sign-in on your chosen infrastructure before opening access.
