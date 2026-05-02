"""
verify_clip.py — Cek apakah URL video milik username yang terdaftar.
Mengembalikan dict: { "match": bool, "found_username": str, "confidence": str }
"""

import re
import httpx
import urllib.parse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.6 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# Browser-like headers untuk oembed
OEMBED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "Accept": "application/json",
}


def normalize_username(u: str) -> str:
    if not u:
        return ""
    return u.lower().lstrip("@").replace("_", "").replace(".", "").replace("-", "").strip()


async def resolve_tiktok_url(url: str) -> tuple[str, str]:
    """
    Resolve short TikTok URLs (vm.tiktok.com, vt.tiktok.com) to full URL.
    Returns tuple: (final_url, found_username_from_url)
    """
    found_username = ""
    final_url = url
    
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            # Use GET instead of HEAD - some servers don't respond to HEAD properly
            r = await client.get(url, headers=HEADERS)
            final_url = str(r.url)
            
            # Try to find username from final URL
            m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)", final_url)
            if m:
                found_username = m.group(1)
            
            # Also check response body for redirected URLs
            if not found_username and r.text:
                # Sometimes the redirect URL is in JavaScript
                redirect_match = re.search(r'href="([^"]*tiktok\.com/@[A-Za-z0-9_.]+[^"]*)"', r.text)
                if redirect_match:
                    m2 = re.search(r"@([A-Za-z0-9_.]+)", redirect_match.group(1))
                    if m2:
                        found_username = m2.group(1)
                        
    except Exception as e:
        print(f"[verify_clip] resolve_tiktok_url error: {e}")
    
    return final_url, found_username


async def verify_tiktok(url: str, registered_username: str) -> dict:
    """
    Verifikasi apakah video TikTok benar-benar milik registered_username.
    Multiple fallback methods untuk reliability.
    """
    found_username = ""
    original_url = url
    debug_info = []
    
    print(f"[verify_clip] Verifying TikTok URL: {url}")
    print(f"[verify_clip] Registered username: {registered_username}")

    # Metode 1: Parse langsung dari URL jika sudah full URL
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)/(?:video|photo)", url)
    if m:
        found_username = m.group(1)
        debug_info.append(f"Method 1 (URL parse): @{found_username}")
        print(f"[verify_clip] Found from URL: @{found_username}")

    # Metode 2: Resolve short URLs
    if not found_username and ("vm.tiktok" in url or "vt.tiktok" in url or "/t/" in url or "tiktok.com/t/" in url):
        print(f"[verify_clip] Resolving short URL...")
        resolved_url, resolved_username = await resolve_tiktok_url(url)
        url = resolved_url
        if resolved_username:
            found_username = resolved_username
            debug_info.append(f"Method 2 (resolve): @{found_username}")
            print(f"[verify_clip] Found from resolved URL: @{found_username}")
        else:
            # Try parsing resolved URL
            m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)/(?:video|photo)", resolved_url)
            if m:
                found_username = m.group(1)
                debug_info.append(f"Method 2b (resolved URL parse): @{found_username}")
                print(f"[verify_clip] Found from resolved URL parse: @{found_username}")

    # Metode 3: oEmbed API (dengan Discord bot user agent untuk bypass)
    if not found_username:
        try:
            encoded_url = urllib.parse.quote(url, safe='')
            oembed_url = f"https://www.tiktok.com/oembed?url={encoded_url}"
            print(f"[verify_clip] Trying oembed: {oembed_url}")
            
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(oembed_url, headers=OEMBED_HEADERS)
                print(f"[verify_clip] oEmbed status: {r.status_code}")
                
                if r.status_code == 200:
                    data = r.json()
                    print(f"[verify_clip] oEmbed data keys: {list(data.keys())}")
                    
                    # author_unique_id adalah yang paling akurat
                    if data.get("author_unique_id"):
                        found_username = data["author_unique_id"]
                        debug_info.append(f"Method 3a (oembed author_unique_id): @{found_username}")
                    # author_url format: https://www.tiktok.com/@username
                    elif data.get("author_url"):
                        am = re.search(r"@([A-Za-z0-9_.]+)", data["author_url"])
                        if am:
                            found_username = am.group(1)
                            debug_info.append(f"Method 3b (oembed author_url): @{found_username}")
                    # author_name sebagai fallback (tapi ini bisa display name)
                    elif data.get("author_name"):
                        # Jika author_name cocok dengan registered_username, gunakan
                        if normalize_username(data["author_name"]) == normalize_username(registered_username):
                            found_username = data["author_name"]
                            debug_info.append(f"Method 3c (oembed author_name match): @{found_username}")
                    
                    if found_username:
                        print(f"[verify_clip] Found from oEmbed: @{found_username}")
        except Exception as e:
            print(f"[verify_clip] oEmbed error: {e}")

    # Metode 4: Fetch halaman video langsung dan parse HTML/JSON
    if not found_username:
        try:
            print(f"[verify_clip] Trying direct fetch: {url}")
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                r = await client.get(url, headers=HEADERS)
                html = r.text
                final_url = str(r.url)
                
                # Cek dari final URL setelah redirect
                m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)", final_url)
                if m:
                    found_username = m.group(1)
                    debug_info.append(f"Method 4a (final URL): @{found_username}")
                    print(f"[verify_clip] Found from final URL: @{found_username}")
                
                if not found_username:
                    # Cari dalam SIGI_STATE atau __UNIVERSAL_DATA_FOR_REHYDRATION__
                    patterns = [
                        r'"uniqueId"\s*:\s*"([^"]+)"',
                        r'"author"\s*:\s*\{[^}]*"uniqueId"\s*:\s*"([^"]+)"',
                        r'"nickname"\s*:\s*"([^"]+)".*?"uniqueId"\s*:\s*"([^"]+)"',
                        r'/@([A-Za-z0-9_.]+)/video/',
                        r'"authorUniqueId"\s*:\s*"([^"]+)"',
                    ]
                    
                    for pat in patterns:
                        match = re.search(pat, html)
                        if match:
                            # Ambil group terakhir (untuk pattern dengan multiple groups)
                            found_username = match.group(match.lastindex or 1)
                            debug_info.append(f"Method 4b (HTML pattern): @{found_username}")
                            print(f"[verify_clip] Found from HTML: @{found_username}")
                            break
                            
        except Exception as e:
            print(f"[verify_clip] Direct fetch error: {e}")

    # Metode 5: Jika username registered ada di URL (fuzzy match)
    if not found_username:
        reg_norm = normalize_username(registered_username)
        if reg_norm and reg_norm in url.lower():
            # Username ada di URL, kemungkinan besar cocok
            found_username = registered_username
            debug_info.append(f"Method 5 (URL contains username): @{found_username}")
            print(f"[verify_clip] Found via URL contains: @{found_username}")

    print(f"[verify_clip] Final result: found='{found_username}', registered='{registered_username}'")
    print(f"[verify_clip] Debug: {debug_info}")

    if not found_username:
        return {
            "match": False,
            "found_username": "",
            "confidence": "unknown",
            "reason": "Tidak bisa mendeteksi username dari URL/oembed",
            "debug": debug_info
        }

    match = normalize_username(found_username) == normalize_username(registered_username)
    return {
        "match": match,
        "found_username": found_username,
        "confidence": "high",
        "reason": "Cocok" if match else f"Username video: @{found_username}, terdaftar: @{registered_username}",
        "debug": debug_info
    }


