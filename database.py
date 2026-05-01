"""
database.py — MongoDB Atlas backend
Drop-in replacement untuk data.json — semua fungsi sama persis.
Bot.py tidak perlu diubah apapun.

Setup:
1. Buat cluster gratis di https://mongodb.com/atlas
2. Set env variable: MONGO_URI = mongodb+srv://user:pass@cluster.mongodb.net/clipperbot
"""

import json
import os
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient

# ── CONFIG ────────────────────────────────────────────────────────────────────

MONGO_URI = os.environ.get("MONGO_URI", "")
DB_NAME = "clipperbot"
USE_JSON_FALLBACK = not MONGO_URI
JSON_FALLBACK_FILE = "data.json"

# ── KONSTAN ───────────────────────────────────────────────────────────────────

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

MAX_WARNINGS = 3

DEFAULT_SETTINGS = {
    "clipper_channel_id": 0,
    "gaji_channel_id": 0,
    "rekap_channel_id": 0,
    "log_channel_id": 0,
    "ticket_channel_id": 0,
    "approval_channel_id": 0,
    "clip_role_name": "Clip",
    "admin_role_name": "Admin",
    "rekap_hari": 1,
    "periode_hari": 30,
    "custom_gaji_tiers": None,      # None = pakai default GAJI_TIERS
    "custom_konsisten_tiers": None, # None = pakai default KONSISTEN_TIERS
}

DEFAULT_PERIODE = {
    "aktif": False, "nama": "", "mulai": "", "selesai": "",
    "clips_periode": {}, "views_periode": {},
}

# ── MONGODB CLIENT ────────────────────────────────────────────────────────────

_client = None
_mongo_db = None

def _get_mongo():
    global _client, _mongo_db
    if _client is None:
        _client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_db = _client[DB_NAME]
    return _mongo_db

def _col(name):
    return _get_mongo()[name]

def _strip(doc):
    if doc and "_id" in doc:
        doc = dict(doc)
        del doc["_id"]
    return doc

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# ══════════════════════════════════════════════════════════════════════════════
# LOAD / SAVE — API sama persis dengan versi JSON lama
# ══════════════════════════════════════════════════════════════════════════════

def load_db() -> dict:
    if USE_JSON_FALLBACK:
        return _load_json()
    try:
        m = _get_mongo()

        clippers = {d["discord_id"]: _strip(d) for d in m["clippers"].find()}
        pending_registrations = {d["discord_id"]: _strip(d) for d in m["pending_registrations"].find()}
        clips = [_strip(d) for d in m["clips"].find().sort("id", 1)]
        gaji_history = [_strip(d) for d in m["gaji_history"].find()]
        warnings = {d["discord_id"]: _strip(d).get("list", []) for d in m["warnings"].find()}
        blacklist = {}
        for doc in m["blacklist"].find():
            d = _strip(doc)
            did = d.pop("discord_id")
            blacklist[did] = d
        tickets = {str(d["id"]): _strip(d) for d in m["tickets"].find()}

        meta_tc = m["meta"].find_one({"_key": "ticket_counter"}) or {}
        ticket_counter = meta_tc.get("value", 0)

        meta_p = m["meta"].find_one({"_key": "periode"}) or {}
        periode = meta_p.get("value", json.loads(json.dumps(DEFAULT_PERIODE)))

        meta_s = m["meta"].find_one({"_key": "settings"}) or {}
        settings = {**DEFAULT_SETTINGS, **meta_s.get("value", {})}

        return {
            "clippers": clippers,
            "pending_registrations": pending_registrations,
            "clips": clips,
            "gaji_history": gaji_history,
            "warnings": warnings,
            "blacklist": blacklist,
            "tickets": tickets,
            "ticket_counter": ticket_counter,
            "periode": periode,
            "settings": settings,
        }
    except Exception as e:
        print(f"[MongoDB] load_db error: {e} — fallback JSON")
        return _load_json()


