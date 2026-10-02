# Apple Store iPhone pickup checker

Checks US Apple Store pickup availability for configured iPhone models and ZIP
codes. It can run locally and optionally send email or WhatsApp notifications.

## Run locally

Open PowerShell in the project folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env
.\.venv\Scripts\python.exe check.py --dry-run
```

`--dry-run` prints the result without sending notifications. Omit it when you
want to send configured notifications:

```powershell
.\.venv\Scripts\python.exe check.py
```

Playwright first tries a normal Chrome window placed off-screen, then falls
back to headless mode. To force one mode, set `BROWSER_MODE` in `.env` to
`windowed` or `headless`. With no setting, the script tries both.

## Configure devices and locations

Edit `config.json` in the same folder as `check.py`:

- `zips`: US ZIP codes to check.
- `radius_miles`: maximum distance for nearby stores.
- `devices`: devices to check. Specify `model`, `color`, `storage`, and
  `carrier` (`Unlocked`, `AT&T`, `Verizon`, or `T-Mobile`), or specify a
  part number directly with `"part": "..."`.

By default, `check.py` reads `config.json` next to the script, regardless of
PowerShell's current directory. To use a different config file, pass its path:

```powershell
.\.venv\Scripts\python.exe check.py --config "D:\path\to\config.json" --dry-run
```

To see Apple's available model/color/storage names:

```powershell
.\.venv\Scripts\python.exe check.py --list "iPhone 18 Pro"
```

## Environment and notifications

The script loads `.env` from the same folder as `check.py`. Existing process
environment variables take precedence over values in `.env`. Copy
`.env.example` to `.env` and fill in only the channels you use. Keep `.env`
private and do not commit it.

Supported notification settings:

| Channel | Environment variables |
|---|---|
| Email (Gmail) | `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER`, `SMTP_PASSWORD` (Gmail App Password), `EMAIL_TO` (comma-separated recipients are supported) |
| WhatsApp (CallMeBot) | `CALLMEBOT_PHONE`, `CALLMEBOT_APIKEY` |
| WhatsApp (Twilio) | `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM`, `TWILIO_TO` |

The current script does not use an `APPLE_PROXY` setting. Apple may return
HTTP 541 from a local network; the browser fallback retries the request and
tries both browser modes, but cannot guarantee Apple will accept it. Apple
controls access to this undocumented endpoint.

## Optional: GitHub Actions

The repository includes a workflow that runs every 30 minutes and can also be
started manually from the Actions tab. It runs on a GitHub-hosted runner, not
on your local computer. Configure the notification credentials as repository
Actions secrets using the variable names in the table above.

GitHub may pause scheduled workflows after 60 days without repository activity.
