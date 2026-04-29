import json
import os
from datetime import datetime, timezone

DB_FILE = "data.json"

DEFAULT_DB = {
    "clippers": {},
    "clips": [],
    "gaji_history": [],
    "warnings": {},        # discord_id -> list of warning dicts
    "blacklist": {},       # discord_id -> blacklist info
    "periode": {           # info periode aktif
        "aktif": False,
        "nama": "",
        "mulai": "",
        "selesai": "",
        "clips_periode": {},   # discord_id -> clip count in period
        "views_periode": {},   # discord_id -> views in period
    },
    "settings": {
        "clipper_channel_id": 0,
        "gaji_channel_id": 0,
        "rekap_channel_id": 0,
        "log_channel_id": 0,
        "admin_role_name": "Admin",
        "rekap_hari": 1,        # Senin = 0, Minggu = 6
        "periode_hari": 30,     # durasi periode default
    }
}

GAJI_TIERS = [
    {"min": 1_000_000, "gaji": 700_000, "label": "1.000.000+", "emoji": "💎"},
    {"min": 500_000,   "gaji": 300_000, "label": "500.000+",   "emoji": "🥇"},
    {"min": 300_000,   "gaji": 150_000, "label": "300.000+",   "emoji": "🥈"},
    {"min": 100_000,   "gaji": 50_000,  "label": "100.000+",   "emoji": "🥉"},
    {"min": 0,         "gaji": 0,       "label": "< 100.000",  "emoji": "⬜"},
]

KONSISTEN_TIERS = [
    {"min_clips": 20, "hadiah": 50_000, "label": "🥇 Super Konsisten"},
    {"min_clips": 10, "hadiah": 25_000, "label": "🥈 Konsisten"},
]

MAX_WARNINGS = 3  # auto-blacklist setelah ini


def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        for key, val in DEFAULT_DB.items():
            if key not in db:
                db[key] = val
        # Pastikan sub-keys settings ada
        for k, v in DEFAULT_DB["settings"].items():
            if k not in db["settings"]:
                db["settings"][k] = v
        return db
    db = {}
    for k, v in DEFAULT_DB.items():
        db[k] = json.loads(json.dumps(v))
    return db


def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── CLIPPER ──────────────────────────────────────────────────────────────────

def get_clipper(db, discord_id: str):
    return db["clippers"].get(str(discord_id))


def register_clipper(db, discord_id: str, username: str, platform: str, display_name: str):
    db["clippers"][str(discord_id)] = {
        "discord_id": str(discord_id),
        "display_name": display_name,
        "username": username,
        "platform": platform,
        "joined_at": now_iso(),
        "total_views": 0,
        "total_gaji": 0,
        "total_clips": 0,
        "pending_gaji": 0,
        "active": True,
    }
    save_db(db)


# ── GAJI ─────────────────────────────────────────────────────────────────────

def calc_gaji(views: int) -> int:
    for tier in GAJI_TIERS:
        if views >= tier["min"]:
            return tier["gaji"]
    return 0


def get_tier(views: int) -> dict:
    for tier in GAJI_TIERS:
        if views >= tier["min"]:
            return tier
    return GAJI_TIERS[-1]


def calc_konsisten_hadiah(clip_count: int) -> dict | None:
    for tier in KONSISTEN_TIERS:
        if clip_count >= tier["min_clips"]:
            return tier
    return None


# ── LEADERBOARD ───────────────────────────────────────────────────────────────

def get_leaderboard(db, top_n=10):
    aktif = [c for c in db["clippers"].values() if c.get("active", True)]
    return sorted(aktif, key=lambda x: x["total_views"], reverse=True)[:top_n]


def get_konsisten_leaderboard(db, top_n=10):
    aktif = [c for c in db["clippers"].values() if c.get("active", True)]
    return sorted(aktif, key=lambda x: x["total_clips"], reverse=True)[:top_n]


def get_periode_leaderboard(db, top_n=10):
    periode = db.get("periode", {})
    views_p = periode.get("views_periode", {})
    clips_p = periode.get("clips_periode", {})
    result = []
    for did, clipper in db["clippers"].items():
        if not clipper.get("active", True):
            continue
        result.append({
            **clipper,
            "periode_views": views_p.get(did, 0),
            "periode_clips": clips_p.get(did, 0),
        })
    return sorted(result, key=lambda x: x["periode_views"], reverse=True)[:top_n]


# ── WARNING & BLACKLIST ───────────────────────────────────────────────────────

def add_warning(db, discord_id: str, alasan: str, by: str) -> int:
    did = str(discord_id)
    if did not in db["warnings"]:
        db["warnings"][did] = []
    db["warnings"][did].append({
        "alasan": alasan,
        "by": by,
        "at": now_iso(),
    })
    count = len(db["warnings"][did])
    if count >= MAX_WARNINGS:
        blacklist_clipper(db, did, f"Auto-blacklist setelah {MAX_WARNINGS} warning", by)
    save_db(db)
    return count


def get_warnings(db, discord_id: str) -> list:
    return db["warnings"].get(str(discord_id), [])


def clear_warnings(db, discord_id: str):
    db["warnings"][str(discord_id)] = []
    save_db(db)


def blacklist_clipper(db, discord_id: str, alasan: str, by: str):
    did = str(discord_id)
    db["blacklist"][did] = {
        "alasan": alasan,
        "by": by,
        "at": now_iso(),
    }
    if did in db["clippers"]:
        db["clippers"][did]["active"] = False
    save_db(db)


def unblacklist_clipper(db, discord_id: str):
    did = str(discord_id)
    db["blacklist"].pop(did, None)
    db["warnings"].pop(did, None)
    if did in db["clippers"]:
        db["clippers"][did]["active"] = True
    save_db(db)


def is_blacklisted(db, discord_id: str) -> bool:
    return str(discord_id) in db["blacklist"]


# ── PERIODE ───────────────────────────────────────────────────────────────────

def buka_periode(db, nama: str, durasi_hari: int):
    from datetime import timedelta
    mulai = datetime.now(timezone.utc)
    selesai = mulai + timedelta(days=durasi_hari)
    db["periode"] = {
        "aktif": True,
        "nama": nama,
        "mulai": mulai.isoformat(),
        "selesai": selesai.isoformat(),
        "clips_periode": {},
        "views_periode": {},
    }
    save_db(db)


def tutup_periode(db):
    db["periode"]["aktif"] = False
    save_db(db)


def periode_aktif(db) -> bool:
    return db.get("periode", {}).get("aktif", False)


def tambah_periode_stats(db, discord_id: str, views: int):
    did = str(discord_id)
    p = db["periode"]
    p["clips_periode"][did] = p["clips_periode"].get(did, 0) + 1
    p["views_periode"][did] = p["views_periode"].get(did, 0) + views


# ── URL DUPLICATE CHECK ───────────────────────────────────────────────────────

def is_duplicate_url(db, url: str) -> bool:
    url_clean = url.strip().rstrip("/").lower()
    for c in db["clips"]:
        existing = c.get("url", "").strip().rstrip("/").lower()
        if existing == url_clean:
            return True
    return False
