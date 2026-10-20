# scounter

A cookies-only, full-HD Instagram archiver. Downloads posts, reels, stories, highlights, tagged posts and the profile picture from a profile, and can find any downloaded file's original Instagram URL.

> Use only on profiles you're authorized to access, with a burner account. You are responsible for complying with Instagram's Terms of Service and local law.

## Install

```powershell
cd C:\scounter
pip install -r requirements.txt
pip install -e ".[tor]"
```

## Get your cookies

Log into Instagram in your browser → press **F12** → **Application** → **Cookies** → `https://www.instagram.com`. Copy the values of these five cookies: `sessionid`, `ds_user_id`, `mid`, `ig_did`, `rur`.

## Run (with Tor + rotation)

Open the **Tor Browser** and leave it running (it uses ports 9150/9151).

```powershell
$env:SCOUNTER_TOR_SOCKS_PORT=9150
$env:SCOUNTER_TOR_CONTROL_PORT=9151
$env:SCOUNTER_COOKIES='sessionid=PASTE; ds_user_id=PASTE; mid=PASTE; ig_did=PASTE; rur=PASTE'
python -m scounter collect <target> --everything --tor --tor-rotate 5
```

Replace `<target>` with the username, and `PASTE` with each cookie value (keep the single quotes).

## Resume

```powershell
python -m scounter collect <target> --everything --tor --tor-rotate 5 --fast-update
```

## Find a post's Instagram URL

```powershell
python -m scounter link ABC123xyz                       # by shortcode
python -m scounter link ABC123xyz_20240601_123045.jpg   # by downloaded filename
python -m scounter link 2024-06-01                      # by date
```

## Exit codes

`0` success · `2` soft errors · `3` cookies rejected (re-copy them) · `130` interrupted.

## Output

Everything lands in `output\<target>\` — media in `media/`, plus `manifest.db`, `report.md`, and `collection.log`.
