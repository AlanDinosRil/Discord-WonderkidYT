import json
import os
from datetime import datetime, timezone

DB_FILE = "data.json"

DEFAULT_DB = {
    "clippers": {},           # discord_id -> clipper info (multi-account support)
    "pending_registrations": {},  # discord_id -> list of pending account registrations
    "clips": [],
    "gaji_history": [],
    "warnings": {},           # discord_id -> list of warning dicts
    "blacklist": {},          # discord_id -> blacklist info
    "tickets": {},            # ticket_id -> ticket data for reward claims
    "ticket_counter": 0,      # auto-increment ticket ID
    "periode": {              # info periode aktif
        "aktif": False,
        "nama": "",
        "mulai": "",
        "selesai": "",
        "clips_periode": {},  # discord_id -> clip count in period
        "views_periode": {},  # discord_id -> views in period
    },
    "settings": {
        "clipper_channel_id": 0,
        "gaji_channel_id": 0,
        "rekap_channel_id": 0,
        "log_channel_id": 0,
        "ticket_channel_id": 0,       # channel for ticket management
        "approval_channel_id": 0,     # channel for registration approvals
        "clip_role_name": "Clip",     # role name given to approved clippers
        "admin_role_name": "Admin",
        "rekap_hari": 1,              # Senin = 0, Minggu = 6
        "periode_hari": 30,           # durasi periode default
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
                db[key] = val if not isinstance(val, dict) else json.loads(json.dumps(val))
        # Pastikan sub-keys settings ada
        for k, v in DEFAULT_DB["settings"].items():
            if k not in db["settings"]:
                db["settings"][k] = v
        # Migrate old single-account clippers to multi-account format
        for did, clipper in db["clippers"].items():
            if "accounts" not in clipper:
                # Migrate old format to new format
                db["clippers"][did]["accounts"] = [{
                    "id": 1,
                    "username": clipper.get("username", ""),
                    "platform": clipper.get("platform", "tiktok"),
                    "added_at": clipper.get("joined_at", now_iso()),
                    "active": True,
                }]
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


# ── CLIPPER (MULTI-ACCOUNT SUPPORT) ──────────────────────────────────────────

def get_clipper(db, discord_id: str):
    return db["clippers"].get(str(discord_id))


def get_clipper_account(db, discord_id: str, account_id: int):
    """Get specific account by ID"""
    clipper = get_clipper(db, discord_id)
    if not clipper:
        return None
    for acc in clipper.get("accounts", []):
        if acc["id"] == account_id:
            return acc
    return None


def get_clipper_account_by_username(db, discord_id: str, username: str, platform: str):
    """Find account by username and platform"""
    clipper = get_clipper(db, discord_id)
    if not clipper:
        return None
    username_clean = username.lower().lstrip("@")
    for acc in clipper.get("accounts", []):
        if acc["username"].lower() == username_clean and acc["platform"] == platform:
            return acc
    return None


def get_all_accounts(db, discord_id: str) -> list:
    """Get all accounts for a clipper"""
    clipper = get_clipper(db, discord_id)
    if not clipper:
        return []
    return clipper.get("accounts", [])


def register_clipper(db, discord_id: str, username: str, platform: str, display_name: str):
    """Register a new clipper with their first account"""
    db["clippers"][str(discord_id)] = {
        "discord_id": str(discord_id),
        "display_name": display_name,
        "accounts": [{
            "id": 1,
            "username": username.lstrip("@"),
            "platform": platform,
            "added_at": now_iso(),
            "active": True,
        }],
        "joined_at": now_iso(),
        "total_views": 0,
        "total_gaji": 0,
        "total_clips": 0,
        "pending_gaji": 0,
        "active": True,
    }
    save_db(db)


def add_clipper_account(db, discord_id: str, username: str, platform: str) -> dict:
    """Add a new account to existing clipper"""
    did = str(discord_id)
    if did not in db["clippers"]:
        return None
    
    accounts = db["clippers"][did].get("accounts", [])
    new_id = max([a["id"] for a in accounts], default=0) + 1
    
    new_account = {
        "id": new_id,
        "username": username.lstrip("@"),
        "platform": platform,
        "added_at": now_iso(),
        "active": True,
    }
    db["clippers"][did]["accounts"].append(new_account)
    save_db(db)
    return new_account


def remove_clipper_account(db, discord_id: str, account_id: int) -> bool:
    """Remove an account from clipper"""
    did = str(discord_id)
    if did not in db["clippers"]:
        return False
    
    accounts = db["clippers"][did].get("accounts", [])
    new_accounts = [a for a in accounts if a["id"] != account_id]
    
    if len(new_accounts) == len(accounts):
        return False  # Account not found
    
    db["clippers"][did]["accounts"] = new_accounts
    save_db(db)
    return True


# ── PENDING REGISTRATIONS ────────────────────────────────────────────────────

def add_pending_registration(db, discord_id: str, username: str, platform: str, display_name: str) -> int:
    """Add a pending registration request"""
    did = str(discord_id)
    if did not in db["pending_registrations"]:
        db["pending_registrations"][did] = {
            "discord_id": did,
            "display_name": display_name,
            "accounts": [],
            "requested_at": now_iso(),
        }
    
    accounts = db["pending_registrations"][did]["accounts"]
    new_id = max([a.get("id", 0) for a in accounts], default=0) + 1
    
    db["pending_registrations"][did]["accounts"].append({
        "id": new_id,
        "username": username.lstrip("@"),
        "platform": platform,
        "requested_at": now_iso(),
    })
    db["pending_registrations"][did]["display_name"] = display_name
    save_db(db)
    return new_id


def get_pending_registration(db, discord_id: str):
    """Get pending registration for a user"""
    return db["pending_registrations"].get(str(discord_id))


def get_all_pending_registrations(db) -> list:
    """Get all pending registrations"""
    return list(db["pending_registrations"].values())


def approve_registration(db, discord_id: str) -> dict:
    """Approve a pending registration"""
    did = str(discord_id)
    pending = db["pending_registrations"].get(did)
    if not pending:
        return None
    
    # Check if clipper already exists
    existing = get_clipper(db, did)
    
    if existing:
        # Add pending accounts to existing clipper
        for acc in pending["accounts"]:
            add_clipper_account(db, did, acc["username"], acc["platform"])
    else:
        # Create new clipper with all pending accounts
        db["clippers"][did] = {
            "discord_id": did,
            "display_name": pending["display_name"],
            "accounts": [],
            "joined_at": now_iso(),
            "total_views": 0,
            "total_gaji": 0,
            "total_clips": 0,
            "pending_gaji": 0,
            "active": True,
        }
        for i, acc in enumerate(pending["accounts"], 1):
            db["clippers"][did]["accounts"].append({
                "id": i,
                "username": acc["username"],
                "platform": acc["platform"],
                "added_at": now_iso(),
                "active": True,
            })
    
    # Remove from pending
    del db["pending_registrations"][did]
    save_db(db)
    return db["clippers"][did]


def reject_registration(db, discord_id: str, reason: str = "") -> dict:
    """Reject a pending registration"""
    did = str(discord_id)
    pending = db["pending_registrations"].get(did)
    if not pending:
        return None
    
    rejected = {**pending, "rejected_reason": reason, "rejected_at": now_iso()}
    del db["pending_registrations"][did]
    save_db(db)
    return rejected


def cancel_pending_account(db, discord_id: str, account_id: int) -> bool:
    """Cancel a specific pending account"""
    did = str(discord_id)
    pending = db["pending_registrations"].get(did)
    if not pending:
        return False
    
    accounts = pending["accounts"]
    new_accounts = [a for a in accounts if a.get("id") != account_id]
    
    if len(new_accounts) == len(accounts):
        return False
    
    if len(new_accounts) == 0:
        # No more pending accounts, remove entire registration
        del db["pending_registrations"][did]
    else:
        db["pending_registrations"][did]["accounts"] = new_accounts
    
    save_db(db)
    return True


# ── TICKETS (REWARD CLAIM) ───────────────────────────────────────────────────

def create_ticket(db, discord_id: str, display_name: str, data: dict) -> dict:
    """Create a new ticket for reward claim"""
    db["ticket_counter"] = db.get("ticket_counter", 0) + 1
    ticket_id = db["ticket_counter"]
    
    ticket = {
        "id": ticket_id,
        "discord_id": str(discord_id),
        "display_name": display_name,
        "bank_name": data.get("bank_name", ""),
        "account_number": data.get("account_number", ""),
        "account_holder": data.get("account_holder", ""),
        "phone_number": data.get("phone_number", ""),
        "notes": data.get("notes", ""),
        "status": "open",  # open, processing, completed, cancelled
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "processed_by": None,
        "completed_at": None,
    }
    
    db["tickets"][str(ticket_id)] = ticket
    save_db(db)
    return ticket


def get_ticket(db, ticket_id: int):
    """Get a ticket by ID"""
    return db["tickets"].get(str(ticket_id))


def get_user_tickets(db, discord_id: str) -> list:
    """Get all tickets for a user"""
    did = str(discord_id)
    return [t for t in db["tickets"].values() if t["discord_id"] == did]


def get_open_tickets(db) -> list:
    """Get all open tickets"""
    return [t for t in db["tickets"].values() if t["status"] in ("open", "processing")]


def update_ticket_status(db, ticket_id: int, status: str, processed_by: str = None) -> dict:
    """Update ticket status"""
    tid = str(ticket_id)
    if tid not in db["tickets"]:
        return None
    
    db["tickets"][tid]["status"] = status
    db["tickets"][tid]["updated_at"] = now_iso()
    
    if processed_by:
        db["tickets"][tid]["processed_by"] = processed_by
    
    if status == "completed":
        db["tickets"][tid]["completed_at"] = now_iso()
    
    save_db(db)
    return db["tickets"][tid]


def update_ticket_data(db, ticket_id: int, data: dict) -> dict:
    """Update ticket payment data"""
    tid = str(ticket_id)
    if tid not in db["tickets"]:
        return None
    
    for key in ["bank_name", "account_number", "account_holder", "phone_number", "notes"]:
        if key in data:
            db["tickets"][tid][key] = data[key]
    
    db["tickets"][tid]["updated_at"] = now_iso()
    save_db(db)
    return db["tickets"][tid]


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


# ── HELPER: Check account ownership for verification ─────────────────────────

def check_account_ownership(db, discord_id: str, username: str, platform: str) -> bool:
    """Check if user owns an account with given username and platform"""
    clipper = get_clipper(db, discord_id)
    if not clipper:
        return False
    
    username_clean = username.lower().lstrip("@").replace("_", "").replace(".", "").replace("-", "")
    
    for acc in clipper.get("accounts", []):
        acc_username = acc["username"].lower().replace("_", "").replace(".", "").replace("-", "")
        if acc_username == username_clean and acc["platform"] == platform:
            return True
    return False


def get_matching_account(db, discord_id: str, username: str, platform: str) -> dict:
    """Get the matching account for verification"""
    clipper = get_clipper(db, discord_id)
    if not clipper:
        return None
    
    username_clean = username.lower().lstrip("@").replace("_", "").replace(".", "").replace("-", "")
    
    for acc in clipper.get("accounts", []):
        acc_username = acc["username"].lower().replace("_", "").replace(".", "").replace("-", "")
        if acc_username == username_clean and acc["platform"] == platform:
            return acc
    return None
