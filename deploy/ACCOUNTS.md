# Customer account infrastructure

## Configuration and upgrade

Restart the web server after updating. Startup creates missing account tables in the configured TrialDelta database without dropping or altering existing user and research tables. Back up the database before an upgrade. Customer accounts, profile details, sessions, email-token hashes, and authentication throttles are persisted alongside the existing per-user watchlists and preferences.

`AUTH_REQUIRED` and `REGISTRATION_ENABLED` default to `true`. `PUBLIC_BROWSING_ENABLED` defaults to `true` for loopback-only local development and `false` in hosted mode; the hosted template explicitly keeps it false. Personal actions always require an account. Hosted mode never honors the local administrator shortcut, even if AUTH_REQUIRED is false. Never expose local mode remotely. The desktop administrator workflow is independent of customer signup.

SMTP settings are shared with the existing digest worker: SMTP_HOST, SMTP_PORT (587), SMTP_FROM, SMTP_USER, and SMTP_PASSWORD. The mail connection requires STARTTLS. Set APP_ORIGIN to the real origin; production also requires HTTPS and COOKIE_SECURE=true. Credentials belong in the deployment environment, never source control. SMTP is optional for initial local account work; configure and test it before relying on email verification or recovery with customers.

## HTTP API

All mutation requests require `X-TrialDelta-Client: web` and pass the existing same-origin checks. Authentication uses the `td_session` HttpOnly cookie. Account routes take the current identity from the session; clients cannot choose another user ID or upgrade their role.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /api/auth/config | Public account capabilities and password minimum |
| POST | /api/auth/register | Create reader from email, password, optional display_name; start session |
| POST | /api/login | Authenticate email and password |
| POST | /api/logout | Revoke current session and clear cookie |
| POST | /api/logout-all | Revoke all sessions for current account |
| GET | /api/me | Current identity and account capabilities |
| GET | /api/account | Profile, verification and preferences |
| PATCH | /api/account | Update display_name and/or digest_enabled |
| POST | /api/account/password | Change current_password to new_password; rotate session |
| POST | /api/account/verification | Send another verification link |
| POST | /api/auth/verify-email | Consume verification token |
| POST | /api/auth/forgot-password | Request recovery by email, with a generic response |
| POST | /api/auth/reset-password | Consume token and set new_password; revoke sessions |
| POST | /api/users | Administrator-only provisioning of reader accounts |

Email is read-only after account creation. Profile names are optional; no address, phone, organization, payment, or subscription fields are required. New accounts start unverified and can use the research workspace. Verification is required before digest mail is sent. Settings and Account share the same stored digest preference.

## Anonymous browsing

With `PUBLIC_BROWSING_ENABLED=true`, visitors can read the company directory, Home trial/news feeds, trial detail, published Change log entries and their evidence, and preserved registry snapshots. Anonymous company results always have `followed=false`. Change log defaults to all published entries for visitors and the personal watchlist for signed-in customers; an anonymous `followed=true` query returns an empty list.

The browser offers Sign in, Create account, and Continue browsing. Account, digest, and settings screens explain that personal features require an account. Follow and evidence-submission buttons invite sign-in; they do not submit anonymous writes or replay an action after login. No guest database row or anonymous session cookie is created. Signing out clears customer data and returns to visitor Home.

`/api/me`, `/api/account`, `/api/follows`, and `/api/digest` (including export) remain protected, as do all account/research mutations and administrator operations. Invalid or expired cookies may browse public research but cannot access personal endpoints. Unapproved company evidence remains hidden. Host/origin checks and the loopback-only local boundary still apply.

Set `PUBLIC_BROWSING_ENABLED=false` to gate browsing again. Future visitor quotas or read restrictions can be implemented centrally in `trialdelta.auth.browsing_user`; no quotas, visitor tracking, or billing have been introduced. Public hosting defaults have not changed.

## Delivery and security behavior

Sessions and email links use cryptographically random tokens; only SHA-256 hashes are retained in the database. Passwords retain the salted PBKDF2-SHA256 format. Password change/reset revokes outstanding sessions and recovery tokens. Verification and recovery tokens have separate purposes and cannot be interchanged. Link fragments are removed from browser history before form submission; no reset token is returned in an API response.

Mail requests are throttled. If SMTP is unavailable, Account and recovery show that delivery is unavailable. Registration still succeeds and leaves the address unverified. Never treat a successful generic recovery response as proof that an address exists or mail reached the inbox. Check provider logs for delivery problems without logging full recovery links or credentials.

Existing reader accounts without a profile remain usable and can create their profile through Account. They must verify their email before digest delivery. Administrator bootstrap credentials are operator-provided and stay separate from new reader registration.

## Verification

Run `python -m pytest -q` against the isolated test database. The account tests cover identity boundaries, cookies, credential changes, recovery/verification tokens, throttling, and additive initialization. Run `pnpm test:visitors` for visitor browsing and sign-in transitions, and `pnpm test:accounts` for gated account flows; it starts its own local server with a temporary SQLite database and disables mail. It does not create users in your normal database.

Before a hosted launch, test signup, verification delivery, password recovery, logout, and HTTPS cookies against your actual domain and mail provider. This repository implements the application layer; it does not create or verify AWS or SMTP service resources.