def save_db(db: dict):
    if USE_JSON_FALLBACK:
        _save_json(db)
        return
    try:
        m = _get_mongo()

        for did, data in db.get("clippers", {}).items():
            m["clippers"].replace_one({"discord_id": did}, {**data, "discord_id": did}, upsert=True)

        # pending: hapus yang tidak ada lagi
        current_dids = list(db.get("pending_registrations", {}).keys())
        if current_dids:
            m["pending_registrations"].delete_many({"discord_id": {"$nin": current_dids}})
        else:
            m["pending_registrations"].delete_many({})
        for did, data in db.get("pending_registrations", {}).items():
            m["pending_registrations"].replace_one({"discord_id": did}, {**data, "discord_id": did}, upsert=True)

        for clip in db.get("clips", []):
            m["clips"].replace_one({"id": clip["id"]}, clip, upsert=True)

        for h in db.get("gaji_history", []):
            if "paid_at" in h:
                m["gaji_history"].replace_one(
                    {"discord_id": h["discord_id"], "paid_at": h["paid_at"]}, h, upsert=True
                )

        for did, wlist in db.get("warnings", {}).items():
            m["warnings"].replace_one({"discord_id": did}, {"discord_id": did, "list": wlist}, upsert=True)

        bl_dids = list(db.get("blacklist", {}).keys())
        m["blacklist"].delete_many({"discord_id": {"$nin": bl_dids}} if bl_dids else {})
        for did, info in db.get("blacklist", {}).items():
            m["blacklist"].replace_one({"discord_id": did}, {"discord_id": did, **info}, upsert=True)

        for tid, ticket in db.get("tickets", {}).items():
            m["tickets"].replace_one({"id": ticket["id"]}, ticket, upsert=True)

        m["meta"].replace_one({"_key": "ticket_counter"}, {"_key": "ticket_counter", "value": db.get("ticket_counter", 0)}, upsert=True)
        m["meta"].replace_one({"_key": "periode"}, {"_key": "periode", "value": db.get("periode", DEFAULT_PERIODE)}, upsert=True)
        m["meta"].replace_one({"_key": "settings"}, {"_key": "settings", "value": db.get("settings", DEFAULT_SETTINGS)}, upsert=True)

    except Exception as e:
        print(f"[MongoDB] save_db error: {e} — fallback JSON")
        _save_json(db)


