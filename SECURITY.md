# Security policy

Flow holds personal financial data, so security reports are welcome and taken seriously.

## Security model

Flow is a single-user, self-hosted app **intended for a private, limited-access environment**: a home network, or remote access only through a VPN or an HTTPS reverse proxy you control. It is not designed to be exposed directly to the internet, and the decisions below assume it isn't.

**Design decisions**

- **Authentication.** One shared password, kept as a scrypt hash in Flow's database (`FLOW_PASSWORD` in `.env` sets it on the first start), no user accounts or roles. The session cookie is signed and tied to the password, so changing the password, or **Sign out everywhere**, ends every session; **Sign out** revokes that one cookie, copies included. Failed logins are limited per client (5 per 5 minutes); a locked-out client is refused before its password is checked. Startup refuses a weak or example `SECRET_KEY` and password.
- **Browser protections.** Every response carries a Content-Security-Policy, `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy`. Nothing is loaded from outside the server. The session cookie is `HttpOnly` and `SameSite=Lax`, and every state-changing request must come from Flow's own origin by the browser's account (`Sec-Fetch-Site`, or `Origin` against `Host`): together these are the CSRF defense, and the origin check covers another port on the same host name, which `SameSite` alone treats as the same site.
- **Untrusted input.** Imported files, pasted pages and backups are treated as hostile: text is escaped when displayed, request bodies (12 MB), files (10 MB, 50,000 rows) and backup zips have size limits, and backups are checked against the database schema before anything is restored.
- **Containers.** The app runs as an unprivileged user, with no compiler or pip in its image. Base images are pinned by digest and updated through reviewed pull requests. The database port isn't published outside the Docker network.
- **AI features.** Off unless you add an API key, which is stored encrypted under `SECRET_KEY` in Flow's database rather than as an environment variable (so `docker inspect` doesn't show it), and never included in a backup. What each feature may send is limited by design, checked by tests and logged in full (see the README's privacy notes).

**Known constraints and accepted risks**

- **Plain HTTP by default.** Without HTTPS, anyone able to watch your network traffic (another device on shared Wi-Fi, for example) could read the password or session cookie.
- **The Content-Security-Policy allows inline script**, because the page uses inline handlers. Output escaping is the main defense against script injection.
- **Login lock shared between devices.** Failed logins are counted per client address. Where Docker hides the real address (Docker Desktop, connections through Docker's userland proxy), every device shares one count, so another device on your network can lock you out for five minutes, though not get in.
- **No data encryption by Flow**, beyond the API key. The database and backup zips are stored in plain form; their protection is the host's. `SECRET_KEY` in `.env` is the root: it signs sessions and encrypts the stored API key, so a database dump alone holds nothing usable.
- **The app's database user is a Postgres superuser**, as the official image creates it.
- **Some scanner findings are accepted on a private network:** Docker hardening options not set (capability limits, `no-new-privileges`, health checks) and base-image CVEs in code Flow never calls (for example in zlib's gzip-file functions and in Postgres's `gosu` start-up helper); fixes for those arrive with updated official images.

## Reporting a vulnerability

Please report it privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Don't open a public issue or pull request for it.

Include what you can of:

- what an attacker could do, and what they would need (network access, a session, a crafted file)
- steps to reproduce, or a proof of concept
- the version (commit) you tested

You should get an acknowledgement within a week. Once a fix is ready it is released on `main` and the advisory is published, crediting you unless you'd rather not be named.

## Supported versions

Only the latest release is supported (the version is at the bottom of the **Sign out ▾** menu). Update with `git pull && ./deploy.sh` before reporting, in case the problem is already fixed. Security fixes ship as a new release, listed on the Releases page.

## Scope

Flow is a single-user, self-hosted app with one shared password. In scope: anything that lets someone without the password read or change data, run code on the server, or get data out of the AI features beyond what the README says is sent; and anything that lets a crafted import, backup or pasted text do harm.

Out of scope: attacks that need the password, `.env` or shell access to the host; the accepted risks listed above; and running Flow exposed to the internet, or to an untrusted network, without the protections the README describes (a VPN, or HTTPS through a reverse proxy with `COOKIE_SECURE=true`).
