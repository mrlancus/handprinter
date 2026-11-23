# Using Handprinter online (iPad, phone, anywhere)

Handprinter keeps running **on your PC**. A free Cloudflare Tunnel gives it an `https://` web
address so your iPad or phone can use it from anywhere, behind a password. Your photos, database,
Claude Code recognition and the GPU models all stay on the PC; Cloudflare only carries the
encrypted traffic. The PC must be on, with the online window open, while you use it.

## Start it (Windows)

**One click:** double-click `Handprinter.bat` in the `handprinter` folder. It pulls the latest
code, installs or updates whatever is missing, and starts the app online. (`Handprinter.bat local`
starts it for this PC only.) The steps it performs:

1. Double-click `scripts\online.bat`. The first time it installs `cloudflared` with winget (say yes
   if Windows asks). If it says cloudflared was just installed but is not found, close the window
   and double-click it again.
2. After a few seconds a box appears with an address like
   `https://some-random-words.trycloudflare.com`. It is also shown in Settings → Data and access.
3. Open that address on the iPad, enter the password, and tap **Sign in**. You stay signed in on
   that device for 30 days.
4. iPad: tap Share → **Add to Home Screen**. Handprinter then opens full screen like an app.

Closing the window takes the site offline. Stopping it with `Ctrl+C` is fine.

## The password

The password is `6892` until you change it. To change it, put this line in the `.env` file in the
`handprinter` folder (copy `.env.example` to `.env` if you don't have one), then restart:

```
HANDPRINTER_PASSWORD=your new password
```

Changing it signs every device out. Wrong guesses are limited: 5 per device per 15 minutes and 20
in total per day, after which sign-in pauses (devices already signed in keep working).

## A permanent address, free, no domain (recommended)

The free quick address changes every time you start it. A free Cloudflare Worker gives you a fixed
`https://handprinter.<you>.workers.dev` address instead. Everything still runs on your PC: each time
Handprinter starts online, your PC tells the Worker its new quick address, and the Worker passes
every request through to it. Your password works as before. When the PC is off, that address
shows "Handprinter is not running" instead of an error.

With a Cloudflare API token (Workers Scripts: Edit) and account id in the environment, `python scripts/deploy_relay.py` does steps 3-7 for you (it uses a Durable Object instead of KV and prints the two `.env` lines); `--update` redeploys a changed `worker.js` and keeps the secret.

One-time setup by hand (about 10 minutes, all in the browser):

1. Make a free account at **dash.cloudflare.com** (no domain needed).
2. Make a secret on your PC (any long random text). In a terminal in the Handprinter folder:
   ```
   .venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Copy what it prints. Keep it private.
3. **Storage & Databases → KV → Create** a namespace called `handprinter-relay`.
4. **Workers & Pages → Create → Worker** (start from "Hello World"), name it `handprinter`, **Deploy**.
   Then **Edit code**, delete everything, paste the whole of `relay/worker.js` from this folder,
   and **Deploy** again.
5. On the Worker: **Settings → Bindings → Add → KV namespace**: variable name `RELAY`, namespace
   `handprinter-relay`.
6. **Settings → Variables and Secrets → Add**: type **Secret**, name `RELAY_SECRET`, value: the
   secret from step 2. Save/deploy.
7. Note the Worker's address, shown on its overview, e.g. `https://handprinter.lucas.workers.dev`.
8. In `.env` in the Handprinter folder add:
   ```
   HANDPRINTER_RELAY_URL=https://handprinter.<you>.workers.dev
   HANDPRINTER_RELAY_SECRET=the secret from step 2
   ```
9. Double-click `Handprinter.bat`. The window now shows the workers.dev address; bookmark it or
   Add to Home Screen on the iPad once, and it stays the same.

Notes: right after the PC restarts, the old address can linger for up to a minute, so the page may
say "not running" briefly: reload. If the window says "Relay not updated", it gives the reason
(usually the two secrets differ) and shows the quick address to use meanwhile. The free Workers
plan allows 100,000 requests a day, far more than one person uses. If you have Node, you can deploy
with `npx wrangler deploy` from the `relay` folder instead of steps 4-6 (fill in `relay/wrangler.toml`).

## A permanent address on your own domain (optional)

If you have a domain on Cloudflare, a named tunnel also gives a fixed address:

1. Cloudflare dashboard → Zero Trust → Networks → Tunnels → **Create a tunnel** (Cloudflared).
2. Add a public hostname, e.g. `hand.yourdomain.com`, pointing to `http://127.0.0.1:8765`.
3. Copy the tunnel token it shows, and put it in `.env`:
   ```
   HANDPRINTER_TUNNEL_TOKEN=the long token
   HANDPRINTER_PUBLIC_URL=https://hand.yourdomain.com
   ```
4. Start `scripts\online.bat` as usual.

## What works online

Everything, because it is still the same app on your PC:

| Part | Online |
| --- | --- |
| Upload photos from the iPad (camera or Photos) | yes, up to 95 MB per file (Cloudflare's limit is 100 MB). Split larger ZIPs. |
| Page analysis, omitting areas, corner correction | yes, touch friendly |
| Review | yes: tap outlines, pinch to zoom, two fingers to pan, **Select several** instead of shift-click, Previous/Next buttons |
| Recognition | yes: online mode turns on **Run headless**, so the PC's Claude Code runs it when you tap the button. The PC must be signed in to Claude Code (`claude auth login`, once). |
| Dataset export, Studio, maths | yes. Exported files are written on the PC. |
| Model experiments (GPU) | yes, run on the PC's GPU once set up |

## Notes

- Quick tunnels are meant for personal and testing use and come with no uptime promise. If the
  address stops responding, restart `online.bat` for a new one. A named tunnel is more dependable.
- Normal `scripts\run.bat` is unchanged: it is local to the PC only, with no password.
