import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from datetime import datetime, timezone, timedelta

from database import (
    load_db, save_db, get_clipper, register_clipper,
    calc_gaji, get_tier, calc_konsisten_hadiah,
    get_leaderboard, get_konsisten_leaderboard, get_periode_leaderboard,
    add_warning, get_warnings, clear_warnings,
    blacklist_clipper, unblacklist_clipper, is_blacklisted,
    buka_periode, tutup_periode, periode_aktif, tambah_periode_stats,
    is_duplicate_url, now_iso,
    GAJI_TIERS, KONSISTEN_TIERS, MAX_WARNINGS,
    # New imports for multi-account & approval system
    add_pending_registration, get_pending_registration, get_all_pending_registrations,
    approve_registration, reject_registration, cancel_pending_account,
    get_all_accounts, add_clipper_account, remove_clipper_account,
    get_clipper_account, get_clipper_account_by_username,
    check_account_ownership, get_matching_account,
    # Ticket system
    create_ticket, get_ticket, get_user_tickets, get_open_tickets,
    update_ticket_status, update_ticket_data,
)
from views_fetcher import fetch_views
from verify_clip import verify_clip

TOKEN = os.environ.get("DISCORD_TOKEN", "")
ADMIN_ROLE_NAME = os.environ.get("ADMIN_ROLE_NAME", "Admin")
CLIP_ROLE_NAME = os.environ.get("CLIP_ROLE_NAME", "Clip")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── HELPERS ───────────────────────────────────────────────────────────────────

def is_admin(member: discord.Member) -> bool:
    return any(r.name == ADMIN_ROLE_NAME for r in member.roles) or member.guild_permissions.administrator

def has_clip_role(member: discord.Member) -> bool:
    """Check if member has the Clip role"""
    return any(r.name == CLIP_ROLE_NAME for r in member.roles)

async def give_clip_role(member: discord.Member, guild: discord.Guild) -> bool:
    """Give Clip role to member"""
    role = discord.utils.get(guild.roles, name=CLIP_ROLE_NAME)
    if role:
        try:
            await member.add_roles(role)
            return True
        except Exception:
            return False
    return False

async def remove_clip_role(member: discord.Member, guild: discord.Guild) -> bool:
    """Remove Clip role from member"""
    role = discord.utils.get(guild.roles, name=CLIP_ROLE_NAME)
    if role:
        try:
            await member.remove_roles(role)
            return True
        except Exception:
            return False
    return False

def fmt_rp(n: int) -> str:
    return f"Rp {n:,.0f}".replace(",", ".")

def fmt_views(v: int) -> str:
    if v >= 1_000_000: return f"{v/1_000_000:.2f}M"
    if v >= 1_000: return f"{v/1_000:.1f}K"
    return str(v)

async def send_log(guild: discord.Guild, db: dict, msg: str = None, embed: discord.Embed = None):
    ch_id = db["settings"].get("log_channel_id", 0)
    if not ch_id:
        return
    ch = guild.get_channel(ch_id)
    if ch:
        try:
            await ch.send(content=msg, embed=embed)
        except Exception:
            pass

def format_accounts_list(accounts: list) -> str:
    """Format list of accounts for display"""
    if not accounts:
        return "Tidak ada akun"
    lines = []
    for acc in accounts:
        icon = "🎵" if acc["platform"] == "tiktok" else "▶️"
        lines.append(f"{icon} #{acc['id']} @{acc['username']} ({acc['platform'].title()})")
    return "\n".join(lines)

# ── ON READY ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[BOT] {bot.user} online!")
    try:
        synced = await bot.tree.sync()
        print(f"[BOT] {len(synced)} commands synced")
    except Exception as e:
        print(f"[BOT] Sync error: {e}")
    auto_update_views.start()
    weekly_recap.start()

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 1 — !daftar dengan sistem approval (butuh persetujuan admin)
# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="daftar")
async def cmd_daftar(ctx, *, args: str = ""):
    db = load_db()

    parts = args.strip().split()
    if len(parts) < 2:
        embed = discord.Embed(
            title="Format Salah",
            description=(
                "**Cara daftar:**\n"
                "`!daftar tiktok @username`\n"
                "`!daftar youtube NamaChannel`\n\n"
                "Contoh: `!daftar tiktok @budi.clips`\n\n"
                "**Note:** Pendaftaran memerlukan persetujuan admin."
            ),
            color=0xED4245
        )
        return await ctx.reply(embed=embed)

    platform = parts[0].lower()
    if platform == "yt":
        platform = "youtube"
    username = parts[1].lstrip("@")

    if platform not in ("tiktok", "youtube"):
        return await ctx.reply(embed=discord.Embed(
            title="Platform tidak valid",
            description="Gunakan `tiktok` atau `youtube`",
            color=0xED4245
        ))

    did = str(ctx.author.id)

    # Cek blacklist
    if is_blacklisted(db, did):
        bl = db["blacklist"][did]
        return await ctx.reply(embed=discord.Embed(
            title="Kamu Di-blacklist",
            description=f"**Alasan:** {bl['alasan']}\nHubungi admin untuk banding.",
            color=0xED4245
        ))

    # Cek apakah sudah terdaftar sebagai clipper
    existing_clipper = get_clipper(db, did)
    
    # Cek apakah akun ini sudah ada (baik di pending maupun approved)
    if existing_clipper:
        for acc in existing_clipper.get("accounts", []):
            if acc["username"].lower() == username.lower() and acc["platform"] == platform:
                return await ctx.reply(embed=discord.Embed(
                    title="Akun Sudah Terdaftar",
                    description=f"Akun **@{username}** ({platform.title()}) sudah terdaftar.\n\nGunakan `/profil` untuk melihat semua akun kamu.",
                    color=0xFEE75C
                ))
    
    # Cek pending registration
    pending = get_pending_registration(db, did)
    if pending:
        for acc in pending.get("accounts", []):
            if acc["username"].lower() == username.lower() and acc["platform"] == platform:
                return await ctx.reply(embed=discord.Embed(
                    title="Akun Sudah Dalam Antrian",
                    description=f"Akun **@{username}** ({platform.title()}) sudah dalam antrian approval.\n\nTunggu admin untuk approve pendaftaranmu.",
                    color=0xFEE75C
                ))

    # Tambah ke pending registration
    add_pending_registration(db, did, username, platform, ctx.author.display_name)
    
    # Get updated pending info
    pending = get_pending_registration(db, did)
    accounts_list = format_accounts_list([
        {"id": a.get("id", i+1), "username": a["username"], "platform": a["platform"]} 
        for i, a in enumerate(pending.get("accounts", []))
    ])
    
    embed = discord.Embed(
        title="Pendaftaran Dikirim!",
        description=(
            f"Halo **{ctx.author.display_name}**!\n\n"
            f"Pendaftaran akun **@{username}** ({platform.title()}) sudah dikirim.\n\n"
            f"**Akun yang menunggu approval:**\n{accounts_list}\n\n"
            f"Tunggu admin untuk menyetujui pendaftaranmu.\n"
            f"Setelah di-approve, kamu akan mendapat role **{CLIP_ROLE_NAME}**."
        ),
        color=0xFEE75C,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.set_footer(text="Campaign Clipper System")
    await ctx.reply(embed=embed)

    # Kirim notifikasi ke channel approval
    approval_ch_id = db["settings"].get("approval_channel_id", 0)
    if approval_ch_id:
        ch = ctx.guild.get_channel(approval_ch_id)
        if ch:
            notif_embed = discord.Embed(
                title="Pendaftaran Clipper Baru",
                description=(
                    f"**User:** {ctx.author.mention}\n"
                    f"**Akun Baru:** @{username} ({platform.title()})\n\n"
                    f"**Semua akun pending:**\n{accounts_list}\n\n"
                    f"Gunakan `/approve @user` untuk menyetujui atau `/reject @user` untuk menolak."
                ),
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc)
            )
            notif_embed.set_thumbnail(url=ctx.author.display_avatar.url)
            try:
                await ch.send(embed=notif_embed)
            except Exception:
                pass

    await send_log(ctx.guild, db, embed=discord.Embed(
        title="Pendaftaran Clipper Baru (Pending)",
        description=f"{ctx.author.mention} mendaftar dengan akun @{username} ({platform.title()})",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    ))