async def verify_youtube(url: str, registered_username: str) -> dict:
    """
    Verifikasi apakah video YouTube benar-benar milik registered_username.
    Mengambil ownerChannelName dari halaman video.
    """
    found_username = ""

    # Extract video ID
    video_id = None
    for p in [r"youtu\.be/([A-Za-z0-9_-]{11})", r"v=([A-Za-z0-9_-]{11})", r"shorts/([A-Za-z0-9_-]{11})"]:
        m = re.search(p, url)
        if m:
            video_id = m.group(1)
            break

    if not video_id:
        return {"match": False, "found_username": "", "confidence": "unknown", "reason": "Video ID tidak ditemukan"}

    try:
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(watch_url, headers=HEADERS)
            html = r.text

        # Cari ownerChannelName atau channelName
        patterns = [
            r'"ownerChannelName"\s*:\s*"([^"]+)"',
            r'"channelName"\s*:\s*"([^"]+)"',
            r'"author"\s*:\s*"([^"]+)"',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                found_username = m.group(1)
                break

        # Cari handle @username
        handle_m = re.search(r'"canonicalBaseUrl"\s*:\s*"/@([^"]+)"', html)
        handle = handle_m.group(1) if handle_m else ""

    except Exception as e:
        return {"match": False, "found_username": "", "confidence": "unknown", "reason": str(e)}

    if not found_username and not handle:
        return {"match": False, "found_username": "", "confidence": "low", "reason": "Tidak bisa membaca channel name"}

    # Cek match dengan channel name atau handle
    reg_norm = normalize_username(registered_username)
    name_match = normalize_username(found_username) == reg_norm if found_username else False
    handle_match = normalize_username(handle) == reg_norm if handle else False

    match = name_match or handle_match
    display = found_username or handle

    return {
        "match": match,
        "found_username": display,
        "confidence": "high",
        "reason": "Cocok ✅" if match else f"Channel video: {display}, terdaftar: {registered_username}"
    }


async def verify_clip(url: str, registered_username: str, platform: str) -> dict:
    """Entry point verifikasi."""
    url = url.strip()
    if platform == "tiktok":
        return await verify_tiktok(url, registered_username)
    elif platform == "youtube":
        return await verify_youtube(url, registered_username)
    return {"match": False, "found_username": "", "confidence": "unknown", "reason": "Platform tidak dikenali"}
