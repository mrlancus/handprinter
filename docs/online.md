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

## A permanent address (optional)

The free quick address changes every time you start it. For the same address every time you need
a free Cloudflare account and a domain on Cloudflare:

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
