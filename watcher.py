"""Car watcher: checks Marktplaats (category Auto's) for new car ads and sends them to Telegram."""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

SEARCHES_FILE = "searches.json"
SEEN_FILE = "seen.json"
MAX_REMEMBERED = 500
API = "https://www.marktplaats.nl/lrp/api/search"
CARS_CATEGORY = 91  # Marktplaats category "Auto's"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept": "application/json",
}


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets are missing (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return False
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception as e:
        print("Telegram error:", e)
        return False


def number(value):
    """Turn things like '45.000 km' or '2019' into a whole number."""
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def attributes(listing):
    out = {}
    for a in listing.get("attributes", []) + listing.get("extendedAttributes", []):
        if a.get("key") and a.get("value") is not None:
            out[a["key"].lower()] = a["value"]
    return out


def first(attrs, *keys):
    for k in keys:
        if k in attrs:
            return attrs[k]
    return None


def describe(listing):
    attrs = attributes(listing)
    price_cents = (listing.get("priceInfo") or {}).get("priceCents")
    location = listing.get("location") or {}
    return {
        "price": price_cents / 100 if price_cents else None,
        "year": number(first(attrs, "constructionyear", "year", "bouwjaar")),
        "km": number(first(attrs, "mileage", "kilometerstand")),
        "fuel": first(attrs, "fuel", "brandstof"),
        "city": location.get("cityName"),
        "distance_km": round(location["distanceMeters"] / 1000) if location.get("distanceMeters", -1) >= 0 else None,
    }


def matches(info, s):
    """Ads with a missing detail are kept, so you never miss a car just because the seller left a field empty."""
    if s.get("min_price") and info["price"] and info["price"] < s["min_price"]:
        return False
    if s.get("max_price") and info["price"] and info["price"] > s["max_price"]:
        return False
    if s.get("min_year") and info["year"] and info["year"] < s["min_year"]:
        return False
    if s.get("max_mileage_km") and info["km"] and info["km"] > s["max_mileage_km"]:
        return False
    if s.get("fuel") and info["fuel"]:
        wanted = [f.lower() for f in s["fuel"]]
        if not any(w in str(info["fuel"]).lower() for w in wanted):
            return False
    if s.get("max_distance_km") and info["distance_km"] is not None and info["distance_km"] > s["max_distance_km"]:
        return False
    return True


def message(listing, info):
    parts = []
    parts.append(f"€ {info['price']:,.0f}".replace(",", ".") if info["price"] else "Prijs: bieden/op aanvraag")
    if info["year"]:
        parts.append(str(info["year"]))
    if info["km"] is not None:
        parts.append(f"{info['km']:,} km".replace(",", "."))
    if info["fuel"]:
        parts.append(str(info["fuel"]))
    if info["city"]:
        parts.append(info["city"] + (f" ({info['distance_km']} km)" if info["distance_km"] is not None else ""))
    url = "https://www.marktplaats.nl" + listing.get("vipUrl", "")
    return f"🚗 {listing.get('title', 'Nieuwe auto')}\n" + " · ".join(parts) + f"\n{url}"


def search_url(s):
    params = {
        "query": s["query"],
        "l1CategoryId": CARS_CATEGORY,
        "limit": 50,
        "offset": 0,
        "sortBy": "SORT_INDEX",
        "sortOrder": "DECREASING",  # newest first
    }
    if s.get("postcode") and s.get("max_distance_km"):
        params["postcode"] = s["postcode"].replace(" ", "")
        params["distanceMeters"] = int(s["max_distance_km"] * 1000)
    return API + "?" + urllib.parse.urlencode(params)


def main():
    if os.environ.get("TEST_MESSAGE", "").lower() == "true":
        ok = send_telegram("✅ Test from your car watcher. Telegram alerts are working!")
        print("Test message sent." if ok else "Test message FAILED.")
        sys.exit(0 if ok else 1)

    with open(SEARCHES_FILE, encoding="utf-8") as f:
        searches = json.load(f)
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        seen = {}

    failures = 0
    for s in searches:
        key = s.get("name") or s["query"]
        print(f"Searching: {key}")
        try:
            listings = get_json(search_url(s)).get("listings", [])
        except Exception as e:
            print("  Search failed:", e)
            failures += 1
            continue

        # Only real Marktplaats ads (ids start with "m"), not paid ads from other sites.
        listings = [l for l in listings if str(l.get("itemId", "")).startswith("m")]
        first_run = key not in seen
        already = set(seen.get(key, []))
        new_ids = []
        sent = 0
        for listing in listings:
            item_id = listing["itemId"]
            if item_id in already:
                continue
            new_ids.append(item_id)
            if first_run:
                continue
            info = describe(listing)
            if matches(info, s) and send_telegram(message(listing, info)):
                sent += 1

        if first_run:
            print(f"  First run: remembered {len(new_ids)} existing ads, no alerts sent.")
        else:
            print(f"  {len(new_ids)} new ads, {sent} matched your filters and were sent.")
        seen[key] = (new_ids + seen.get(key, []))[:MAX_REMEMBERED]

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=1)

    if searches and failures == len(searches):
        print("All searches failed - Marktplaats may be blocking the request.")
        sys.exit(1)


if __name__ == "__main__":
    main()
