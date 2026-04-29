"""
verify_clip.py — Cek apakah URL video milik username yang terdaftar.
Mengembalikan dict: { "match": bool, "found_username": str, "confidence": str }
"""

import re
import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_username(u: str) -> str:
    return u.lower().lstrip("@").replace("_", "").replace(".", "").replace("-", "").strip()


async def verify_tiktok(url: str, registered_username: str) -> dict:
    """
    Verifikasi apakah video TikTok benar-benar milik registered_username.
    Metode 1: cek dari URL path (@username/video/xxx)
    Metode 2: cek dari oembed author_name
    """
    found_username = ""

    # Metode 1: parse dari URL
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)/video", url)
    if m:
        found_username = m.group(1)

    # Metode 2: fallback oembed
    if not found_username:
        try:
            oembed_url = f"https://www.tiktok.com/oembed?url={url}"
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(oembed_url, headers=HEADERS)
                if r.status_code == 200:
                    data = r.json()
                    author = data.get("author_name") or data.get("author_url", "")
                    # author_url biasanya https://www.tiktok.com/@username
                    am = re.search(r"@([A-Za-z0-9_.]+)", author)
                    if am:
                        found_username = am.group(1)
        except Exception:
            pass

    if not found_username:
        return {
            "match": False,
            "found_username": "",
            "confidence": "unknown",
            "reason": "Tidak bisa mendeteksi username dari URL/oembed"
        }

    match = normalize_username(found_username) == normalize_username(registered_username)
    return {
        "match": match,
        "found_username": found_username,
        "confidence": "high",
        "reason": "Cocok ✅" if match else f"Username video: @{found_username}, terdaftar: @{registered_username}"
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
