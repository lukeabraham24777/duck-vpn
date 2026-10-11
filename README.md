# 🦆 duck-vpn

Free geo-unblocking for Windows, macOS and iPhone.

duck-vpn finds a free [VPN Gate](https://www.vpngate.net) server in the country you pick and
**tests that the site you want actually loads through it** before handing it to you. It tries
servers until one works and remembers the winners, so the next run is quick.

- **Free:** VPN Gate is a volunteer relay network run by the University of Tsukuba. No
  accounts, no servers to rent.
- **Smart ordering:** servers on home internet connections are tried first, because streaming
  sites block datacentre and known-VPN IPs. The check uses free IP-reputation data from
  ip-api.com.
- **Real test:** it connects, checks the exit country, loads your site and looks for "not
  available in your region"-style pages.

> **Know the limits.** Nothing free can guarantee unblocking: big streaming services keep
> blocklists, and VPN Gate's list is public. Expect some trial and error. Volunteer operators
> can see unencrypted traffic and VPN Gate keeps connection logs, so use this for unblocking
> content, **not** for privacy, banking or logins you care about. Getting around geo-blocks
> usually breaks a service's terms of use, and accounts can be suspended.

## Windows and macOS: the `duck-vpn` command

### Install

**macOS**
```
brew install openvpn pipx
pipx ensurepath          # then close and reopen Terminal
pipx install git+https://github.com/lukeabraham24777/duck-vpn
```

**Windows**: install [Python 3.9+](https://www.python.org/downloads/) and the
[OpenVPN community client](https://openvpn.net/community-downloads/), then:
```
pip install git+https://github.com/lukeabraham24777/duck-vpn
```

To update later: `pipx upgrade duck-vpn` (macOS) or re-run the `pip install` with `--upgrade`
(Windows).

OpenVPN needs admin rights to reroute traffic. On **Windows**, run your terminal as
Administrator. On **macOS**, duck-vpn asks for your password.

### Use

```
duck-vpn countries                         # which countries have servers right now
duck-vpn list japan                        # ranked servers: home IPs first

duck-vpn connect jp                        # connect to the best working server in Japan
duck-vpn connect us --test https://www.example.com/show/123
duck-vpn connect uk --test https://site.example --expect "Play"   # page must contain "Play"
duck-vpn connect kr --home-only            # only try home-IP servers
```

Once connected, duck-vpn waits for you:

```
[1/15] public-vpn-213 219.100.37.xx (flagged IP) ... site blocked: page says "not available in your country"
[2/15] vpn751293 126.75.xx.xx (home IP) ... OK, exit IP 126.75.xx.xx

Connected through Japan. Check your site in the browser now.
  Enter      disconnect and quit
  n + Enter  this one doesn't work, try the next server
```

Some services (Netflix, iPlayer, Disney+) only show the block after you log in or press play,
which the automatic test can't see. Check in your browser and type `n` to move on if it's
blocked. Servers you reject are skipped for 24 hours.

Other options: `--max-tries N` (default 15), `--timeout SECONDS` (default 25), `--reject TEXT`
(extra "blocked" wording, repeatable), `-v` (show OpenVPN output), `--refresh` (re-download the
list now; otherwise it's cached for 15 minutes).

## macOS menu bar app

```
duck-vpn menubar                 # starts it in the background; a duck-with-shield icon appears in the menu bar
duck-vpn menubar --autostart on  # also start it when you log in
```

| Menu item | What it does |
|---|---|
| **Turn On / Turn Off** (⌘T) | Finds a working server in your chosen country and connects. The country code (e.g. `JP`) appears next to the icon while connected. |
| **Try Another Server** | Use this when your site is still blocked on the current server. |
| **Country ▸** | Pick a country. Changing it while connected switches straight over. |
| **Home IPs Only** | Only try servers on home connections (best for streaming, fewer choices). |
| **Test Site…** | Optional page that must load before a server counts as working. |
| **Send Server to iPhone…** | Saves the current server to Downloads and shows it in Finder, ready to AirDrop. |

The first time you turn it on, macOS shows its standard password dialog. That starts a small
helper with admin rights that only starts and stops OpenVPN, and it quits when you quit the
app. Turning the VPN off and on again doesn't ask again until you restart the app.

## iPhone

An iPhone can't run duck-vpn's automatic testing: apps that control the VPN need a paid Apple
developer account. You get two free alternatives:

**A. The web picker (no computer needed).** A GitHub Action rebuilds a phone-friendly server
list every 2 hours and publishes it on GitHub Pages:

1. Install **OpenVPN Connect** from the App Store (free).
2. Open your picker page (`https://lukeabraham24777.github.io/duck-vpn/`), choose a
   country and tap **Get** on a server marked *Home IP*.
3. Open the file with OpenVPN Connect, tap **Add**, then connect.
4. If it won't connect, go to OpenVPN Connect **Settings → Security Level → Insecure** (VPN Gate
   relays use older ciphers), or try the next server.

**B. Hand over a tested server from your computer.** After `duck-vpn connect` finds a working
server:

```
duck-vpn export --last          # writes the .ovpn file that just worked
duck-vpn export japan --count 3 # or the top 3 servers for a country
```

AirDrop the file to your iPhone and open it in OpenVPN Connect. The same files work in
Tunnelblick (macOS) and OpenVPN GUI (Windows) if you prefer a GUI.

### Setting up the web picker (one time)

1. Merge this code into `main`.
2. In the repo, go to **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. Go to **Actions → Publish server picker → Run workflow**.

GitHub Pages is free for **public** repos. On a private repo it needs a paid plan, so either
make the repo public or host the `site/` folder elsewhere for free (for example, Cloudflare
Pages). The page only lists public VPN Gate data, so nothing personal is published.

## How it works

```
VPN Gate list ──► rank (worked before → home IP → unknown → flagged; then score)
                    │
                    ▼   for each server
            start OpenVPN ──► exit country right? ──► test site loads? ──► keep it
                 │ fail              │ no                    │ blocked
                 └───────────────────┴───────────────────────┴──► mark bad, try next
```

| File | Purpose |
|---|---|
| `duckvpn/vpngate.py` | Download, parse, cache and rank the VPN Gate list |
| `duckvpn/reputation.py` | Home vs datacentre/proxy flags from ip-api.com (cached 24 h) |
| `duckvpn/openvpn.py` | Run OpenVPN with admin rights; clean shutdown through its management port |
| `duckvpn/probe.py` | Exit-country check and site test |
| `duckvpn/ovpn.py` | Allow-lists config directives (no scripts or plugins as root) and enables VPN Gate's older ciphers |
| `duckvpn/state.py` | Remembers good and bad servers |
| `duckvpn/session.py` | The try-servers-until-one-works loop shared by the CLI and the app |
| `duckvpn/controller.py`, `menubar.py` | Menu bar app logic and UI ([rumps](https://github.com/jaredks/rumps)) |
| `duckvpn/helper.py` | Admin helper for the app: only starts and stops OpenVPN |
| `scripts/build_site.py` + `web/` | The iPhone web picker |

State and cache live in `%APPDATA%\duck-vpn` (Windows) or
`~/Library/Application Support/duck-vpn` (macOS). Set `DUCKVPN_HOME` to change this.
`DUCKVPN_OPENVPN` points to a custom OpenVPN binary, and `DUCKVPN_LIST_URL` to a VPN Gate
mirror if the main site is blocked where you are.

## Development

```
python -m unittest discover -s tests -v
```

The CLI has no third-party dependencies; the menu bar app uses rumps (installed automatically on macOS). CI runs the tests on Windows, macOS and Linux.
