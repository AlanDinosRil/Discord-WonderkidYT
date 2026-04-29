import re
import httpx
import json

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


async def fetch_tiktok_views(url: str) -> dict:
    """
    Ambil views dari URL TikTok video.
    Contoh URL: https://www.tiktok.com/@user/video/1234567890
    """
    try:
        # TikTok embed/oembed endpoint (no auth needed)
        oembed_url = f"https://www.tiktok.com/oembed?url={url}"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(oembed_url, headers=HEADERS)
            if r.status_code != 200:
                return {"success": False, "error": f"HTTP {r.status_code}"}
            data = r.json()
            title = data.get("title", "Unknown Title")
            author = data.get("author_name", "Unknown")
            thumbnail = data.get("thumbnail_url", "")

        # Scrape halaman video untuk views (oembed tidak return views)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            page = await client.get(url, headers=HEADERS)
            html = page.text

        # Cari views di JSON embedded di halaman
        views = 0
        patterns = [
            r'"playCount"\s*:\s*(\d+)',
            r'"play_count"\s*:\s*(\d+)',
            r'"viewCount"\s*:\s*"?(\d+)"?',
            r'\"stats\".*?\"playCount\":(\d+)',
        ]
        for p in patterns:
            m = re.search(p, html)
            if m:
                views = int(m.group(1))
                break

        return {
            "success": True,
            "platform": "tiktok",
            "title": title,
            "author": author,
            "views": views,
            "thumbnail": thumbnail,
            "url": url,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


async def fetch_youtube_views(url: str) -> dict:
    """
    Ambil views dari URL YouTube video.
    Contoh URL: https://youtu.be/xxxx atau https://youtube.com/watch?v=xxxx
    """
    try:
        # Normalisasi URL
        video_id = None
        patterns = [
            r"youtu\.be/([A-Za-z0-9_-]{11})",
            r"v=([A-Za-z0-9_-]{11})",
            r"shorts/([A-Za-z0-9_-]{11})",
        ]
        for p in patterns:
            m = re.search(p, url)
            if m:
                video_id = m.group(1)
                break

        if not video_id:
            return {"success": False, "error": "Video ID tidak ditemukan"}

        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(watch_url, headers=HEADERS)
            if r.status_code != 200:
                return {"success": False, "error": f"HTTP {r.status_code}"}
            html = r.text

        # Cari views dari ytInitialData
        views = 0
        title = "Unknown Title"
        author = "Unknown"
        thumbnail = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

        view_patterns = [
            r'"viewCount"\s*:\s*\{\s*"videoViewCountRenderer"\s*:\s*\{\s*"viewCount"\s*:\s*\{\s*"simpleText"\s*:\s*"([\d,\.]+)',
            r'"viewCount":"(\d+)"',
            r'"views":\{"simpleText":"([\d,\. ]+) views"',
        ]
        for p in view_patterns:
            m = re.search(p, html)
            if m:
                views_str = m.group(1).replace(",", "").replace(".", "").replace(" ", "")
                try:
                    views = int(views_str)
                    break
                except ValueError:
                    continue

        title_m = re.search(r'"title"\s*:\s*\{\s*"runs"\s*:\s*\[.*?"text"\s*:\s*"([^"]+)"', html)
        if title_m:
            title = title_m.group(1)

        author_m = re.search(r'"ownerChannelName"\s*:\s*"([^"]+)"', html)
        if author_m:
            author = author_m.group(1)

        return {
            "success": True,
            "platform": "youtube",
            "title": title,
            "author": author,
            "views": views,
            "thumbnail": thumbnail,
            "url": watch_url,
            "video_id": video_id,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


async def fetch_views(url: str) -> dict:
    """Auto-detect platform dan fetch views."""
    url = url.strip()
    if "tiktok.com" in url:
        return await fetch_tiktok_views(url)
    elif "youtu" in url:
        return await fetch_youtube_views(url)
    else:
        return {"success": False, "error": "Platform tidak dikenali. Gunakan link TikTok atau YouTube."}


def detect_platform(url: str) -> str:
    if "tiktok.com" in url:
        return "tiktok"
    elif "youtu" in url:
        return "youtube"
    return "unknown"