# ══════════════════════════════════════════════════════════════════════════════
# FITUR BARU — /add untuk menambah akun clipper
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="add", description="Tambah akun clipper baru (TikTok/YouTube)")
@app_commands.describe(
    platform="Platform akun: TikTok atau YouTube",
    username="Username akun (tanpa @)"
)
@app_commands.choices(platform=[
    app_commands.Choice(name="TikTok", value="tiktok"),
    app_commands.Choice(name="YouTube", value="youtube"),
])
async def add_account(interaction: discord.Interaction, platform: str, username: str):
    db = load_db()
    did = str(interaction.user.id)
    username = username.lstrip("@")
    
    # Cek blacklist
    if is_blacklisted(db, did):
        bl = db["blacklist"][did]
        return await interaction.response.send_message(
            embed=discord.Embed(
                title="Kamu Di-blacklist",
                description=f"**Alasan:** {bl['alasan']}\nHubungi admin untuk banding.",
                color=0xED4245
            ),
            ephemeral=True
        )
    
    # Cek apakah sudah jadi clipper
    clipper = get_clipper(db, did)
    
    if clipper:
        # Cek apakah akun sudah ada
        for acc in clipper.get("accounts", []):
            if acc["username"].lower() == username.lower() and acc["platform"] == platform:
                return await interaction.response.send_message(
                    embed=discord.Embed(
                        title="Akun Sudah Terdaftar",
                        description=f"Akun **@{username}** ({platform.title()}) sudah terdaftar.",
                        color=0xFEE75C
                    ),
                    ephemeral=True
                )
    
    # Cek pending
    pending = get_pending_registration(db, did)
    if pending:
        for acc in pending.get("accounts", []):
            if acc["username"].lower() == username.lower() and acc["platform"] == platform:
                return await interaction.response.send_message(
                    embed=discord.Embed(
                        title="Akun Sudah Dalam Antrian",
                        description=f"Akun **@{username}** ({platform.title()}) sudah dalam antrian approval.",
                        color=0xFEE75C
                    ),
                    ephemeral=True
                )
    
    # Tambah ke pending
    add_pending_registration(db, did, username, platform, interaction.user.display_name)
    
    # Get updated pending
    pending = get_pending_registration(db, did)
    accounts_list = format_accounts_list([
        {"id": a.get("id", i+1), "username": a["username"], "platform": a["platform"]} 
        for i, a in enumerate(pending.get("accounts", []))
    ])
    
    embed = discord.Embed(
        title="Permintaan Akun Baru Dikirim!",
        description=(
            f"Akun **@{username}** ({platform.title()}) sudah ditambahkan ke antrian.\n\n"
            f"**Akun yang menunggu approval:**\n{accounts_list}\n\n"
            f"Tunggu admin untuk menyetujui."
        ),
        color=0xFEE75C,
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.response.send_message(embed=embed)
    
    # Notifikasi ke channel approval
    approval_ch_id = db["settings"].get("approval_channel_id", 0)
    if approval_ch_id:
        ch = interaction.guild.get_channel(approval_ch_id)
        if ch:
            already_clipper = "Ya" if clipper else "Belum"
            notif_embed = discord.Embed(
                title="Permintaan Tambah Akun",
                description=(
                    f"**User:** {interaction.user.mention}\n"
                    f"**Sudah Clipper:** {already_clipper}\n"
                    f"**Akun Baru:** @{username} ({platform.title()})\n\n"
                    f"**Semua akun pending:**\n{accounts_list}"
                ),
                color=0x5865F2,
                timestamp=datetime.now(timezone.utc)
            )
            try:
                await ch.send(embed=notif_embed)
            except Exception:
                pass

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Approve & Reject Registration
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="approve", description="[ADMIN] Setujui pendaftaran clipper")
@app_commands.describe(member="User yang pendaftarannya disetujui")
async def approve_cmd(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    
    db = load_db()
    did = str(member.id)
    
    pending = get_pending_registration(db, did)
    if not pending:
        return await interaction.response.send_message(
            f"{member.mention} tidak memiliki pendaftaran yang pending.",
            ephemeral=True
        )
    
    # Approve registration
    clipper = approve_registration(db, did)
    
    # Give Clip role
    role_given = await give_clip_role(member, interaction.guild)
    role_msg = f"\nRole **{CLIP_ROLE_NAME}** diberikan!" if role_given else "\n(Gagal memberikan role, berikan manual)"
    
    # Give channel access
    access_msg = ""
    ch_id = db["settings"].get("clipper_channel_id", 0)
    if ch_id:
        ch = interaction.guild.get_channel(ch_id)
        if ch:
            try:
                await ch.set_permissions(member, read_messages=True, send_messages=True)
                access_msg = f"\nAkses ke {ch.mention} diberikan!"
            except Exception:
                access_msg = "\n(Gagal beri akses channel)"
    
    accounts_list = format_accounts_list(clipper.get("accounts", []))
    
    embed = discord.Embed(
        title="Clipper Disetujui!",
        description=(
            f"**User:** {member.mention}\n\n"
            f"**Akun Terdaftar:**\n{accounts_list}\n"
            f"{role_msg}{access_msg}"
        ),
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Approved by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    
    # DM to clipper
    try:
        dm_embed = discord.Embed(
            title="Selamat! Pendaftaran Disetujui!",
            description=(
                f"Kamu sekarang resmi menjadi **Clipper**!\n\n"
                f"**Akun Terdaftar:**\n{accounts_list}\n\n"
                f"Gunakan `/submit <link>` untuk submit clip pertamamu!"
            ),
            color=0x57F287
        )
        await member.send(embed=dm_embed)
    except Exception:
        pass
    
    await send_log(interaction.guild, db, embed=discord.Embed(
        title="Clipper Approved",
        description=f"{member.mention} disetujui oleh {interaction.user.mention}",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    ))

@bot.tree.command(name="reject", description="[ADMIN] Tolak pendaftaran clipper")
@app_commands.describe(member="User yang pendaftarannya ditolak", alasan="Alasan penolakan")
async def reject_cmd(interaction: discord.Interaction, member: discord.Member, alasan: str = "Tidak memenuhi syarat"):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    
    db = load_db()
    did = str(member.id)
    
    pending = get_pending_registration(db, did)
    if not pending:
        return await interaction.response.send_message(
            f"{member.mention} tidak memiliki pendaftaran yang pending.",
            ephemeral=True
        )
    
    rejected = reject_registration(db, did, alasan)
    
    embed = discord.Embed(
        title="Pendaftaran Ditolak",
        description=(
            f"**User:** {member.mention}\n"
            f"**Alasan:** {alasan}"
        ),
        color=0xED4245,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Rejected by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    
    # DM to user
    try:
        dm_embed = discord.Embed(
            title="Pendaftaran Ditolak",
            description=f"Maaf, pendaftaranmu sebagai clipper ditolak.\n\n**Alasan:** {alasan}",
            color=0xED4245
        )
        await member.send(embed=dm_embed)
    except Exception:
        pass

@bot.tree.command(name="pending", description="[ADMIN] Lihat semua pendaftaran yang pending")
async def pending_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    
    db = load_db()
    pending_list = get_all_pending_registrations(db)
    
    if not pending_list:
        return await interaction.response.send_message(
            embed=discord.Embed(
                title="Pendaftaran Pending",
                description="Tidak ada pendaftaran yang menunggu approval.",
                color=0x5865F2
            )
        )
    
    embed = discord.Embed(
        title=f"Pendaftaran Pending ({len(pending_list)})",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    
    for p in pending_list[:10]:  # Max 10
        accounts = p.get("accounts", [])
        acc_text = "\n".join([
            f"{'🎵' if a['platform'] == 'tiktok' else '▶️'} @{a['username']} ({a['platform'].title()})"
            for a in accounts
        ])
        member = interaction.guild.get_member(int(p["discord_id"]))
        name = member.mention if member else p["display_name"]
        embed.add_field(
            name=f"{p['display_name']}",
            value=f"{name}\n{acc_text}\n*{p['requested_at'][:10]}*",
            inline=False
        )
    
    embed.set_footer(text="Gunakan /approve atau /reject untuk memproses")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 2 — /submit dengan verifikasi multi-akun
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="submit", description="Submit clip campaign TikTok/YouTube kamu")
@app_commands.describe(url="Link video TikTok atau YouTube")
async def submit_clip(interaction: discord.Interaction, url: str):
    db = load_db()
    did = str(interaction.user.id)
    clipper = get_clipper(db, did)

    if not clipper:
        # Cek apakah ada pending
        pending = get_pending_registration(db, did)
        if pending:
            return await interaction.response.send_message(
                "Pendaftaranmu masih menunggu approval admin. Tunggu sampai disetujui.", 
                ephemeral=True
            )
        return await interaction.response.send_message(
            "Belum terdaftar! Ketik `!daftar tiktok @username` dulu.", ephemeral=True
        )

    if is_blacklisted(db, did):
        return await interaction.response.send_message("Kamu di-blacklist. Hubungi admin.", ephemeral=True)

    # ── Anti-duplikat ─────────────────────────────────────────────────────────
    if is_duplicate_url(db, url):
        existing = next((c for c in db["clips"] if c["url"].strip().rstrip("/").lower() == url.strip().rstrip("/").lower()), None)
        warn_count = add_warning(db, did, f"Submit URL duplikat: {url}", "System")
        embed = discord.Embed(
            title="URL Sudah Pernah Disubmit!",
            description=(
                f"Link ini sudah ada di database (Clip #{existing['id']} oleh **{existing['clipper_name']}**).\n\n"
                f"Warning kamu: **{warn_count}/{MAX_WARNINGS}**"
                + ("\n**Satu warning lagi = auto blacklist!**" if warn_count == MAX_WARNINGS - 1 else "")
                + ("\n**Kamu telah di-blacklist otomatis!**" if warn_count >= MAX_WARNINGS else "")
            ),
            color=0xED4245
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    await interaction.response.defer(thinking=True)

    # ── Verifikasi kepemilikan video (cek semua akun) ─────────────────────────
    # Detect platform from URL
    if "tiktok.com" in url:
        detected_platform = "tiktok"
    elif "youtu" in url:
        detected_platform = "youtube"
    else:
        return await interaction.followup.send("Platform tidak dikenali. Gunakan link TikTok atau YouTube.", ephemeral=True)
    
    # Get all accounts for this platform
    accounts = [acc for acc in clipper.get("accounts", []) if acc["platform"] == detected_platform]
    
    if not accounts:
        return await interaction.followup.send(
            f"Kamu tidak punya akun {detected_platform.title()} terdaftar. Tambah dulu dengan `/add`.", 
            ephemeral=True
        )
    
    # Try to verify against all accounts
    verify_result = None
    matched_account = None
    
    for acc in accounts:
        verify = await verify_clip(url, acc["username"], acc["platform"])
        if verify["match"]:
            verify_result = verify
            matched_account = acc
            break
        elif verify_result is None:
            verify_result = verify  # Keep first result for error message
    
    if not matched_account:
        # Beri warning jika verifikasi gagal (bukan unknown)
        if verify_result and verify_result["confidence"] != "unknown":
            warn_count = add_warning(db, did, f"Submit video bukan miliknya: {url}", "System")
            warn_msg = (
                f"\n\nWarning **{warn_count}/{MAX_WARNINGS}** diberikan."
                + ("\n**Auto-blacklist setelah 1 warning lagi!**" if warn_count == MAX_WARNINGS - 1 else "")
                + ("\n**Kamu telah di-blacklist!**" if warn_count >= MAX_WARNINGS else "")
            )
        else:
            warn_msg = "\n\nTidak bisa memverifikasi kepemilikan (video mungkin private)."
        
        accounts_list = ", ".join([f"@{acc['username']}" for acc in accounts])
        embed = discord.Embed(
            title="Verifikasi Gagal",
            description=(
                f"Video ini **tidak cocok** dengan akun yang kamu daftarkan.\n\n"
                f"**Akun {detected_platform.title()} Terdaftar:** {accounts_list}\n"
                f"**Ditemukan:** @{verify_result.get('found_username', '?')}\n"
                f"**Detail:** {verify_result['reason']}"
                f"{warn_msg}"
            ),
            color=0xED4245
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Fetch views ───────────────────────────────────────────────────────────
    result = await fetch_views(url)
    if not result["success"]:
        return await interaction.followup.send(
            f"Gagal ambil views: `{result['error']}`\nPastikan video tidak private.", ephemeral=True
        )

    views = result["views"]
    gaji = calc_gaji(views)
    tier = get_tier(views)

    clip_data = {
        "id": len(db["clips"]) + 1,
        "discord_id": did,
        "clipper_name": clipper["display_name"],
        "account_id": matched_account["id"],
        "account_username": matched_account["username"],
        "platform": result["platform"],
        "url": url,
        "title": result.get("title", "Unknown"),
        "thumbnail": result.get("thumbnail", ""),
        "views": views,
        "views_milestones": [],
        "gaji": gaji,
        "gaji_paid": False,
        "submitted_at": now_iso(),
        "last_updated": now_iso(),
    }
    db["clips"].append(clip_data)
    db["clippers"][did]["total_clips"] += 1
    db["clippers"][did]["total_views"] += views
    db["clippers"][did]["pending_gaji"] += gaji

    # Update stats periode jika aktif
    if periode_aktif(db):
        tambah_periode_stats(db, did, views)

    save_db(db)

    embed = discord.Embed(
        title="Clip Berhasil Disubmit!",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=result.get("thumbnail", ""))
    embed.add_field(name="Judul", value=result.get("title", "Unknown")[:80], inline=False)
    embed.add_field(name="Views", value=fmt_views(views), inline=True)
    embed.add_field(name=f"{tier['emoji']} Tier", value=tier["label"], inline=True)
    embed.add_field(name="Estimasi Gaji", value=fmt_rp(gaji) if gaji > 0 else "Belum mencapai 100K", inline=True)
    embed.add_field(name="Akun", value=f"@{matched_account['username']}", inline=True)
    embed.add_field(name="Clip ID", value=f"#{clip_data['id']}", inline=True)
    embed.set_footer(text=f"Submit oleh {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 3 — /profil dengan multi-akun display
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="profil", description="Lihat profil dan statistik clipper")
@app_commands.describe(member="Profil clipper lain (opsional)")
async def profil(interaction: discord.Interaction, member: discord.Member = None):
    db = load_db()
    target = member or interaction.user
    clipper = get_clipper(db, str(target.id))

    if not clipper:
        # Cek pending
        pending = get_pending_registration(db, str(target.id))
        if pending:
            accounts_list = format_accounts_list([
                {"id": a.get("id", i+1), "username": a["username"], "platform": a["platform"]} 
                for i, a in enumerate(pending.get("accounts", []))
            ])
            return await interaction.response.send_message(
                embed=discord.Embed(
                    title=f"Profil — {target.display_name} (Pending)",
                    description=f"Pendaftaran masih menunggu approval.\n\n**Akun Pending:**\n{accounts_list}",
                    color=0xFEE75C
                ),
                ephemeral=True
            )
        return await interaction.response.send_message(
            f"{'Kamu' if not member else target.display_name} belum terdaftar.", ephemeral=True
        )

    clips = [c for c in db["clips"] if c["discord_id"] == str(target.id)]
    konsisten = calc_konsisten_hadiah(len(clips))
    warnings = get_warnings(db, str(target.id))
    bl = db["blacklist"].get(str(target.id))

    embed = discord.Embed(
        title=f"Profil — {clipper['display_name']}",
        color=0xED4245 if bl else 0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    if bl:
        embed.add_field(name="Status", value=f"BLACKLIST\n*{bl['alasan']}*", inline=False)

    # Display all accounts
    accounts = clipper.get("accounts", [])
    accounts_text = format_accounts_list(accounts)
    embed.add_field(name=f"Akun Terdaftar ({len(accounts)})", value=accounts_text, inline=False)
    
    embed.add_field(name="Bergabung", value=clipper["joined_at"][:10], inline=True)
    embed.add_field(name="Total Clip", value=str(clipper["total_clips"]), inline=True)
    embed.add_field(name="Total Views", value=fmt_views(clipper["total_views"]), inline=True)
    embed.add_field(name="Total Gaji", value=fmt_rp(clipper["total_gaji"]), inline=True)
    embed.add_field(name="Pending", value=fmt_rp(clipper["pending_gaji"]), inline=True)

    if warnings:
        embed.add_field(name=f"Warning ({len(warnings)}/{MAX_WARNINGS})",
                        value="\n".join(f"- {w['alasan'][:40]}" for w in warnings[-3:]), inline=False)

    if konsisten:
        embed.add_field(name="Status Konsisten",
                        value=f"{konsisten['label']} (+{fmt_rp(konsisten['hadiah'])})", inline=False)

    if clips:
        val = "\n".join(
            f"#{c['id']} {fmt_views(c['views'])} views - {fmt_rp(c['gaji'])} {'(paid)' if c['gaji_paid'] else '(pending)'}"
            for c in clips[-3:][::-1]
        )
        embed.add_field(name="3 Clip Terakhir", value=val, inline=False)

    embed.set_footer(text="Campaign Clipper System")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR — /akun untuk manage akun sendiri
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="akun", description="Lihat dan kelola akun clipper kamu")
async def akun_cmd(interaction: discord.Interaction):
    db = load_db()
    did = str(interaction.user.id)
    clipper = get_clipper(db, did)
    
    # Build response
    embed = discord.Embed(
        title=f"Akun Clipper — {interaction.user.display_name}",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    
    if clipper:
        accounts = clipper.get("accounts", [])
        if accounts:
            for acc in accounts:
                icon = "🎵" if acc["platform"] == "tiktok" else "▶️"
                clips_count = len([c for c in db["clips"] if c["discord_id"] == did and c.get("account_id") == acc["id"]])
                embed.add_field(
                    name=f"{icon} #{acc['id']} @{acc['username']}",
                    value=f"Platform: {acc['platform'].title()}\nClips: {clips_count}\nTambah: {acc['added_at'][:10]}",
                    inline=True
                )
        else:
            embed.description = "Tidak ada akun terdaftar."
    else:
        embed.description = "Kamu belum terdaftar sebagai clipper."
    
    # Check pending accounts
    pending = get_pending_registration(db, did)
    if pending and pending.get("accounts"):
        pending_text = "\n".join([
            f"{'🎵' if a['platform'] == 'tiktok' else '▶️'} @{a['username']} ({a['platform'].title()})"
            for a in pending["accounts"]
        ])
        embed.add_field(name="Menunggu Approval", value=pending_text, inline=False)
    
    embed.set_footer(text="Gunakan /add untuk menambah akun baru")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 4 — /daftar_clipper
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="daftar_clipper", description="Lihat semua clipper terdaftar")
async def daftar_clipper(interaction: discord.Interaction):
    db = load_db()
    clippers = sorted(db["clippers"].values(), key=lambda x: x["total_views"], reverse=True)

    if not clippers:
        return await interaction.response.send_message("Belum ada clipper.", ephemeral=True)

    aktif = [c for c in clippers if c.get("active", True)]
    nonaktif = [c for c in clippers if not c.get("active", True)]

    embed = discord.Embed(
        title=f"Daftar Clipper — {len(aktif)} aktif, {len(nonaktif)} nonaktif",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )

    for i, c in enumerate(aktif[:15], 1):
        accounts = c.get("accounts", [])
        acc_text = ", ".join([f"@{a['username']}" for a in accounts[:3]])
        if len(accounts) > 3:
            acc_text += f" +{len(accounts)-3}"
        warnings = get_warnings(db, c["discord_id"])
        warn_badge = f" (warn:{len(warnings)})" if warnings else ""
        embed.add_field(
            name=f"{i}. {c['display_name']}{warn_badge}",
            value=f"{acc_text}\nClips: {c['total_clips']} | Views: {fmt_views(c['total_views'])} | Gaji: {fmt_rp(c['total_gaji'])}",
            inline=False
        )

    if nonaktif:
        embed.add_field(name="Blacklist", value=", ".join(c["display_name"] for c in nonaktif), inline=False)

    embed.set_footer(text="Gunakan /profil @member untuk detail")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 5 — /leaderboard
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="leaderboard", description="Leaderboard clipper terbaik")
@app_commands.describe(tipe="Pilih jenis leaderboard")
@app_commands.choices(tipe=[
    app_commands.Choice(name="Views Terbanyak", value="views"),
    app_commands.Choice(name="Clip Terbanyak (Konsisten)", value="clips"),
    app_commands.Choice(name="Periode Aktif", value="periode"),
])
async def leaderboard(interaction: discord.Interaction, tipe: str = "views"):
    db = load_db()

    if tipe == "clips":
        top = get_konsisten_leaderboard(db)
        title = "Leaderboard — Clipper Terkonsisten"
        color = 0xFEE75C
    elif tipe == "periode":
        if not periode_aktif(db):
            return await interaction.response.send_message("Tidak ada periode aktif.", ephemeral=True)
        top = get_periode_leaderboard(db)
        title = f"Leaderboard Periode: {db['periode']['nama']}"
        color = 0xEB459E
    else:
        top = get_leaderboard(db)
        title = "Leaderboard — Views Terbanyak"
        color = 0xEB459E

    if not top:
        return await interaction.response.send_message("Belum ada data.", ephemeral=True)

    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    medals = ["1.", "2.", "3."]

    for i, c in enumerate(top):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        konsisten = calc_konsisten_hadiah(c["total_clips"])

        if tipe == "clips":
            bonus = f" +{fmt_rp(konsisten['hadiah'])}" if konsisten else ""
            stat = f"Clips: {c['total_clips']}{bonus}"
        elif tipe == "periode":
            stat = f"Views: {fmt_views(c.get('periode_views',0))} | Clips: {c.get('periode_clips',0)}"
        else:
            stat = f"Views: {fmt_views(c['total_views'])} | Gaji: {fmt_rp(c['total_gaji'])}"

        accounts = c.get("accounts", [])
        acc_text = ", ".join([f"@{a['username']}" for a in accounts[:2]])
        embed.add_field(
            name=f"{medal} {c['display_name']}",
            value=f"{acc_text}\n{stat}",
            inline=False
        )

    if tipe == "clips":
        embed.set_footer(text=f"20+ clips = Rp 50.000 bonus | 10+ clips = Rp 25.000 bonus")
    elif tipe == "periode":
        selesai = db["periode"].get("selesai", "")[:10]
        embed.set_footer(text=f"Periode selesai: {selesai}")
    else:
        embed.set_footer(text="100K=50rb | 300K=150rb | 500K=300rb | 1M=700rb")

    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 6 — /update_views
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="update_views", description="Refresh views sebuah clip secara manual")
@app_commands.describe(clip_id="ID clip yang ingin diupdate")
async def update_views(interaction: discord.Interaction, clip_id: int):
    db = load_db()
    clip = next((c for c in db["clips"] if c["id"] == clip_id), None)

    if not clip:
        return await interaction.response.send_message(f"Clip #{clip_id} tidak ditemukan.", ephemeral=True)

    if clip["discord_id"] != str(interaction.user.id) and not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya bisa update clip milikmu sendiri.", ephemeral=True)

    await interaction.response.defer(thinking=True)
    result = await fetch_views(clip["url"])

    if not result["success"]:
        return await interaction.followup.send(f"Gagal: `{result['error']}`")

    old_views = clip["views"]
    new_views = result["views"]
    diff = new_views - old_views
    new_gaji = calc_gaji(new_views)
    gaji_diff = new_gaji - clip["gaji"]
    owner_id = clip["discord_id"]

    for i, c in enumerate(db["clips"]):
        if c["id"] == clip_id:
            db["clips"][i]["views"] = new_views
            db["clips"][i]["gaji"] = new_gaji
            db["clips"][i]["last_updated"] = now_iso()
            break

    if owner_id in db["clippers"]:
        db["clippers"][owner_id]["total_views"] += diff
        if not clip.get("gaji_paid"):
            db["clippers"][owner_id]["pending_gaji"] += gaji_diff

    save_db(db)

    trend = "+" if diff > 0 else ("-" if diff < 0 else "=")
    embed = discord.Embed(title=f"Views Updated — Clip #{clip_id}", color=0x00C9A7)
    embed.add_field(name="Views Lama", value=fmt_views(old_views), inline=True)
    embed.add_field(name="Views Baru", value=fmt_views(new_views), inline=True)
    embed.add_field(name=f"{trend} Pertumbuhan", value=f"{'+' if diff>=0 else ''}{fmt_views(diff)}", inline=True)
    embed.add_field(name="Gaji Lama", value=fmt_rp(clip["gaji"]), inline=True)
    embed.add_field(name="Gaji Baru", value=fmt_rp(new_gaji), inline=True)
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 7 — Periode Gaji
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="buka_periode", description="[ADMIN] Buka periode gaji baru")
@app_commands.describe(nama="Nama periode (misal: April 2025)", durasi="Durasi hari (default: 30)")
async def buka_periode_cmd(interaction: discord.Interaction, nama: str, durasi: int = 30):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    db = load_db()
    if periode_aktif(db):
        return await interaction.response.send_message(
            f"Periode **{db['periode']['nama']}** masih aktif. Tutup dulu dengan `/tutup_periode`.", ephemeral=True
        )
    buka_periode(db, nama, durasi)
    embed = discord.Embed(
        title="Periode Baru Dibuka!",
        description=f"**{nama}** — {durasi} hari\nLeaderboard periode dapat dilihat di `/leaderboard periode`",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="tutup_periode", description="[ADMIN] Tutup periode aktif & umumkan pemenang")
async def tutup_periode_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    db = load_db()
    if not periode_aktif(db):
        return await interaction.response.send_message("Tidak ada periode aktif.", ephemeral=True)

    top = get_periode_leaderboard(db, top_n=5)
    nama = db["periode"]["nama"]
    tutup_periode(db)

    embed = discord.Embed(
        title=f"Periode **{nama}** Selesai!",
        description="Berikut hasil akhir periode:",
        color=0xFEE75C,
        timestamp=datetime.now(timezone.utc)
    )
    medals = ["1.", "2.", "3.", "4.", "5."]
    for i, c in enumerate(top):
        konsisten = calc_konsisten_hadiah(c.get("periode_clips", 0))
        bonus = f" +{fmt_rp(konsisten['hadiah'])}" if konsisten else ""
        embed.add_field(
            name=f"{medals[i]} {c['display_name']}",
            value=f"Views: {fmt_views(c.get('periode_views',0))} | Clips: {c.get('periode_clips',0)}{bonus}",
            inline=False
        )

    recap_ch_id = db["settings"].get("rekap_channel_id", 0)
    if recap_ch_id:
        ch = interaction.guild.get_channel(recap_ch_id)
        if ch:
            await ch.send(embed=embed)

    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Warning & Blacklist
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="warning", description="[ADMIN] Beri warning ke clipper")
@app_commands.describe(member="Clipper yang diberi warning", alasan="Alasan warning")
async def warning_cmd(interaction: discord.Interaction, member: discord.Member, alasan: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    db = load_db()
    if not get_clipper(db, str(member.id)):
        return await interaction.response.send_message("Bukan clipper terdaftar.", ephemeral=True)

    count = add_warning(db, str(member.id), alasan, str(interaction.user))
    auto_bl = count >= MAX_WARNINGS

    embed = discord.Embed(
        title="Warning Diberikan" + (" - AUTO BLACKLIST" if auto_bl else ""),
        color=0xED4245 if auto_bl else 0xFEE75C,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Clipper", value=member.mention, inline=True)
    embed.add_field(name="Warning", value=f"{count}/{MAX_WARNINGS}", inline=True)
    embed.add_field(name="Alasan", value=alasan, inline=False)
    if auto_bl:
        embed.add_field(name="Status", value="Otomatis di-blacklist!", inline=False)
        await remove_clip_role(member, interaction.guild)
    await interaction.response.send_message(embed=embed)

    try:
        dm = discord.Embed(
            title="Kamu Mendapat Warning" + (" — BLACKLIST" if auto_bl else ""),
            description=f"**Alasan:** {alasan}\n**Warning:** {count}/{MAX_WARNINGS}",
            color=0xED4245
        )
        await member.send(embed=dm)
    except Exception:
        pass

@bot.tree.command(name="blacklist", description="[ADMIN] Blacklist clipper secara langsung")
@app_commands.describe(member="Clipper yang di-blacklist", alasan="Alasan blacklist")
async def blacklist_cmd(interaction: discord.Interaction, member: discord.Member, alasan: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    db = load_db()
    blacklist_clipper(db, str(member.id), alasan, str(interaction.user))

    embed = discord.Embed(
        title="Clipper Di-Blacklist",
        description=f"{member.mention} telah diblacklist.\n**Alasan:** {alasan}",
        color=0xED4245,
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.response.send_message(embed=embed)

    # Cabut role dan akses channel
    await remove_clip_role(member, interaction.guild)
    
    ch_id = db["settings"].get("clipper_channel_id", 0)
    if ch_id:
        ch = interaction.guild.get_channel(ch_id)
        if ch:
            try:
                await ch.set_permissions(member, read_messages=False, send_messages=False)
            except Exception:
                pass

@bot.tree.command(name="unblacklist", description="[ADMIN] Cabut blacklist clipper")
@app_commands.describe(member="Clipper yang mau dicabut blacklist-nya")
async def unblacklist_cmd(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    db = load_db()
    unblacklist_clipper(db, str(member.id))

    # Kembalikan role dan akses channel
    await give_clip_role(member, interaction.guild)
    
    ch_id = db["settings"].get("clipper_channel_id", 0)
    if ch_id:
        ch = interaction.guild.get_channel(ch_id)
        if ch:
            try:
                await ch.set_permissions(member, read_messages=True, send_messages=True)
            except Exception:
                pass

    await interaction.response.send_message(
        embed=discord.Embed(
            title="Blacklist Dicabut",
            description=f"{member.mention} bisa aktif kembali sebagai clipper.",
            color=0x57F287
        )
    )

@bot.tree.command(name="hapus_warning", description="[ADMIN] Hapus semua warning clipper")
@app_commands.describe(member="Clipper yang warningnya dihapus")
async def hapus_warning_cmd(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    db = load_db()
    clear_warnings(db, str(member.id))
    await interaction.response.send_message(f"Semua warning {member.mention} dihapus.", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# EDIT CLIPPER — Admin only untuk edit akun
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="edit_clipper", description="[ADMIN] Edit data akun clipper")
@app_commands.describe(
    member="Clipper yang ingin diedit",
    account_id="ID akun yang ingin diedit",
    platform="Platform baru (opsional)",
    username="Username baru (opsional)",
)
@app_commands.choices(platform=[
    app_commands.Choice(name="TikTok", value="tiktok"),
    app_commands.Choice(name="YouTube", value="youtube"),
])
async def edit_clipper_cmd(
    interaction: discord.Interaction,
    member: discord.Member,
    account_id: int,
    platform: str = None,
    username: str = None,
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
        
    db = load_db()
    did = str(member.id)
    clipper = get_clipper(db, did)

    if not clipper:
        return await interaction.response.send_message(
            f"{member.display_name} belum terdaftar sebagai clipper.",
            ephemeral=True
        )

    # Find account
    account = None
    for acc in clipper.get("accounts", []):
        if acc["id"] == account_id:
            account = acc
            break
    
    if not account:
        return await interaction.response.send_message(
            f"Akun #{account_id} tidak ditemukan untuk {member.display_name}.",
            ephemeral=True
        )

    if not platform and not username:
        return await interaction.response.send_message(
            "Isi minimal satu field yang ingin diubah (`platform` atau `username`).",
            ephemeral=True
        )

    old_platform = account["platform"]
    old_username = account["username"]

    # Update
    for i, acc in enumerate(db["clippers"][did]["accounts"]):
        if acc["id"] == account_id:
            if platform:
                db["clippers"][did]["accounts"][i]["platform"] = platform
            if username:
                db["clippers"][did]["accounts"][i]["username"] = username.lstrip("@")
            break

    save_db(db)

    new_account = get_clipper_account(db, did, account_id)

    embed = discord.Embed(
        title="Data Akun Diperbarui",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Clipper", value=member.mention, inline=False)
    embed.add_field(name="Akun ID", value=f"#{account_id}", inline=True)

    if platform:
        embed.add_field(
            name="Platform",
            value=f"~~{old_platform.title()}~~ -> **{new_account['platform'].title()}**",
            inline=True
        )
    if username:
        embed.add_field(
            name="Username",
            value=f"~~@{old_username}~~ -> **@{new_account['username']}**",
            inline=True
        )

    embed.set_footer(text=f"Diubah oleh {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="hapus_akun", description="[ADMIN] Hapus akun clipper")
@app_commands.describe(
    member="Clipper yang akunnya ingin dihapus",
    account_id="ID akun yang ingin dihapus",
)
async def hapus_akun_cmd(interaction: discord.Interaction, member: discord.Member, account_id: int):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    
    db = load_db()
    did = str(member.id)
    clipper = get_clipper(db, did)
    
    if not clipper:
        return await interaction.response.send_message(
            f"{member.display_name} belum terdaftar sebagai clipper.",
            ephemeral=True
        )
    
    accounts = clipper.get("accounts", [])
    if len(accounts) <= 1:
        return await interaction.response.send_message(
            "Tidak bisa menghapus akun terakhir. Gunakan `/blacklist` jika ingin menonaktifkan clipper.",
            ephemeral=True
        )
    
    account = get_clipper_account(db, did, account_id)
    if not account:
        return await interaction.response.send_message(
            f"Akun #{account_id} tidak ditemukan.",
            ephemeral=True
        )
    
    removed = remove_clipper_account(db, did, account_id)
    if removed:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Akun Dihapus",
                description=f"Akun #{account_id} (@{account['username']}) milik {member.mention} telah dihapus.",
                color=0xFEE75C
            )
        )
    else:
        await interaction.response.send_message("Gagal menghapus akun.", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Bayar Gaji
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="bayar", description="[ADMIN] Approve & tandai gaji sudah dibayar")
@app_commands.describe(member="Clipper yang dibayar", catatan="Catatan (opsional)")
async def bayar(interaction: discord.Interaction, member: discord.Member, catatan: str = ""):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)

    db = load_db()
    did = str(member.id)
    clipper = get_clipper(db, did)
    if not clipper:
        return await interaction.response.send_message("Bukan clipper terdaftar.", ephemeral=True)

    pending = clipper["pending_gaji"]
    konsisten_data = calc_konsisten_hadiah(clipper["total_clips"])
    bonus = konsisten_data["hadiah"] if konsisten_data else 0
    total = pending + bonus

    if total == 0:
        return await interaction.response.send_message("Tidak ada gaji pending.", ephemeral=True)

    for i, c in enumerate(db["clips"]):
        if c["discord_id"] == did and not c.get("gaji_paid"):
            db["clips"][i]["gaji_paid"] = True

    db["clippers"][did]["total_gaji"] += total
    db["clippers"][did]["pending_gaji"] = 0
    db["gaji_history"].append({
        "discord_id": did, "clipper_name": clipper["display_name"],
        "amount": total, "gaji_clips": pending, "bonus_konsisten": bonus,
        "approved_by": str(interaction.user), "catatan": catatan, "paid_at": now_iso(),
    })
    save_db(db)

    embed = discord.Embed(title="Pembayaran Berhasil!", color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Clipper", value=member.mention, inline=True)
    embed.add_field(name="Gaji Clip", value=fmt_rp(pending), inline=True)
    if bonus > 0:
        embed.add_field(name=f"Bonus ({konsisten_data['label']})", value=fmt_rp(bonus), inline=True)
    embed.add_field(name="Total", value=f"**{fmt_rp(total)}**", inline=False)
    if catatan:
        embed.add_field(name="Catatan", value=catatan, inline=False)
    embed.set_footer(text=f"Approved by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

    try:
        dm = discord.Embed(title="Gajimu Sudah Ditransfer!", description=f"Total: **{fmt_rp(total)}**", color=0x57F287)
        if bonus > 0:
            dm.add_field(name="Bonus Konsisten", value=fmt_rp(bonus))
        if catatan:
            dm.add_field(name="Catatan Admin", value=catatan)
        await member.send(embed=dm)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# TIKET SYSTEM — Untuk klaim reward (nomor rekening dll)
# ══════════════════════════════════════════════════════════════════════════════

class TicketModal(discord.ui.Modal, title="Buat Tiket Klaim Reward"):
    bank_name = discord.ui.TextInput(
        label="Nama Bank/E-Wallet",
        placeholder="BCA, Mandiri, BNI, Dana, OVO, GoPay, dll",
        required=True,
        max_length=50
    )
    account_number = discord.ui.TextInput(
        label="Nomor Rekening/E-Wallet",
        placeholder="1234567890",
        required=True,
        max_length=30
    )
    account_holder = discord.ui.TextInput(
        label="Nama Pemilik Rekening",
        placeholder="Nama sesuai rekening/e-wallet",
        required=True,
        max_length=100
    )
    phone_number = discord.ui.TextInput(
        label="Nomor WhatsApp",
        placeholder="08xxxxxxxxxx",
        required=True,
        max_length=20
    )
    notes = discord.ui.TextInput(
        label="Catatan (Opsional)",
        placeholder="Catatan tambahan jika ada",
        required=False,
        max_length=200,
        style=discord.TextStyle.paragraph
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        db = load_db()
        did = str(interaction.user.id)
        
        # Check if user is a clipper
        clipper = get_clipper(db, did)
        if not clipper:
            return await interaction.response.send_message(
                "Kamu harus terdaftar sebagai clipper untuk membuat tiket.",
                ephemeral=True
            )
        
        # Create ticket
        ticket = create_ticket(db, did, interaction.user.display_name, {
            "bank_name": self.bank_name.value,
            "account_number": self.account_number.value,
            "account_holder": self.account_holder.value,
            "phone_number": self.phone_number.value,
            "notes": self.notes.value,
        })
        
        embed = discord.Embed(
            title=f"Tiket #{ticket['id']} Dibuat!",
            description="Tiket klaim reward kamu sudah dibuat. Admin akan memproses segera.",
            color=0x57F287,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Bank/E-Wallet", value=self.bank_name.value, inline=True)
        embed.add_field(name="Nomor Rekening", value=f"||{self.account_number.value}||", inline=True)
        embed.add_field(name="Atas Nama", value=self.account_holder.value, inline=True)
        embed.add_field(name="WhatsApp", value=self.phone_number.value, inline=True)
        embed.add_field(name="Gaji Pending", value=fmt_rp(clipper["pending_gaji"]), inline=True)
        if self.notes.value:
            embed.add_field(name="Catatan", value=self.notes.value, inline=False)
        embed.set_footer(text="Status: Open")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Send to ticket channel
        ticket_ch_id = db["settings"].get("ticket_channel_id", 0)
        if ticket_ch_id:
            ch = interaction.guild.get_channel(ticket_ch_id)
            if ch:
                admin_embed = discord.Embed(
                    title=f"Tiket Baru #{ticket['id']}",
                    description=f"**User:** {interaction.user.mention}\n**Display Name:** {interaction.user.display_name}",
                    color=0x5865F2,
                    timestamp=datetime.now(timezone.utc)
                )
                admin_embed.add_field(name="Bank/E-Wallet", value=self.bank_name.value, inline=True)
                admin_embed.add_field(name="Nomor Rekening", value=self.account_number.value, inline=True)
                admin_embed.add_field(name="Atas Nama", value=self.account_holder.value, inline=True)
                admin_embed.add_field(name="WhatsApp", value=self.phone_number.value, inline=True)
                admin_embed.add_field(name="Gaji Pending", value=fmt_rp(clipper["pending_gaji"]), inline=True)
                if self.notes.value:
                    admin_embed.add_field(name="Catatan", value=self.notes.value, inline=False)
                admin_embed.set_footer(text="Gunakan /tiket_proses untuk memproses")
                try:
                    await ch.send(embed=admin_embed)
                except Exception:
                    pass

@bot.tree.command(name="tiket", description="Buat tiket untuk klaim reward (isi data rekening)")
async def tiket_cmd(interaction: discord.Interaction):
    db = load_db()
    did = str(interaction.user.id)
    
    # Check if user is a clipper
    clipper = get_clipper(db, did)
    if not clipper:
        return await interaction.response.send_message(
            "Kamu harus terdaftar sebagai clipper untuk membuat tiket.",
            ephemeral=True
        )
    
    if clipper["pending_gaji"] == 0:
        return await interaction.response.send_message(
            "Kamu tidak memiliki gaji pending untuk diklaim.",
            ephemeral=True
        )
    
    # Check for existing open ticket
    user_tickets = get_user_tickets(db, did)
    open_tickets = [t for t in user_tickets if t["status"] in ("open", "processing")]
    if open_tickets:
        return await interaction.response.send_message(
            f"Kamu sudah memiliki tiket yang belum selesai (#{open_tickets[0]['id']}). Tunggu admin memproses.",
            ephemeral=True
        )
    
    await interaction.response.send_modal(TicketModal())

@bot.tree.command(name="tiket_saya", description="Lihat tiket klaim reward kamu")
async def tiket_saya_cmd(interaction: discord.Interaction):
    db = load_db()
    did = str(interaction.user.id)
    
    tickets = get_user_tickets(db, did)
    if not tickets:
        return await interaction.response.send_message("Kamu belum pernah membuat tiket.", ephemeral=True)
    
    embed = discord.Embed(
        title="Tiket Kamu",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    
    status_emoji = {
        "open": "🟡",
        "processing": "🔵",
        "completed": "🟢",
        "cancelled": "🔴"
    }
    
    for t in sorted(tickets, key=lambda x: x["id"], reverse=True)[:5]:
        emoji = status_emoji.get(t["status"], "⚪")
        embed.add_field(
            name=f"{emoji} Tiket #{t['id']} - {t['status'].upper()}",
            value=(
                f"Bank: {t['bank_name']}\n"
                f"Dibuat: {t['created_at'][:10]}"
                + (f"\nSelesai: {t['completed_at'][:10]}" if t.get('completed_at') else "")
            ),
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="tiket_list", description="[ADMIN] Lihat semua tiket yang terbuka")
async def tiket_list_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    
    db = load_db()
    open_tickets = get_open_tickets(db)
    
    if not open_tickets:
        return await interaction.response.send_message(
            embed=discord.Embed(
                title="Tiket Terbuka",
                description="Tidak ada tiket yang perlu diproses.",
                color=0x57F287
            )
        )
    
    embed = discord.Embed(
        title=f"Tiket Terbuka ({len(open_tickets)})",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    
    for t in open_tickets[:10]:
        clipper = get_clipper(db, t["discord_id"])
        pending = clipper["pending_gaji"] if clipper else 0
        status_emoji = "🟡" if t["status"] == "open" else "🔵"
        
        member = interaction.guild.get_member(int(t["discord_id"]))
        user_text = member.mention if member else t["display_name"]
        
        embed.add_field(
            name=f"{status_emoji} #{t['id']} - {t['display_name']}",
            value=(
                f"User: {user_text}\n"
                f"Bank: {t['bank_name']} - {t['account_number']}\n"
                f"Atas Nama: {t['account_holder']}\n"
                f"WA: {t['phone_number']}\n"
                f"Pending: {fmt_rp(pending)}"
            ),
            inline=False
        )
    
    embed.set_footer(text="Gunakan /tiket_proses untuk memproses tiket")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="tiket_proses", description="[ADMIN] Proses tiket (tandai sebagai processing/completed)")
@app_commands.describe(
    ticket_id="ID tiket",
    status="Status baru tiket"
)
@app_commands.choices(status=[
    app_commands.Choice(name="Processing (Sedang Diproses)", value="processing"),
    app_commands.Choice(name="Completed (Selesai)", value="completed"),
    app_commands.Choice(name="Cancelled (Dibatalkan)", value="cancelled"),
])
async def tiket_proses_cmd(interaction: discord.Interaction, ticket_id: int, status: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    
    db = load_db()
    ticket = get_ticket(db, ticket_id)
    
    if not ticket:
        return await interaction.response.send_message(f"Tiket #{ticket_id} tidak ditemukan.", ephemeral=True)
    
    old_status = ticket["status"]
    updated = update_ticket_status(db, ticket_id, status, str(interaction.user))
    
    status_text = {
        "processing": "Sedang Diproses",
        "completed": "Selesai",
        "cancelled": "Dibatalkan"
    }
    
    embed = discord.Embed(
        title=f"Tiket #{ticket_id} Diupdate",
        description=f"Status: **{old_status.upper()}** -> **{status.upper()}**",
        color=0x57F287 if status == "completed" else (0xFEE75C if status == "processing" else 0xED4245),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="User", value=ticket["display_name"], inline=True)
    embed.add_field(name="Bank", value=ticket["bank_name"], inline=True)
    embed.set_footer(text=f"Diproses oleh {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)
    
    # DM user
    try:
        member = interaction.guild.get_member(int(ticket["discord_id"]))
        if member:
            dm_embed = discord.Embed(
                title=f"Update Tiket #{ticket_id}",
                description=f"Status tiket kamu: **{status_text.get(status, status)}**",
                color=0x57F287 if status == "completed" else 0xFEE75C
            )
            if status == "completed":
                dm_embed.add_field(name="Info", value="Pembayaran telah diproses!", inline=False)
            await member.send(embed=dm_embed)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Setup & Riwayat
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setup", description="[ADMIN] Setup semua channel sistem")
@app_commands.describe(
    clipper_channel="Channel utama clipper",
    gaji_channel="Channel notifikasi gaji",
    rekap_channel="Channel rekap mingguan & periode",
    log_channel="Channel log aktivitas bot",
    approval_channel="Channel untuk approval pendaftaran",
    ticket_channel="Channel untuk tiket klaim reward",
)
async def setup(
    interaction: discord.Interaction,
    clipper_channel: discord.TextChannel,
    gaji_channel: discord.TextChannel,
    rekap_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
    approval_channel: discord.TextChannel = None,
    ticket_channel: discord.TextChannel = None,
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    db = load_db()
    db["settings"]["clipper_channel_id"] = clipper_channel.id
    db["settings"]["gaji_channel_id"] = gaji_channel.id
    db["settings"]["rekap_channel_id"] = rekap_channel.id
    db["settings"]["log_channel_id"] = log_channel.id
    if approval_channel:
        db["settings"]["approval_channel_id"] = approval_channel.id
    if ticket_channel:
        db["settings"]["ticket_channel_id"] = ticket_channel.id
    save_db(db)

    embed = discord.Embed(title="Setup Berhasil!", color=0x57F287)
    embed.add_field(name="Clipper", value=clipper_channel.mention, inline=True)
    embed.add_field(name="Gaji", value=gaji_channel.mention, inline=True)
    embed.add_field(name="Rekap", value=rekap_channel.mention, inline=True)
    embed.add_field(name="Log", value=log_channel.mention, inline=True)
    if approval_channel:
        embed.add_field(name="Approval", value=approval_channel.mention, inline=True)
    if ticket_channel:
        embed.add_field(name="Tiket", value=ticket_channel.mention, inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="riwayat_gaji", description="Lihat riwayat pembayaran gaji")
@app_commands.describe(member="Lihat riwayat gaji orang lain (admin only)")
async def riwayat_gaji(interaction: discord.Interaction, member: discord.Member = None):
    db = load_db()
    if member and not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    target_id = str(member.id) if member else str(interaction.user.id)
    target_name = (member or interaction.user).display_name
    history = [h for h in db["gaji_history"] if h["discord_id"] == target_id]
    if not history:
        return await interaction.response.send_message(f"Belum ada riwayat untuk {target_name}.", ephemeral=True)

    embed = discord.Embed(
        title=f"Riwayat Gaji — {target_name}",
        description=f"Total diterima: **{fmt_rp(sum(h['amount'] for h in history))}**",
        color=0xFEE75C
    )
    for h in reversed(history[-10:]):
        bonus_txt = f" + Bonus {fmt_rp(h.get('bonus_konsisten',0))}" if h.get("bonus_konsisten",0) > 0 else ""
        embed.add_field(
            name=f"{fmt_rp(h['amount'])} — {h['paid_at'][:10]}",
            value=f"Clip: {fmt_rp(h['gaji_clips'])}{bonus_txt} | Oleh: {h['approved_by'].split('#')[0]}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="info_gaji", description="Lihat tabel sistem gaji & bonus")
async def info_gaji(interaction: discord.Interaction):
    embed = discord.Embed(title="Sistem Gaji Campaign Clipper", color=0xFEE75C)
    tabel = "\n".join(f"{t['emoji']} **{t['label']}** -> {fmt_rp(t['gaji'])}" for t in GAJI_TIERS if t["gaji"] > 0)
    embed.add_field(name="Gaji per Views", value=tabel, inline=False)
    bonus = "\n".join(f"**{t['label']}** ({t['min_clips']}+ clips) -> +{fmt_rp(t['hadiah'])}" for t in KONSISTEN_TIERS)
    embed.add_field(name="Bonus Konsisten", value=bonus, inline=False)
    embed.add_field(name="Cara Kerja", value=(
        "1. `!daftar tiktok/youtube @username` (tunggu approval)\n"
        "2. Setelah di-approve, dapat role **Clip**\n"
        "3. `/submit <link>` — bot verifikasi & hitung gaji\n"
        "4. `/tiket` — isi data rekening untuk klaim\n"
        "5. Views auto-update tiap 6 jam\n"
        "6. Admin bayar gaji via `/bayar`"
    ), inline=False)
    embed.set_footer(text="Anti-duplikat & verifikasi aktif — submit hanya video milikmu!")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR — Milestone Notif (dicek saat auto-update)
# ══════════════════════════════════════════════════════════════════════════════

MILESTONES = [100_000, 300_000, 500_000, 1_000_000]

async def check_milestone(clip: dict, old_views: int, new_views: int, guild: discord.Guild, db: dict):
    hit = [m for m in MILESTONES if old_views < m <= new_views]
    if not hit:
        return
    for m in hit:
        if m in clip.get("views_milestones", []):
            continue
        gaji = calc_gaji(m)
        tier = get_tier(m)
        for i, c in enumerate(db["clips"]):
            if c["id"] == clip["id"]:
                db["clips"][i].setdefault("views_milestones", []).append(m)
                break

        gaji_ch_id = db["settings"].get("gaji_channel_id", 0)
        if not gaji_ch_id:
            continue
        ch = guild.get_channel(gaji_ch_id)
        if not ch:
            continue

        try:
            member = guild.get_member(int(clip["discord_id"]))
            mention = member.mention if member else clip["clipper_name"]
            embed = discord.Embed(
                title=f"Milestone {fmt_views(m)} Views Tercapai!",
                description=f"{mention} video-nya tembus **{fmt_views(m)} views**!",
                color=0xFEE75C,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Video", value=clip["title"][:60], inline=False)
            embed.add_field(name=f"{tier['emoji']} Tier Baru", value=tier["label"], inline=True)
            embed.add_field(name="Gaji Naik Ke", value=fmt_rp(gaji), inline=True)
            embed.add_field(name="Link", value=clip["url"], inline=False)
            await ch.send(embed=embed)
        except Exception as e:
            print(f"[BOT] Milestone notif error: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# AUTO UPDATE VIEWS — setiap 6 jam + cek milestone
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(hours=6)
async def auto_update_views():
    db = load_db()
    unpaid = [c for c in db["clips"] if not c.get("gaji_paid")]
    updated = 0
    guild = None
    for g in bot.guilds:
        guild = g
        break

    for clip in unpaid:
        try:
            result = await fetch_views(clip["url"])
            if not result["success"]:
                continue
            new_views = result["views"]
            old_views = clip["views"]
            diff = new_views - old_views
            if diff == 0:
                continue

            new_gaji = calc_gaji(new_views)
            gaji_diff = new_gaji - clip["gaji"]
            owner_id = clip["discord_id"]

            if guild:
                await check_milestone(clip, old_views, new_views, guild, db)

            for i, c in enumerate(db["clips"]):
                if c["id"] == clip["id"]:
                    db["clips"][i]["views"] = new_views
                    db["clips"][i]["gaji"] = new_gaji
                    db["clips"][i]["last_updated"] = now_iso()
                    break

            if owner_id in db["clippers"]:
                db["clippers"][owner_id]["total_views"] += diff
                db["clippers"][owner_id]["pending_gaji"] += gaji_diff
                if periode_aktif(db):
                    db["periode"]["views_periode"][owner_id] = (
                        db["periode"]["views_periode"].get(owner_id, 0) + diff
                    )
            updated += 1
        except Exception as e:
            print(f"[BOT] Auto-update error clip #{clip['id']}: {e}")

    if updated > 0:
        save_db(db)
        print(f"[BOT] Auto-Update: {updated} clips updated")

@auto_update_views.before_loop
async def before_update():
    await bot.wait_until_ready()

# ══════════════════════════════════════════════════════════════════════════════
# REKAP OTOMATIS MINGGUAN — setiap Senin pagi
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(hours=24)
async def weekly_recap():
    now = datetime.now(timezone.utc)
    if now.weekday() != 0:
        return

    db = load_db()
    rekap_ch_id = db["settings"].get("rekap_channel_id", 0)
    if not rekap_ch_id:
        return

    guild = None
    for g in bot.guilds:
        guild = g
        break
    if not guild:
        return

    ch = guild.get_channel(rekap_ch_id)
    if not ch:
        return

    clippers = list(db["clippers"].values())
    if not clippers:
        return

    seminggu_lalu = (now - timedelta(days=7)).isoformat()
    clips_minggu = [c for c in db["clips"] if c.get("submitted_at", "") >= seminggu_lalu]

    total_views_minggu = sum(c["views"] for c in clips_minggu)
    total_clips_minggu = len(clips_minggu)
    total_gaji_pending = sum(c.get("pending_gaji", 0) for c in clippers)

    views_per_clipper = {}
    clips_per_clipper = {}
    for c in clips_minggu:
        did = c["discord_id"]
        views_per_clipper[did] = views_per_clipper.get(did, 0) + c["views"]
        clips_per_clipper[did] = clips_per_clipper.get(did, 0) + 1

    top_minggu = sorted(views_per_clipper.items(), key=lambda x: x[1], reverse=True)[:3]

    embed = discord.Embed(
        title=f"Rekap Mingguan — {now.strftime('%d %b %Y')}",
        description=f"Ringkasan aktivitas clipper 7 hari terakhir",
        color=0x5865F2,
        timestamp=now
    )
    embed.add_field(name="Total Clip Minggu Ini", value=str(total_clips_minggu), inline=True)
    embed.add_field(name="Total Views Minggu Ini", value=fmt_views(total_views_minggu), inline=True)
    embed.add_field(name="Total Gaji Pending", value=fmt_rp(total_gaji_pending), inline=True)
    embed.add_field(name="Total Clipper Aktif", value=str(len([c for c in clippers if c.get("active", True)])), inline=True)

    if top_minggu:
        top_txt = ""
        medals = ["1.", "2.", "3."]
        for i, (did, views) in enumerate(top_minggu):
            clipper = db["clippers"].get(did, {})
            name = clipper.get("display_name", "Unknown")
            clips_count = clips_per_clipper.get(did, 0)
            top_txt += f"{medals[i]} **{name}** — {fmt_views(views)} views ({clips_count} clips)\n"
        embed.add_field(name="Top Clipper Minggu Ini", value=top_txt, inline=False)

    top_pending = sorted(clippers, key=lambda x: x.get("pending_gaji", 0), reverse=True)[:3]
    if any(c["pending_gaji"] > 0 for c in top_pending):
        pending_txt = "\n".join(
            f"- **{c['display_name']}** — {fmt_rp(c['pending_gaji'])}"
            for c in top_pending if c["pending_gaji"] > 0
        )
        embed.add_field(name="Gaji Menunggu Approval", value=pending_txt, inline=False)

    embed.set_footer(text="Rekap otomatis setiap Senin - Campaign Clipper System")
    await ch.send(embed=embed)
    print(f"[BOT] Rekap Mingguan terkirim ke #{ch.name}")

@weekly_recap.before_loop
async def before_rekap():
    await bot.wait_until_ready()

# ══════════════════════════════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not TOKEN:
        print("[BOT] Set DISCORD_TOKEN di environment variable!")
    else:
        bot.run(TOKEN)
