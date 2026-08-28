# Gmail Verification Setup (for ApplyPilot auto-apply)

This lets ApplyPilot read the verification codes that job sites email you during
signup. You do this **once**. It uses the `@gongrzhe/server-gmail-autoauth-mcp`
helper that ApplyPilot launches automatically during `applypilot apply`.

Use the Google account that receives your job emails: **owner@example.com**.

---

## Part 1 — Google Cloud Console (in your browser)

1. Go to **https://console.cloud.google.com** and sign in as owner@example.com.
2. **Create a project:** top bar → project dropdown → "New Project" → name it
   `applypilot-gmail` → Create. Make sure it's selected.
3. **Enable the Gmail API:** search bar → "Gmail API" → open it → **Enable**.
4. **Configure the OAuth consent screen:**
   - Left menu → "APIs & Services" → "OAuth consent screen".
   - User type: **External** → Create.
   - App name: `ApplyPilot`. User support email: your email. Developer contact:
     your email. Save and continue.
   - Scopes: skip (Save and continue) — the app requests Gmail access at runtime.
   - **Test users: click "Add users" and add `owner@example.com`.** (Required, or
     login will be blocked.) Save and continue.
5. **Create the OAuth credentials:**
   - Left menu → "APIs & Services" → "Credentials".
   - "Create Credentials" → "OAuth client ID".
   - Application type: **Desktop app**. Name: `applypilot-desktop` → Create.
   - In the popup, click **"Download JSON"**. Save the file.

---

## Part 2 — Terminal (on your Mac)

Put the downloaded key where the helper expects it, renamed to
`gcp-oauth.keys.json`. Assuming it went to your Downloads folder:

```
mkdir -p ~/.gmail-mcp
mv ~/Downloads/client_secret_*.json ~/.gmail-mcp/gcp-oauth.keys.json
```

(If the filename is different, adjust — it's the JSON you just downloaded.)

Then run the one-time authentication:

```
npx @gongrzhe/server-gmail-autoauth-mcp auth
```

- Your browser opens a Google sign-in. Sign in as **owner@example.com**.
- You'll see an "unverified app" warning (expected — it's your own app):
  click **"Advanced" → "Go to ApplyPilot (unsafe)"**, then **Allow** all requested
  Gmail permissions.
- The terminal saves credentials to `~/.gmail-mcp/credentials.json` and prints
  a success message. Done.

---

## Verify it worked

```
ls ~/.gmail-mcp/
```
You should see both `gcp-oauth.keys.json` and `credentials.json`. Once
`credentials.json` exists, ApplyPilot's apply stage will read verification
codes automatically — no further action needed.

---

## Good to know

- **7-day expiry:** while the OAuth app is in "Testing" mode, Google expires the
  login about every 7 days, so you may occasionally need to re-run the
  `npx ... auth` command. To avoid that, go to the OAuth consent screen and click
  **"Publish app"** (set to "In production") — it keeps working for your own
  account even though it's "unverified."
- This is entirely separate from your Claude/API keys — it only grants Gmail read
  access to the local helper on your machine.
