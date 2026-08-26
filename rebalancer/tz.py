"""IST timezone -- Windows-safe.

Windows ke paas apna timezone database nahi hota. ZoneInfo("Asia/Kolkata")
wahan tabhi chalta hai jab `tzdata` package install ho. Agar nahi hai toh
ZoneInfoNotFoundError aata hai aur poori app import par hi mar jaati hai.

IST mein daylight saving nahi hoti, toh fixed +05:30 offset bilkul sahi
jawaab deta hai -- fallback se koi galat time nahi banta.
"""
from __future__ import annotations

from datetime import timedelta, timezone

try:                                        # pehli koshish: asli tz database
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:                           # Windows bina tzdata ke
    IST = timezone(timedelta(hours=5, minutes=30), "IST")
