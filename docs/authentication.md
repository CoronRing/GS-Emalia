# Authentication

Emalia needs to log in to a mailbox over IMAP and SMTP. There are four ways to
give it that, and this page covers each one end to end.

| | What you give Emalia | Expiry | Browser | Rotate without a human |
|---|---|---|---|---|
| **1. App password** | One password string | none | once, to create it | no |
| **2. Service account** | A key file, plus one admin authorisation | **none** | **never** | **yes, `gcloud`** |
| **3. OAuth, supplied directly** | Client ID, secret, refresh token | see below | once, to mint it | no |
| **4. OAuth, `emalia auth google`** | Nothing — Emalia stores it | see below | once per mailbox | no |

Ways 3 and 4 are the same credential; they differ only in where the refresh
token is kept.

Ways 1, 3 and 4 work on every provider Emalia knows. Way 2 is Google only.
Microsoft has its own console steps, in
[Microsoft mailboxes](#microsoft-mailboxes).

## Contents

- [Which one should I use](#which-one-should-i-use)
- [Way 1: an app password](#way-1-an-app-password)
- [Way 2: a service account](#way-2-a-service-account)
- [Creating an OAuth client](#creating-an-oauth-client)
- [Way 3: OAuth supplied directly](#way-3-oauth-supplied-directly)
- [Way 4: OAuth through `emalia auth google`](#way-4-oauth-through-emalia-auth-google)
- [Microsoft mailboxes](#microsoft-mailboxes)
- [The seven-day trap](#the-seven-day-trap)
- [Precedence](#precedence)
- [What is stored, and where](#what-is-stored-and-where)
- [Troubleshooting](#troubleshooting)

## Which one should I use

**Running this in production, on a Workspace domain? Use a service account.**
It is the only credential here that never expires, never needs a browser, and
can be rotated by a script. Everything else eventually asks a person to click
something, which is exactly what you do not want at three in the morning. It
requires a Google Workspace domain and a one-time authorisation by an
administrator.

**On a personal `@gmail.com`? Use an app password.** Service accounts cannot
reach a personal account — there is no domain, so there is no administrator to
delegate anything. An app password does not expire either, so it is the stable
choice here. It is two minutes of work.

**Use OAuth** when neither applies:

- a personal account where you want a credential revocable on its own, without
  changing the account password;
- a Workspace domain where you are not the administrator and cannot get
  delegation approved;
- a provider that is not Google.

Before investing in OAuth, read [the seven-day trap](#the-seven-day-trap): on
an unverified app the refresh token dies after a week, which is a poor fit for
anything unattended.

### A note on what a client ID and secret actually are

A client ID and client secret identify the **application**. On their own they
authorise nothing — they cannot open any mailbox, and they are not a
credential in the sense that matters here. Something has to say "this
application may act on *this* mailbox", and that is either:

- a **refresh token**, which a user produces by consenting in a browser, or
- **domain-wide delegation**, which an administrator grants once and which
  needs no token at all.

If you have a client ID and secret and are wondering what is missing, that is
the missing piece.

## Way 1: an app password

There is no API for this on any provider. Google in particular exposes
`list`, `get` and `delete` for app passwords through the Admin SDK but never
`insert`, so no CLI — `gcloud`, `gws`, `gam` or otherwise — can create one. It
is a browser step by design.

### Gmail

1. Turn on **2-Step Verification** at
   <https://myaccount.google.com/signinoptions/twosv>. Google will not offer
   app passwords without it, and the page simply will not exist until you do.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Name it something you will recognise later — `emalia` — and create it.
4. Copy the 16 characters. **Remove the spaces Google displays**; they are
   presentation only and pasting them causes an authentication failure that
   looks like a wrong password.

You only get to see it once. If you lose it, delete it and make another.

```bash
EMALIA_ADDRESS=you@gmail.com
EMALIA_PASSWORD=abcdefghijklmnop
EMALIA_PROVIDER=gmail
```

### Other providers

- **iCloud** — <https://appleid.apple.com>, Sign-In and Security,
  App-Specific Passwords.
- **Yahoo** — Account Security, Generate app password.
- **Fastmail** — Settings, Privacy & Security, New app password, scoped to Mail.
- **Outlook, personal** — <https://account.live.com/proofs/AppPassword>.
  Requires two-step verification.
- **Outlook, work or school** — usually impossible. Most tenants have basic
  authentication disabled entirely; use OAuth.
- **Proton** — no app password. Run the Proton Mail Bridge and use the
  credentials it prints, with `EMALIA_PROVIDER=proton`.

Then confirm it:

```bash
emalia check
```

## Way 2: a service account

**The credential for a deployed system.** A service account holds a private
key. It signs a short-lived assertion asking Google for a token to act as a
particular mailbox, and Google issues one because an administrator authorised
that account for that scope, once, in advance.

What that buys you:

- **No expiry.** There is no refresh token to die after seven days, and no
  password change that revokes it. The key works until you delete it.
- **No browser, ever.** Not at setup, not at renewal. The whole flow is a
  signed HTTP request.
- **Rotation by script.** `gcloud iam service-accounts keys create` issues a
  new key, and `keys delete` retires the old one. No console, no consent.
- **Delegation is the boundary.** The key alone grants nothing. An
  administrator decides which scopes it may use, and can withdraw that in one
  place for every mailbox at once.

**Requirements:** a Google Workspace domain, and administrator access to it (or
someone who has it). This does not work for a personal `@gmail.com` address.

### 1. Create the account and key

```bash
PROJECT=your-project

gcloud config set project "$PROJECT"
gcloud services enable gmail.googleapis.com

gcloud iam service-accounts create emalia \
  --display-name="Emalia mail agent"

gcloud iam service-accounts keys create ./emalia-key.json \
  --iam-account="emalia@${PROJECT}.iam.gserviceaccount.com"
```

`emalia-key.json` is now the credential. Treat it as you would a password: it
is not encrypted, and anyone holding it can act as every mailbox the delegation
covers.

No IAM roles are needed on the project. Mailbox access comes entirely from the
delegation in the next step, not from Cloud IAM.

### 2. Find out what to authorise

```bash
emalia auth google service-account --key ./emalia-key.json --address you@yourdomain.com
```

This prints the two values the Admin console form wants, including the
**numeric client ID** — which is buried in the key file and is not the service
account's email address, a confusion that accounts for most failed setups.

### 3. Authorise it, once

As a Workspace administrator, at
<https://admin.google.com>:

**Security → Access and data control → API controls → Domain-wide delegation →
Add new**

- **Client ID:** the numeric one the command printed
- **OAuth scopes:** `https://mail.google.com/`

The scope must match exactly. A narrower Gmail API scope does not grant IMAP,
and delegation authorised for the wrong scope fails with `unauthorized_client`
— which reads like the key is broken when it is not.

Changes can take a few minutes to take effect.

### 4. Confirm it works

```bash
emalia auth google service-account --key ./emalia-key.json \
  --address you@yourdomain.com --check
```

`--check` actually mints a token. If it succeeds, delegation is live.

### 5. Configure Emalia

```bash
EMALIA_ADDRESS=you@yourdomain.com
EMALIA_PROVIDER=gmail
EMALIA_SERVICE_ACCOUNT_FILE=/etc/emalia/emalia-key.json
# and no EMALIA_PASSWORD
```

The mailbox address doubles as the account to act as, so there is no second
variable to keep in step with it. Pointing `EMALIA_ADDRESS` at a different user
in the same domain is all it takes to run against another mailbox — no new key,
no new authorisation.

Install the signing dependency:

```bash
pip install "emalia[gcp]"
```

For a secret store with no filesystem, pass the key's JSON inline instead:

```bash
EMALIA_SERVICE_ACCOUNT_KEY='{"type":"service_account", ...}'
```

Escaped `\n` in the private key are repaired automatically, since that is what
a CI secret box usually does to a PEM.

`GOOGLE_APPLICATION_CREDENTIALS` is also honoured, but **only** when you set
`EMALIA_AUTH=service_account`. It is frequently set machine-wide for unrelated
Cloud work, and quietly authenticating a mailbox with whatever it happens to
point at would be a poor surprise.

### Rotating the key

No downtime, no browser:

```bash
gcloud iam service-accounts keys create ./new-key.json \
  --iam-account="emalia@${PROJECT}.iam.gserviceaccount.com"
# deploy new-key.json, restart Emalia, confirm with `emalia check`
gcloud iam service-accounts keys delete OLD_KEY_ID \
  --iam-account="emalia@${PROJECT}.iam.gserviceaccount.com"
```

The delegation is attached to the service account, not to the key, so it
survives a rotation untouched. `emalia check` prints the `private_key_id` of
the key actually loaded, which is how you confirm a restart picked up the new
one.

List existing keys with:

```bash
gcloud iam service-accounts keys list \
  --iam-account="emalia@${PROJECT}.iam.gserviceaccount.com"
```

### Scoping it down

Delegation grants the service account access to **every** mailbox in the domain
for the listed scope. That is more than one agent needs. Two ways to narrow it:

- Put the mailbox in its own Workspace domain or use a dedicated account, so
  the blast radius of the key is one inbox that holds nothing else.
- Set `sandbox_roots`, `allowed_senders` and `allowed_recipients` as tightly as
  the deployment allows — see [../SECURITY.md](../SECURITY.md). Those are
  enforced by Emalia regardless of what the credential could reach.

Some organisations disable service account key creation entirely by
policy (`constraints/iam.disableServiceAccountKeyCreation`). If
`keys create` is refused, that is why, and your administrator has to grant an
exception.

## Creating an OAuth client

Both OAuth paths need an OAuth client to authorise against. This is the one
piece no CLI can conjure, because it requires accepting Google's terms as a
human. It is a one-time setup — the same client works for every mailbox you
later authorise.

### With the Cloud Console

1. Create or pick a project at <https://console.cloud.google.com/projectcreate>.
2. Enable the Gmail API at
   <https://console.cloud.google.com/apis/library/gmail.googleapis.com>.
   IMAP does not strictly need it, but the consent screen will not offer the
   mail scope on a project where it is off.
3. Go to **APIs & Services → Google Auth Platform**
   (<https://console.cloud.google.com/auth/overview>) and click through
   **Get started**. Fill in **Branding** with an app name and your own email.
4. Under **Audience**, choose:
   - **Internal** if the account belongs to a Google Workspace organisation you
     administer. Strongly prefer this: no verification, no seven-day expiry.
   - **External** otherwise. Then add every mailbox you intend to authorise
     under **Test users**, or consent will be refused outright.
5. Under **Data access**, add the scope `https://mail.google.com/`. It is a
   restricted scope, so Google will warn you the app needs verification before
   it can serve the general public. That warning does not stop it working for
   test users or for an internal audience.
6. Under **Clients**, create a client of type **Desktop app**. Download the
   JSON. It is named `client_secret_<long-id>.json`.

### With the Google Workspace CLI

If you have [`gws`](https://www.npmjs.com/package/@googleworkspace/cli)
installed, it will drive `gcloud` through most of the above:

```bash
gws auth setup --project YOUR_PROJECT --dry-run   # see what it would do
gws auth setup --project YOUR_PROJECT
```

It enables the APIs and creates the consent screen and client, writing the
client file to `~/.config/gws/client_secret.json`. You still have to set the
audience and add the mail scope in the console; `gws` does not request
restricted scopes on your behalf.

Emalia finds that file automatically. It searches, in order:

1. `$EMALIA_OAUTH_CLIENT_SECRETS`
2. `~/.config/emalia/client_secret.json` and `~/.config/gws/client_secret.json`
3. the same two under `%APPDATA%` on Windows or `$XDG_CONFIG_HOME` elsewhere
4. `client_secret*.json` in the current directory

## Way 3: OAuth supplied directly

Three opaque strings, which is the right shape for CI and for a container: a
secret store can hold them and there is no file to mount.

```bash
EMALIA_ADDRESS=you@gmail.com
EMALIA_PROVIDER=gmail
EMALIA_OAUTH_CLIENT_ID=1234-abc.apps.googleusercontent.com
EMALIA_OAUTH_CLIENT_SECRET=GOCSPX-...
EMALIA_OAUTH_REFRESH_TOKEN=1//0g...
```

Optional, for a provider that is not Google:

```bash
EMALIA_OAUTH_TOKEN_URI=https://login.microsoftonline.com/common/oauth2/v2.0/token
EMALIA_OAUTH_SCOPE="offline_access https://outlook.office.com/IMAP.AccessAsUser.All ..."
```

Emalia exchanges the refresh token for an access token on first use and caches
it in memory for the process's lifetime, refreshing two minutes before expiry.
The refresh token itself is never logged and never leaves the process except in
that one POST to the token endpoint.

To get the three values, run way 4 once and add `--print-env`:

```bash
emalia auth google login --address you@gmail.com --print-env
```

**Setting only some of the three is an error, not a fallback.** A typo in
`EMALIA_OAUTH_CLIENT_SECRET` would otherwise silently drop you back to password
authentication and send a password to a server you meant to reach with a token.

## Way 4: OAuth through `emalia auth google`

The interactive path. Emalia opens a consent screen, catches the redirect on a
loopback port, exchanges the code, and writes the result.

```bash
emalia auth google login --address you@gmail.com
```

What happens:

1. Emalia finds your client file, or you point at it with `--client-secrets`.
2. A browser opens on Google's consent screen. On an External app you will see
   "Google hasn't verified this app" — expected for a restricted scope;
   continue through **Advanced**.
3. Approve the mailbox access request.
4. The browser lands on `http://127.0.0.1:<port>/` and says you can close it.
5. The refresh token is written to
   `%APPDATA%\emalia\google_oauth.json` (Windows) or
   `~/.config/emalia/google_oauth.json` (elsewhere), created mode `0600`.

Then tell Emalia to use it:

```bash
EMALIA_ADDRESS=you@gmail.com
EMALIA_PROVIDER=gmail
EMALIA_AUTH=oauth
# and remove EMALIA_PASSWORD
```

```bash
emalia check
```

### Options worth knowing

| Flag | Why |
|---|---|
| `--no-browser` | Prints the URL instead of opening one. What a headless box or an SSH session needs. |
| `--print-env` | Also prints the three variables from way 2, for copying into CI. |
| `--token-file PATH` | Write somewhere other than the default. Pair with `EMALIA_OAUTH_TOKEN_FILE`. |
| `--port N` | Pin the loopback port, if the client registers a fixed redirect URI. |
| `--address` | Preselects the account. A hint only — Emalia reads back which account actually consented and warns if it differs. |

### Checking and removing

```bash
emalia auth google status              # what is stored, with nothing secret printed
emalia auth google status --refresh    # actually exchange it, proving the grant is live
emalia auth google logout              # delete the local file
```

`logout` is local only. To revoke the grant itself, go to
<https://myaccount.google.com/permissions>.

## Microsoft mailboxes

Everything above is written around Google because that is where most of the
sharp edges are. Outlook.com and Microsoft 365 use the same OAuth machinery,
reached through `emalia auth microsoft` instead.

Microsoft is worth doing properly rather than reaching for an app password:
**basic authentication is being withdrawn in December 2026**, and most work or
school tenants have already turned it off. On those accounts OAuth is not the
recommended option, it is the only one.

### 1. Register an application

At <https://entra.microsoft.com>, under **Applications**, **App registrations**,
**New registration**:

- **Name** — anything, e.g. `emalia`.
- **Supported account types** — "Accounts in any organizational directory and
  personal Microsoft accounts" unless you have a reason to narrow it.
- **Redirect URI** — platform **Mobile and desktop applications**, value
  `http://localhost`. This is what lets the loopback redirect work. A **Web**
  platform entry will not do: it forbids the dynamic port Emalia listens on.

Copy the **Application (client) ID** from the overview page.

### 2. Add the permissions

Under **API permissions**, **Add a permission**, **APIs my organization uses**,
search for **Office 365 Exchange Online**, choose **Delegated permissions**, and
add:

- `IMAP.AccessAsUser.All`
- `SMTP.Send`
- `offline_access`

`offline_access` is the one people miss. Without it the token endpoint returns
an access token and no refresh token, so the credential dies in an hour.

On a work or school tenant, click **Grant admin consent** if you have the
rights, or ask an administrator to.

### 3. Add a client secret

Under **Certificates & secrets**, **New client secret**. Copy the **Value**, not
the Secret ID — the value is shown once and never again.

A public client registration can work without a secret, but Emalia's flow sends
one, so create it.

### 4. Log in

```bash
emalia auth microsoft login \
  --address you@outlook.com \
  --client-id <application-id> \
  --client-secret <secret-value>
```

Or put them in `.env` as `EMALIA_OAUTH_CLIENT_ID` and
`EMALIA_OAUTH_CLIENT_SECRET` and drop the flags.

The token is written to `%APPDATA%\emalia\microsoft_oauth.json` or
`~/.config/emalia/microsoft_oauth.json`, separately from the Google one, so both
providers can be authorised on the same machine.

### 5. Configure Emalia

```bash
EMALIA_ADDRESS=you@outlook.com
EMALIA_PROVIDER=outlook
EMALIA_AUTH=oauth
# and no EMALIA_PASSWORD
```

```bash
emalia check
```

### Single-tenant registrations

If you chose "this organizational directory only" in step 1, the default
endpoint rejects the login. Pass the directory id:

```bash
emalia auth microsoft login --tenant <directory-id> --address you@company.com
```

`--tenant` also accepts `organizations` (work and school only) and `consumers`
(personal only). The default, `common`, admits both.

### Status and revocation

```bash
emalia auth microsoft status --refresh
emalia auth microsoft logout
```

As with Google, `logout` only deletes the local file. Revoke the grant itself at
<https://account.microsoft.com/privacy/app-access>.

### What is not supported

There is no Microsoft equivalent of way 2 yet. Microsoft's unattended path is
the client credentials grant with application permissions, which is a different
mechanism: it needs a tenant-wide admin grant plus an application access policy
to stop the registration reaching every mailbox in the organisation.

The refresh token is therefore the only Microsoft option at present. It is less
fragile there than on Google — a Microsoft 365 refresh token is not subject to
the seven-day expiry described below, and lasts until it goes 90 days unused or
an admin revokes it.

## The seven-day trap

**This is the single thing most likely to bite you.**

A Google Cloud project whose OAuth consent screen has an **External** audience
and a publishing status of **Testing** issues refresh tokens that **expire after
seven days**. Nothing warns you. The credential simply stops working a week
later, with `invalid_grant`.

Three ways out, best first:

1. **Use an Internal audience.** Available only if the mailbox belongs to a
   Workspace organisation you administer. No verification, no expiry, no test
   user list. If you have a Workspace domain, do this.
2. **Use an app password instead.** For a personal Gmail account this is almost
   always the better answer. App passwords do not expire.
3. **Publish the app.** Moving to production makes tokens effectively permanent,
   but `https://mail.google.com/` is a restricted scope, so this means Google's
   full verification process including a third-party security assessment. That
   is weeks of work and real money — not worth it for one mailbox.

Even in production, a grant is revoked by: six months of disuse, the user
revoking it, **the account's password being changed** (specific to mail scopes),
or more than 50 live tokens for the same client and account.

Emalia's `invalid_grant` message names all of these, so you will not be left
guessing.

## Precedence

Set `EMALIA_AUTH` to `password`, `oauth` or `service_account` to name the
method outright. Otherwise `MailAccount.from_env` infers it from what is
present, in this order:

1. `EMALIA_SERVICE_ACCOUNT_KEY` or `EMALIA_SERVICE_ACCOUNT_FILE`
2. `EMALIA_OAUTH_CLIENT_ID` + `EMALIA_OAUTH_CLIENT_SECRET` +
   `EMALIA_OAUTH_REFRESH_TOKEN`
3. `EMALIA_OAUTH_TOKEN_FILE`, a path to a token file
4. `EMALIA_PASSWORD`

`EMALIA_AUTH=oauth` additionally falls back to the default token file that
`emalia auth google login` wrote.

Three rules hold throughout:

- **A password beside a token credential is rejected**, not silently resolved
  one way. That combination is what a half-finished migration looks like, and
  quietly picking either one hides it.
- **A partial OAuth triple is an error, not a fallback to the password.** A
  typo in one variable would otherwise send a password to a server you meant to
  reach with a token.
- **Inference never reaches for an ambient variable.**
  `GOOGLE_APPLICATION_CREDENTIALS` is used only when `EMALIA_AUTH` explicitly
  asks for `service_account`, because it is routinely set machine-wide for
  unrelated Cloud work.

### Standard names

For the default `EMALIA_` prefix, each credential variable also accepts its
provider-standard name, so a machine already holding these needs no second
copy. The prefixed name wins where both are set.

| Emalia | Also accepted |
|---|---|
| `EMALIA_PASSWORD` | `GOOGLE_APP_PASSWORD` |
| `EMALIA_OAUTH_CLIENT_ID` | `GOOGLE_OAUTH_CLIENT_ID` |
| `EMALIA_OAUTH_CLIENT_SECRET` | `GOOGLE_OAUTH_CLIENT_SECRET` |
| `EMALIA_OAUTH_REFRESH_TOKEN` | `GOOGLE_OAUTH_REFRESH_TOKEN` |
| `EMALIA_SERVICE_ACCOUNT_FILE` | `GOOGLE_SERVICE_ACCOUNT_FILE`, and `GOOGLE_APPLICATION_CREDENTIALS` on request |
| `EMALIA_SERVICE_ACCOUNT_KEY` | `GOOGLE_SERVICE_ACCOUNT_KEY` |

The bare `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are deliberately **not**
accepted. They are the generic names for any Google OAuth client, including a
"sign in with Google" on an unrelated service, and reading a mail credential
out of them would be a real surprise on a machine that does several things.

**Aliases apply only to the default prefix.** A suite running under its own
prefix — the end-to-end one, at `EMALIA_E2E_` — cannot see them. That isolation
is the entire reason it has a separate prefix, and a shared alias must not undo
it. See [e2e-testing.md](e2e-testing.md).

Secrets are read from the environment or `.env` only. `emalia.toml` cannot set
`password` or `oauth`; its `[account]` table accepts endpoint fields only, and
anything else is refused by name.

## What is stored, and where

| | Contents | Lifetime |
|---|---|---|
| Service account key | Private key, client email, key ID | Until you rotate or delete it |
| OAuth token file | Client ID, client secret, refresh token, scopes | Until deleted or the grant is revoked |
| Access token | In memory only, never written | About an hour; renewed automatically |

An access token is never persisted by either path. A restart costs one HTTP
round trip to get another.

The OAuth token file uses Google's `authorized_user` shape — the same one
`gcloud auth application-default login` writes — so a token obtained by other
tooling can be dropped in unchanged, and Emalia's can be read by anything that
speaks `google-auth`. It is created with mode `0600` at open time rather than
chmod'ed afterwards, so it is never briefly world-readable. On Windows the mode
is ignored and the file inherits the parent directory's ACL.

A service account key is written by `gcloud`, not by Emalia, so its permissions
are yours to set. Put it somewhere only the daemon's user can read.

Nothing prints a secret. `emalia check` shows a fragment of the OAuth client ID,
or a service account's key ID and client email — enough to tell two credentials
apart, which is the usual debugging need — and `***` for everything else. The
key ID in particular is how you confirm a restart actually picked up a rotated
key.

## Troubleshooting

**`[AUTHENTICATIONFAILED] Invalid credentials (Failure)`** — the app password is
wrong, revoked, or belongs to a different account. Remove the spaces Google
displayed. If you are certain it is right, it may simply have been revoked: a
password change on the account revokes every app password.

**`535 5.7.8 Username and Password not accepted`** — the same thing on the SMTP
side.

**`invalid_grant` on refresh** — see [the seven-day trap](#the-seven-day-trap).
Run `emalia auth google login` again for a new token, or move to a service
account and stop needing one.

**`unauthorized_client` on a service account** — domain-wide delegation is not
in place, or was authorised for a different scope. Check that the **numeric**
client ID is registered, not the service account's email address, and that the
scope reads exactly `https://mail.google.com/`. Changes take a few minutes.

**`invalid_grant` on a service account** — the address does not exist in the
domain, or is outside the organisation the service account belongs to.
Delegation cannot reach a personal `@gmail.com` account at all.

**`Service account authentication needs the cryptography package`** —
`pip install "emalia[gcp]"`.

**The private key could not be read** — its newlines were lost. A PEM passed
through a CI secret box usually arrives with `\n` escaped. Emalia repairs that
for `EMALIA_SERVICE_ACCOUNT_KEY`, but a file has to hold real line breaks.

**A rotated key does not seem to be in use** — `emalia check` prints the
`private_key_id` actually loaded. If it is the old one, the process did not
restart or is reading a different file.

**`Google returned an access token but no refresh token`** — a grant for this
client and account already exists. Emalia asks for `prompt=consent` to avoid
this, but a grant made by other tooling can still shadow it. Revoke it at
<https://myaccount.google.com/permissions> and log in again.

**"Google hasn't verified this app"** — expected on an External audience with a
restricted scope. Continue through **Advanced**. If there is no way through, the
account is not on the **Test users** list.

**`Error 403: access_denied`** — the account is not a test user on an External
app, or is outside the organisation on an Internal one.

**No response from the consent screen** — the browser did not open, or opened as
a different OS user. Rerun with `--no-browser` and open the URL yourself.

**OAuth works but IMAP still refuses** — IMAP access is off on the account. For
Workspace this is an admin setting under Apps → Google Workspace → Gmail →
End User Access.

**`Both EMALIA_PASSWORD and an OAuth credential are set`** — exactly what it
says. Delete whichever you are not using; leaving both is how you end up
authenticating in a way you did not intend.
