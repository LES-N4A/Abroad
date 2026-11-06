"""
currency.py - converts prices from other currencies into TND.
1) It first tries to download LIVE rates (free service, no account needed).
2) If the internet or the service is down, it uses the approximate FALLBACK table below.
"""
import json, time, urllib.request

# TND you get for 1 unit of each currency (approximate, used only when live rates fail)
FALLBACK = {"TND": 1.0, "EUR": 3.36, "USD": 2.93, "GBP": 3.90, "TRY": 0.068,
            "AED": 0.80, "SAR": 0.78, "CAD": 2.15, "CNY": 0.41, "CHF": 3.60}
SUPPORTED = list(FALLBACK)
LIVE_URL = "https://open.er-api.com/v6/latest/TND"      # tells us: 1 TND = x EUR, x USD, ...

_cache = {"rates": dict(FALLBACK), "source": "approximate", "updated": None, "next_try": 0}

def get_rates():
    now = time.time()
    if now >= _cache["next_try"]:                       # refresh at most every 6 hours
        try:
            with urllib.request.urlopen(LIVE_URL, timeout=4) as r:
                data = json.load(r)
            rates = {}
            for cur in SUPPORTED:
                per_tnd = data["rates"][cur]            # 1 TND = per_tnd units of cur
                rates[cur] = 1 / per_tnd                # so 1 unit of cur = 1/per_tnd TND
            _cache.update(rates=rates, source="live", updated=time.strftime("%Y-%m-%d %H:%M"),
                          next_try=now + 6 * 3600)
        except Exception:                               # offline? keep old rates, retry in 10 minutes
            _cache["next_try"] = now + 600
    return {"rates": dict(_cache["rates"]), "source": _cache["source"], "updated": _cache["updated"],
            "currencies": SUPPORTED}
