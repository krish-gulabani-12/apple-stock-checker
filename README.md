# Apple Store iPhone pickup checker

Checks US Apple Store in-store pickup every 30 minutes via GitHub Actions and notifies you every run (available / not available / failed).

## Setup

1. Create a **private** GitHub repo and push this folder to it.
2. Edit `config.json`:
   - `zips`: list of US ZIP codes
   - `radius_miles`: max store distance
   - `devices`: `model`, `color`, `storage`, `carrier` (`Unlocked`, `AT&T`, `Verizon`, `T-Mobile`). Or give `"part": "MJQ44LL/A"` directly.
   - Exact names must match Apple's: `python check.py --list "iPhone 18 Pro"` prints valid combos (works from India).
3. Repo → Settings → Secrets and variables → Actions → add secrets for the channels you want:

| Channel | Secrets |
|---|---|
| Email (Gmail) | `SMTP_HOST`=`smtp.gmail.com`, `SMTP_PORT`=`587`, `SMTP_USER`=your Gmail, `SMTP_PASSWORD`=[App Password](https://myaccount.google.com/apppasswords), `EMAIL_TO` (comma-separated ok) |
| WhatsApp, free (CallMeBot) | `CALLMEBOT_PHONE` (e.g. `+9198...`), `CALLMEBOT_APIKEY` — get the key by following https://www.callmebot.com/blog/free-api-whatsapp-messages/ |
| WhatsApp (Twilio) | `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM`, `TWILIO_TO` |

4. Actions tab → "iPhone availability check" → **Run workflow** to test once. After that it runs every 30 min.

## Notes
- Running locally from India fails (Apple returns HTTP 541 to non-US IPs); that's why it runs on GitHub's US servers.
- GitHub pauses scheduled workflows after 60 days with no repo commits — push any small change to keep it alive.
- Uses Apple's undocumented pickup endpoint; if Apple changes it, the run will report "FAILED".