def _load_json() -> dict:
    DEFAULT = {
        "clippers": {}, "pending_registrations": {}, "clips": [],
        "gaji_history": [], "warnings": {}, "blacklist": {},
        "tickets": {}, "ticket_counter": 0,
        "periode": json.loads(json.dumps(DEFAULT_PERIODE)),
        "settings": json.loads(json.dumps(DEFAULT_SETTINGS)),
    }
    if os.path.exists(JSON_FALLBACK_FILE):
        with open(JSON_FALLBACK_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        for k, v in DEFAULT.items():
            if k not in db:
                db[k] = json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v
        for k, v in DEFAULT_SETTINGS.items():
            if k not in db["settings"]:
                db["settings"][k] = v
        # Migrate format lama
        for did, clipper in db["clippers"].items():
            if "accounts" not in clipper:
                db["clippers"][did]["accounts"] = [{
                    "id": 1, "username": clipper.get("username", ""),
                    "platform": clipper.get("platform", "tiktok"),
                    "added_at": clipper.get("joined_at", now_iso()), "active": True,
                }]
        return db
    return json.loads(json.dumps(DEFAULT))


def _save_json(db: dict):
    with open(JSON_FALLBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# SEMUA FUNGSI DI BAWAH IDENTIK DENGAN VERSI LAMA
# ══════════════════════════════════════════════════════════════════════════════

def get_clipper(db, discord_id: str):
    return db["clippers"].get(str(discord_id))

def get_clipper_account(db, discord_id: str, account_id: int):
    clipper = get_clipper(db, discord_id)
    if not clipper: return None
    return next((a for a in clipper.get("accounts", []) if a["id"] == account_id), None)

def get_clipper_account_by_username(db, discord_id: str, username: str, platform: str):
    clipper = get_clipper(db, discord_id)
    if not clipper: return None
    u = username.lower().lstrip("@")
    return next((a for a in clipper.get("accounts", []) if a["username"].lower() == u and a["platform"] == platform), None)

def get_all_accounts(db, discord_id: str) -> list:
    clipper = get_clipper(db, discord_id)
    return clipper.get("accounts", []) if clipper else []

def register_clipper(db, discord_id: str, username: str, platform: str, display_name: str):
    db["clippers"][str(discord_id)] = {
        "discord_id": str(discord_id), "display_name": display_name,
        "accounts": [{"id": 1, "username": username.lstrip("@"), "platform": platform, "added_at": now_iso(), "active": True}],
        "joined_at": now_iso(), "total_views": 0, "total_gaji": 0, "total_clips": 0, "pending_gaji": 0, "active": True,
    }
    save_db(db)

def add_clipper_account(db, discord_id: str, username: str, platform: str) -> dict:
    did = str(discord_id)
    if did not in db["clippers"]: return None
    accounts = db["clippers"][did].get("accounts", [])
    new_id = max([a["id"] for a in accounts], default=0) + 1
    new_acc = {"id": new_id, "username": username.lstrip("@"), "platform": platform, "added_at": now_iso(), "active": True}
    db["clippers"][did]["accounts"].append(new_acc)
    save_db(db)
    return new_acc

def remove_clipper_account(db, discord_id: str, account_id: int) -> bool:
    did = str(discord_id)
    if did not in db["clippers"]: return False
    old = db["clippers"][did].get("accounts", [])
    new = [a for a in old if a["id"] != account_id]
    if len(new) == len(old): return False
    db["clippers"][did]["accounts"] = new
    save_db(db)
    return True

def add_pending_registration(db, discord_id: str, username: str, platform: str, display_name: str) -> int:
    did = str(discord_id)
    if did not in db["pending_registrations"]:
        db["pending_registrations"][did] = {"discord_id": did, "display_name": display_name, "accounts": [], "requested_at": now_iso()}
    accs = db["pending_registrations"][did]["accounts"]
    new_id = max([a.get("id", 0) for a in accs], default=0) + 1
    db["pending_registrations"][did]["accounts"].append({"id": new_id, "username": username.lstrip("@"), "platform": platform, "requested_at": now_iso()})
    db["pending_registrations"][did]["display_name"] = display_name
    save_db(db)
    return new_id

def get_pending_registration(db, discord_id: str):
    return db["pending_registrations"].get(str(discord_id))

def get_all_pending_registrations(db) -> list:
    return list(db["pending_registrations"].values())

def approve_registration(db, discord_id: str) -> dict:
    did = str(discord_id)
    pending = db["pending_registrations"].get(did)
    if not pending: return None
    if get_clipper(db, did):
        for acc in pending["accounts"]:
            add_clipper_account(db, did, acc["username"], acc["platform"])
    else:
        db["clippers"][did] = {
            "discord_id": did, "display_name": pending["display_name"], "accounts": [],
            "joined_at": now_iso(), "total_views": 0, "total_gaji": 0, "total_clips": 0, "pending_gaji": 0, "active": True,
        }
        for i, acc in enumerate(pending["accounts"], 1):
            db["clippers"][did]["accounts"].append({"id": i, "username": acc["username"], "platform": acc["platform"], "added_at": now_iso(), "active": True})
    del db["pending_registrations"][did]
    save_db(db)
    return db["clippers"][did]

def reject_registration(db, discord_id: str, reason: str = "") -> dict:
    did = str(discord_id)
    pending = db["pending_registrations"].get(did)
    if not pending: return None
    rejected = {**pending, "rejected_reason": reason, "rejected_at": now_iso()}
    del db["pending_registrations"][did]
    save_db(db)
    return rejected

def cancel_pending_account(db, discord_id: str, account_id: int) -> bool:
    did = str(discord_id)
    pending = db["pending_registrations"].get(did)
    if not pending: return False
    new_accs = [a for a in pending["accounts"] if a.get("id") != account_id]
    if len(new_accs) == len(pending["accounts"]): return False
    if not new_accs:
        del db["pending_registrations"][did]
    else:
        db["pending_registrations"][did]["accounts"] = new_accs
    save_db(db)
    return True

def create_ticket(db, discord_id: str, display_name: str, data: dict) -> dict:
    db["ticket_counter"] = db.get("ticket_counter", 0) + 1
    tid = db["ticket_counter"]
    ticket = {
        "id": tid, "discord_id": str(discord_id), "display_name": display_name,
        "bank_name": data.get("bank_name", ""), "account_number": data.get("account_number", ""),
        "account_holder": data.get("account_holder", ""), "phone_number": data.get("phone_number", ""),
        "notes": data.get("notes", ""), "status": "open",
        "created_at": now_iso(), "updated_at": now_iso(), "processed_by": None, "completed_at": None,
    }
    db["tickets"][str(tid)] = ticket
    save_db(db)
    return ticket

def get_ticket(db, ticket_id: int):
    return db["tickets"].get(str(ticket_id))

def get_user_tickets(db, discord_id: str) -> list:
    return [t for t in db["tickets"].values() if t["discord_id"] == str(discord_id)]

def get_open_tickets(db) -> list:
    return [t for t in db["tickets"].values() if t["status"] in ("open", "processing")]

def update_ticket_status(db, ticket_id: int, status: str, processed_by: str = None) -> dict:
    tid = str(ticket_id)
    if tid not in db["tickets"]: return None
    db["tickets"][tid].update({"status": status, "updated_at": now_iso()})
    if processed_by: db["tickets"][tid]["processed_by"] = processed_by
    if status == "completed": db["tickets"][tid]["completed_at"] = now_iso()
    save_db(db)
    return db["tickets"][tid]

def update_ticket_data(db, ticket_id: int, data: dict) -> dict:
    tid = str(ticket_id)
    if tid not in db["tickets"]: return None
    for key in ["bank_name", "account_number", "account_holder", "phone_number", "notes"]:
        if key in data: db["tickets"][tid][key] = data[key]
    db["tickets"][tid]["updated_at"] = now_iso()
    save_db(db)
    return db["tickets"][tid]

def get_active_gaji_tiers(db=None) -> list:
    """Ambil tiers aktif — custom dari DB kalau ada, default kalau tidak."""
    if db:
        custom = db.get("settings", {}).get("custom_gaji_tiers")
        if custom:
            return sorted(custom, key=lambda x: x["min"], reverse=True)
    return GAJI_TIERS

def get_active_konsisten_tiers(db=None) -> list:
    if db:
        custom = db.get("settings", {}).get("custom_konsisten_tiers")
        if custom:
            return sorted(custom, key=lambda x: x["min_clips"], reverse=True)
    return KONSISTEN_TIERS

def calc_gaji(views: int, db=None) -> int:
    tiers = get_active_gaji_tiers(db)
    for t in tiers:
        if views >= t["min"]: return t["gaji"]
    return 0

def get_tier(views: int, db=None) -> dict:
    tiers = get_active_gaji_tiers(db)
    for t in tiers:
        if views >= t["min"]: return t
    return tiers[-1]

def calc_konsisten_hadiah(clip_count: int, db=None):
    tiers = get_active_konsisten_tiers(db)
    for t in tiers:
        if clip_count >= t["min_clips"]: return t
    return None

def get_leaderboard(db, top_n=10):
    return sorted([c for c in db["clippers"].values() if c.get("active", True)], key=lambda x: x["total_views"], reverse=True)[:top_n]

def get_konsisten_leaderboard(db, top_n=10):
    return sorted([c for c in db["clippers"].values() if c.get("active", True)], key=lambda x: x["total_clips"], reverse=True)[:top_n]

def get_periode_leaderboard(db, top_n=10):
    views_p = db.get("periode", {}).get("views_periode", {})
    clips_p = db.get("periode", {}).get("clips_periode", {})
    result = [{**c, "periode_views": views_p.get(did, 0), "periode_clips": clips_p.get(did, 0)}
              for did, c in db["clippers"].items() if c.get("active", True)]
    return sorted(result, key=lambda x: x["periode_views"], reverse=True)[:top_n]

def add_warning(db, discord_id: str, alasan: str, by: str) -> int:
    did = str(discord_id)
    if did not in db["warnings"]: db["warnings"][did] = []
    db["warnings"][did].append({"alasan": alasan, "by": by, "at": now_iso()})
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
    db["blacklist"][did] = {"alasan": alasan, "by": by, "at": now_iso()}
    if did in db["clippers"]: db["clippers"][did]["active"] = False
    save_db(db)

def unblacklist_clipper(db, discord_id: str):
    did = str(discord_id)
    db["blacklist"].pop(did, None)
    db["warnings"].pop(did, None)
    if did in db["clippers"]: db["clippers"][did]["active"] = True
    save_db(db)

def is_blacklisted(db, discord_id: str) -> bool:
    return str(discord_id) in db["blacklist"]

def buka_periode(db, nama: str, durasi_hari: int):
    mulai = datetime.now(timezone.utc)
    db["periode"] = {
        "aktif": True, "nama": nama, "mulai": mulai.isoformat(),
        "selesai": (mulai + timedelta(days=durasi_hari)).isoformat(),
        "clips_periode": {}, "views_periode": {},
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

def is_duplicate_url(db, url: str) -> bool:
    u = url.strip().rstrip("/").lower()
    return any(c.get("url", "").strip().rstrip("/").lower() == u for c in db["clips"])


def get_clip_by_id(db, clip_id: int):
    """Get clip by ID"""
    return next((c for c in db["clips"] if c["id"] == clip_id), None)


def get_clips_by_user(db, discord_id: str) -> list:
    """Get all clips by a user"""
    return [c for c in db["clips"] if c["discord_id"] == str(discord_id)]


def delete_clip(db, clip_id: int, deleted_by: str = None) -> dict:
    """
    Delete a clip and update clipper stats.
    Returns the deleted clip data or None if not found.
    """
    clip = get_clip_by_id(db, clip_id)
    if not clip:
        return None
    
    # Remove from clips list
    db["clips"] = [c for c in db["clips"] if c["id"] != clip_id]
    
    # Update clipper stats
    did = clip["discord_id"]
    if did in db["clippers"]:
        db["clippers"][did]["total_clips"] = max(0, db["clippers"][did]["total_clips"] - 1)
        db["clippers"][did]["total_views"] = max(0, db["clippers"][did]["total_views"] - clip["views"])
        if not clip.get("gaji_paid", False):
            db["clippers"][did]["pending_gaji"] = max(0, db["clippers"][did]["pending_gaji"] - clip.get("gaji", 0))
    
    # Update periode stats if active
    if periode_aktif(db):
        p = db["periode"]
        if did in p["clips_periode"]:
            p["clips_periode"][did] = max(0, p["clips_periode"].get(did, 0) - 1)
        if did in p["views_periode"]:
            p["views_periode"][did] = max(0, p["views_periode"].get(did, 0) - clip["views"])
    
    save_db(db)
    return clip


def update_clip(db, clip_id: int, updates: dict) -> dict:
    """
    Update clip data. Handles views/gaji changes with stat adjustments.
    Returns updated clip or None if not found.
    """
    clip = get_clip_by_id(db, clip_id)
    if not clip:
        return None
    
    did = clip["discord_id"]
    old_views = clip["views"]
    old_gaji = clip.get("gaji", 0)
    was_paid = clip.get("gaji_paid", False)
    
    # Apply updates
    for key, value in updates.items():
        clip[key] = value
    clip["last_updated"] = now_iso()
    
    # Recalculate stats if views changed
    if "views" in updates and did in db["clippers"]:
        views_diff = updates["views"] - old_views
        db["clippers"][did]["total_views"] += views_diff
        
        # Update periode if active
        if periode_aktif(db):
            p = db["periode"]
            if did in p["views_periode"]:
                p["views_periode"][did] = max(0, p["views_periode"].get(did, 0) + views_diff)
    
    # Handle gaji changes
    if "gaji" in updates and did in db["clippers"] and not was_paid:
        gaji_diff = updates["gaji"] - old_gaji
        db["clippers"][did]["pending_gaji"] += gaji_diff
    
    save_db(db)
    return clip

def check_account_ownership(db, discord_id: str, username: str, platform: str) -> bool:
    clipper = get_clipper(db, discord_id)
    if not clipper: return False
    u = username.lower().lstrip("@").replace("_","").replace(".","").replace("-","")
    return any(
        acc["username"].lower().replace("_","").replace(".","").replace("-","") == u and acc["platform"] == platform
        for acc in clipper.get("accounts", [])
    )

def get_matching_account(db, discord_id: str, username: str, platform: str) -> dict:
    clipper = get_clipper(db, discord_id)
    if not clipper: return None
    u = username.lower().lstrip("@").replace("_","").replace(".","").replace("-","")
    return next(
        (acc for acc in clipper.get("accounts", [])
         if acc["username"].lower().replace("_","").replace(".","").replace("-","") == u and acc["platform"] == platform),
        None
    )
