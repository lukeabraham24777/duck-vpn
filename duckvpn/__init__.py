"""duck-vpn: find a free VPN Gate server that actually unblocks the site you want."""

__version__ = "0.1.0"

# Plain identifier for API calls; a browser-like UA is used when testing sites.
USER_AGENT = f"duck-vpn/{__version__}"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
