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
    get_active_gaji_tiers, get_active_konsisten_tiers,
    # New imports for multi-account & approval system
    add_pending_registration, get_pending_registration, get_all_pending_registrations,
    approve_registration, reject_registration, cancel_pending_account,
    get_all_accounts, add_clipper_account, remove_clipper_account,
    get_clipper_account, get_clipper_account_by_username,
    check_account_ownership, get_matching_account,
    # Ticket system
    create_ticket, get_ticket, get_user_tickets, get_open_tickets,
    update_ticket_status, update_ticket_data,
    # Clip management
    get_clip_by_id, get_clips_by_user, delete_clip, update_clip,
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

async def give_clip_role(member: discord.Member, guild: discord.Guild) -> tuple[bool, str]:
    """Give Clip role to member. Returns (success, message)"""
    print(f"[v0] Mencari role dengan nama: '{CLIP_ROLE_NAME}'")
    print(f"[v0] Semua role di server: {[r.name for r in guild.roles]}")
    
    role = discord.utils.get(guild.roles, name=CLIP_ROLE_NAME)
    
    if not role:
        print(f"[v0] Role '{CLIP_ROLE_NAME}' TIDAK DITEMUKAN!")
        return False, f"Role '{CLIP_ROLE_NAME}' tidak ditemukan di server. Buat role dengan nama persis '{CLIP_ROLE_NAME}' atau set CLIP_ROLE_NAME di environment variable."
    
    print(f"[v0] Role ditemukan: {role.name} (ID: {role.id})")
    
    # Check bot permission
    bot_member = guild.me
    if not bot_member.guild_permissions.manage_roles:
        print(f"[v0] Bot tidak punya permission Manage Roles!")
        return False, "Bot tidak punya permission 'Manage Roles'."
    
    # Check role hierarchy
    if role >= bot_member.top_role:
        print(f"[v0] Role {role.name} lebih tinggi dari bot role!")
        return False, f"Role '{role.name}' posisinya lebih tinggi dari role bot. Pindahkan role bot ke atas role '{role.name}'."
    
    try:
        await member.add_roles(role, reason="Approved as Clipper")
        print(f"[v0] Berhasil memberikan role {role.name} ke {member.display_name}")
        return True, f"Role '{role.name}' berhasil diberikan!"
    except discord.Forbidden as e:
        print(f"[v0] Forbidden error: {e}")
        return False, f"Bot tidak punya izin untuk memberikan role ini. Error: {e}"
    except Exception as e:
        print(f"[v0] Error: {e}")
        return False, f"Gagal memberikan role: {e}"

async def remove_clip_role(member: discord.Member, guild: discord.Guild) -> tuple[bool, str]:
    """Remove Clip role from member. Returns (success, message)"""
    role = discord.utils.get(guild.roles, name=CLIP_ROLE_NAME)
    if not role:
        return False, f"Role '{CLIP_ROLE_NAME}' tidak ditemukan."
    try:
        await member.remove_roles(role, reason="Removed as Clipper")
        return True, f"Role '{role.name}' berhasil dihapus."
    except discord.Forbidden:
        return False, "Bot tidak punya izin untuk menghapus role ini."
    except Exception as e:
        return False, f"Gagal menghapus role: {e}"

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
        print(f"[BOT] {len(synced)} commands synced globally")
        # Print all registered commands for debugging
        for cmd in synced:
            print(f"[BOT] - /{cmd.name}")
    except Exception as e:
        print(f"[BOT] Sync error: {e}")
    auto_update_views.start()
    weekly_recap.start()


# ── FORCE SYNC COMMAND (untuk admin) ─────────────────────────────────────────

@bot.command(name="sync")
async def force_sync(ctx):
    """Force sync slash commands - admin only"""
    if not is_admin(ctx.author):
        return await ctx.send("Hanya admin yang bisa sync commands.")
    
    await ctx.send("Syncing commands...")
    try:
        # Sync to current guild (instant) and globally
        if ctx.guild:
            bot.tree.copy_global_to(guild=ctx.guild)
            guild_synced = await bot.tree.sync(guild=ctx.guild)
            print(f"[BOT] {len(guild_synced)} commands synced to guild {ctx.guild.name}")
        
        global_synced = await bot.tree.sync()
        print(f"[BOT] {len(global_synced)} commands synced globally")
        
        # List all commands
        cmd_list = "\n".join([f"- /{cmd.name}" for cmd in global_synced])
        await ctx.send(f"Synced {len(global_synced)} commands!\n```\n{cmd_list}\n```")
    except Exception as e:
        await ctx.send(f"Sync error: {e}")
        print(f"[BOT] Sync error: {e}")


# ══════════════════════════════════════════════════════════════════════════���═══
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

    # Kirim notifikasi ke log_channel (channel admin)
    await send_log(ctx.guild, db, embed=discord.Embed(
        title="Pendaftaran Clipper Baru",
        description=(
            f"**User:** {ctx.author.mention}\n"
            f"**User ID:** {ctx.author.id}\n"
            f"**Akun Baru:** @{username} ({platform.title()})\n\n"
            f"**Semua akun pending:**\n{accounts_list}\n\n"
            f"Gunakan `/approve @user` untuk menyetujui atau `/reject @user` untuk menolak."
        ),
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
    
    # Notifikasi ke log_channel (channel admin)
    already_clipper = "Ya" if clipper else "Belum"
    await send_log(interaction.guild, db, embed=discord.Embed(
        title="Permintaan Tambah Akun",
        description=(
            f"**User:** {interaction.user.mention}\n"
            f"**User ID:** {interaction.user.id}\n"
            f"**Sudah Clipper:** {already_clipper}\n"
            f"**Akun Baru:** @{username} ({platform.title()})\n\n"
            f"**Semua akun pending:**\n{accounts_list}\n\n"
            f"Gunakan `/approve @user` untuk menyetujui."
        ),
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    ))

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Approve & Reject Registration
# ═══════════════════════════════════════��══════════════════════════════════════

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
    role_success, role_message = await give_clip_role(member, interaction.guild)
    role_msg = f"\n{role_message}"
    
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
    
    # Response ephemeral untuk admin
    admin_embed = discord.Embed(
        title="Clipper Berhasil Disetujui!",
        description=(
            f"**User:** {member.mention}\n\n"
            f"**Akun Terdaftar:**\n{accounts_list}\n"
            f"{role_msg}{access_msg}"
        ),
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    admin_embed.set_footer(text=f"Approved by {interaction.user.display_name}")
    await interaction.response.send_message(embed=admin_embed, ephemeral=True)
    
    # Announce di channel utama clipper
    clipper_ch_id = db["settings"].get("clipper_channel_id", 0)
    if clipper_ch_id:
        clipper_ch = interaction.guild.get_channel(clipper_ch_id)
        if clipper_ch:
            announce_embed = discord.Embed(
                title="Selamat Datang Clipper Baru!",
                description=(
                    f"Selamat! {member.mention} resmi bergabung sebagai **Clipper**!\n\n"
                    f"**Akun:**\n{accounts_list}\n\n"
                    f"Selamat berkarya!"
                ),
                color=0x57F287,
                timestamp=datetime.now(timezone.utc)
            )
            announce_embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await clipper_ch.send(embed=announce_embed)
            except Exception:
                pass
    
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
    
    # Log ke log_channel
    await send_log(interaction.guild, db, embed=discord.Embed(
        title="Clipper Approved",
        description=f"{member.mention} disetujui oleh {interaction.user.mention}\n\n**Akun:**\n{accounts_list}",
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
    
    # Response ephemeral untuk admin
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
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
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
    
    # Log ke log_channel
    await send_log(interaction.guild, db, embed=discord.Embed(
        title="Pendaftaran Ditolak",
        description=f"{member.mention} ditolak oleh {interaction.user.mention}\n\n**Alasan:** {alasan}",
        color=0xED4245,
        timestamp=datetime.now(timezone.utc)
    ))

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
            ),
            ephemeral=True
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
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
        # Jika confidence unknown, jangan beri warning - mungkin masalah teknis
        accounts_list = ", ".join([f"@{acc['username']}" for acc in accounts])
        found_user = verify_result.get('found_username', '') if verify_result else ''
        
        if verify_result and verify_result["confidence"] != "unknown" and found_user:
            # Username terdeteksi tapi tidak cocok - ini curiga
            warn_count = add_warning(db, did, f"Submit video bukan miliknya: {url}", "System")
            warn_msg = (
                f"\n\nWarning **{warn_count}/{MAX_WARNINGS}** diberikan."
                + ("\n**Auto-blacklist setelah 1 warning lagi!**" if warn_count == MAX_WARNINGS - 1 else "")
                + ("\n**Kamu telah di-blacklist!**" if warn_count >= MAX_WARNINGS else "")
            )
            embed = discord.Embed(
                title="Verifikasi Gagal - Username Tidak Cocok",
                description=(
                    f"Video ini **tidak cocok** dengan akun yang kamu daftarkan.\n\n"
                    f"**Akun {detected_platform.title()} Terdaftar:** {accounts_list}\n"
                    f"**Username di Video:** @{found_user}\n"
                    f"{warn_msg}"
                ),
                color=0xED4245
            )
        else:
            # Tidak bisa detect username - kemungkinan masalah teknis
            embed = discord.Embed(
                title="Verifikasi Gagal - Tidak Bisa Mendeteksi",
                description=(
                    f"Bot tidak bisa memverifikasi kepemilikan video ini.\n\n"
                    f"**Akun {detected_platform.title()} Terdaftar:** {accounts_list}\n"
                    f"**Kemungkinan penyebab:**\n"
                    f"- Video private/restricted\n"
                    f"- TikTok memblokir request\n"
                    f"- Format URL tidak dikenali\n\n"
                    f"**Solusi:** Minta admin untuk submit dengan `/admin_submit`\n"
                    f"atau coba kirim link full (bukan short link)."
                ),
                color=0xFEE75C
            )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Fetch views ──────────────��────────────────────────────────────────────
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

    # Hitung progress bonus konsisten
    total_clips_now = db["clippers"][did]["total_clips"]
    active_konsisten = get_active_konsisten_tiers(db)
    konsisten_now = calc_konsisten_hadiah(total_clips_now, db)
    # Cari tier berikutnya
    next_tier = None
    for kt in sorted(active_konsisten, key=lambda x: x["min_clips"]):
        if kt["min_clips"] > total_clips_now:
            next_tier = kt
            break

    embed = discord.Embed(
        title="✅ Clip Berhasil Disubmit!",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=result.get("thumbnail", ""))
    embed.add_field(name="🎬 Judul", value=result.get("title", "Unknown")[:80], inline=False)
    embed.add_field(name="��️ Views", value=fmt_views(views), inline=True)
    embed.add_field(name=f"{tier['emoji']} Tier Gaji", value=tier["label"], inline=True)
    embed.add_field(name="💰 Estimasi Gaji", value=fmt_rp(gaji) if gaji > 0 else "Belum 100K", inline=True)
    embed.add_field(name="🎬 Total Clip Kamu", value=f"{total_clips_now} clip", inline=True)
    # Status bonus konsisten
    if konsisten_now:
        embed.add_field(name="🏅 Bonus Konsisten", value=f"{konsisten_now['label']} (+{fmt_rp(konsisten_now['hadiah'])})", inline=True)
    elif next_tier:
        sisa = next_tier["min_clips"] - total_clips_now
        embed.add_field(name="🏅 Progress Bonus", value=f"**{sisa} clip lagi** untuk {next_tier['label']} (+{fmt_rp(next_tier['hadiah'])})", inline=True)
    embed.add_field(name="📌 Clip ID", value=f"#{clip_data['id']}", inline=True)
    embed.set_footer(text=f"Submit oleh {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN SUBMIT — Admin submit clip atas nama clipper (bypass verifikasi)
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="admin_submit", description="[ADMIN] Submit clip atas nama clipper (bypass verifikasi)")
@app_commands.describe(
    member="Clipper pemilik clip",
    url="Link video TikTok atau YouTube",
    alasan="Alasan bypass verifikasi",
)
async def admin_submit(interaction: discord.Interaction, member: discord.Member, url: str, alasan: str = "Dijamin admin"):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    did = str(member.id)
    clipper = get_clipper(db, did)
    if not clipper:
        return await interaction.response.send_message(f"❌ {member.display_name} belum terdaftar.", ephemeral=True)
    if is_duplicate_url(db, url):
        existing = next((c for c in db["clips"] if c["url"].strip().rstrip("/").lower() == url.strip().rstrip("/").lower()), None)
        return await interaction.response.send_message(f"❌ URL sudah ada di database (Clip #{existing['id']}).", ephemeral=True)
    await interaction.response.defer(thinking=True)
    if "tiktok.com" in url:
        detected_platform = "tiktok"
    elif "youtu" in url:
        detected_platform = "youtube"
    else:
        return await interaction.followup.send("❌ Platform tidak dikenali.", ephemeral=True)
    result = await fetch_views(url)
    if not result["success"]:
        return await interaction.followup.send(f"❌ Gagal ambil views: `{result['error']}`", ephemeral=True)
    views = result["views"]
    gaji = calc_gaji(views)
    tier = get_tier(views)
    accounts = clipper.get("accounts", [])
    matched_account = next((a for a in accounts if a["platform"] == detected_platform), accounts[0] if accounts else {"id": 0, "username": "unknown"})
    clip_data = {
        "id": len(db["clips"]) + 1,
        "discord_id": did,
        "clipper_name": clipper["display_name"],
        "account_id": matched_account.get("id", 0),
        "account_username": matched_account.get("username", "unknown"),
        "platform": detected_platform,
        "url": url,
        "title": result.get("title", "Unknown"),
        "thumbnail": result.get("thumbnail", ""),
        "views": views,
        "views_milestones": [],
        "gaji": gaji,
        "gaji_paid": False,
        "submitted_at": now_iso(),
        "last_updated": now_iso(),
        "submitted_by_admin": str(interaction.user),
        "admin_alasan": alasan,
    }
    db["clips"].append(clip_data)
    db["clippers"][did]["total_clips"] += 1
    db["clippers"][did]["total_views"] += views
    db["clippers"][did]["pending_gaji"] += gaji
    if periode_aktif(db):
        tambah_periode_stats(db, did, views)
    save_db(db)
    embed = discord.Embed(title="✅ Clip Disubmit oleh Admin!", color=0x5865F2, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=result.get("thumbnail", ""))
    embed.add_field(name="👤 Clipper", value=member.mention, inline=True)
    embed.add_field(name="🎬 Judul", value=result.get("title", "Unknown")[:80], inline=False)
    embed.add_field(name="👁️ Views", value=fmt_views(views), inline=True)
    embed.add_field(name=f"{tier['emoji']} Tier", value=tier["label"], inline=True)
    embed.add_field(name="💰 Estimasi Gaji", value=fmt_rp(gaji) if gaji > 0 else "Belum 100K", inline=True)
    embed.add_field(name="📌 Clip ID", value=f"#{clip_data['id']}", inline=True)
    embed.add_field(name="📝 Alasan Bypass", value=alasan, inline=False)
    embed.set_footer(text=f"Disubmit oleh Admin {interaction.user.display_name}")
    await interaction.followup.send(embed=embed)
    await send_log(interaction.guild, db, embed=discord.Embed(
        title="⚠️ Admin Submit (Bypass Verifikasi)",
        description=f"**Admin:** {interaction.user.mention}\n**Clipper:** {member.mention}\n**URL:** {url}\n**Views:** {fmt_views(views)}\n**Alasan:** {alasan}",
        color=0xFEE75C, timestamp=datetime.now(timezone.utc)
    ))
    try:
        dm = discord.Embed(title="✅ Clip Kamu Disubmit oleh Admin", description=f"Admin **{interaction.user.display_name}** submit clip atas namamu.", color=0x57F287)
        dm.add_field(name="👁️ Views", value=fmt_views(views), inline=True)
        dm.add_field(name="💰 Estimasi Gaji", value=fmt_rp(gaji), inline=True)
        dm.add_field(name="📌 Clip ID", value=f"#{clip_data['id']}", inline=True)
        await member.send(embed=dm)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# PANDUAN CLIPPER
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="panduan", description="Panduan lengkap cara menjadi clipper")
async def panduan_clipper(interaction: discord.Interaction):
    db = load_db()
    embed1 = discord.Embed(title="📖 Panduan Clipper — Cara Bergabung", description="Ikuti langkah berikut untuk mulai sebagai clipper.", color=0x5865F2, timestamp=datetime.now(timezone.utc))
    embed1.add_field(name="📝 Step 1 — Daftar", value="`!daftar tiktok @username`\n`!daftar youtube NamaChannel`\n\nPendaftaran masuk antrian, **menunggu persetujuan admin**.", inline=False)
    embed1.add_field(name="✅ Step 2 — Tunggu Approval", value="Admin review pendaftaranmu.\nKamu dapat **notif DM** saat disetujui/ditolak.\nSetelah approved → otomatis dapat akses channel clipper.", inline=False)
    embed1.add_field(name="🎬 Step 3 — Submit Clip", value="`/submit url:https://tiktok.com/@user/video/xxx`\n\nBot otomatis verifikasi, ambil views, & hitung gaji.", inline=False)
    active_tiers = get_active_gaji_tiers(db)
    active_konsisten = get_active_konsisten_tiers(db)
    embed2 = discord.Embed(title="💰 Sistem Gaji & Bonus", color=0xFEE75C)
    tabel = "\n".join(f"{t.get('emoji','⭐')} **{t['label']} views** → {fmt_rp(t['gaji'])}" for t in active_tiers if t["gaji"] > 0)
    embed2.add_field(name="📊 Gaji per Views", value=tabel, inline=False)
    bonus_k = "\n".join(f"**{t['label']}** ({t['min_clips']}+ clip) → +{fmt_rp(t['hadiah'])}" for t in active_konsisten)
    embed2.add_field(name="🏅 Bonus Konsisten", value=bonus_k, inline=False)
    embed2.add_field(name="⏰ Kapan Dibayar?", value="Gaji dibayar setelah admin approve.\nViews auto-update tiap **6 jam**.\nCek pending di `/profil`.", inline=False)
    embed3 = discord.Embed(title="📋 Command & Aturan", color=0x57F287)
    embed3.add_field(name="Command Clipper", value="`!daftar` `!tambah_akun` `/submit` `/profil`\n`/leaderboard` `/riwayat_gaji` `/info_gaji`\n`/update_views` `/panduan`", inline=False)
    embed3.add_field(name="⚠️ Aturan Penting", value="▸ Hanya submit video **milikmu sendiri**\n▸ Dilarang submit URL yang sama dua kali\n▸ **3 warning = auto blacklist**\n▸ Video harus **public** (tidak private)", inline=False)
    await interaction.response.send_message(embeds=[embed1, embed2, embed3])


# ══════════════════════════════════════════════════════════════════════════════
# FITUR 3 — /profil dengan multi-akun display
# ══════════════════════════════════════════════════════════���═══════════════════

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

# ════════════════════════════════════───═════════════════════════════════════════
# FITUR — /help untuk panduan command
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="help", description="Lihat panduan semua command")
async def help_cmd(interaction: discord.Interaction):
    """Show help for all commands"""
    is_admin_user = is_admin(interaction.user)
    has_clip = has_clip_role(interaction.user)
    
    embed = discord.Embed(
        title="📖 Panduan Command Bot Clipper",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    
    # For everyone
    embed.add_field(
        name="👤 UNTUK SEMUA CLIPPER",
        value=(
            "**`!daftar <platform> <@username>`** - Daftar sebagai clipper\n"
            "**`/add <platform> <@username>`** - Tambah akun baru\n"
            "**`/akun`** - Lihat semua akun kamu & akun pending\n"
            "**`/submit <clip_link>`** - Submit clip untuk review\n"
            "**`/clip_saya`** - Lihat semua clip yang sudah disubmit\n"
            "**`/hapus_clip <id>`** - Hapus clip kamu (pending only)\n"
            "**`/tiket`** - Buat tiket klaim reward\n"
            "**`/tiket_saya`** - Lihat status tiket kamu\n"
            "**`/profil`** - Lihat profil & statistik kamu\n"
            "**`/stats`** - Lihat statistik global sistem"
        ),
        inline=False
    )
    
    # For admin only
    if is_admin_user:
        embed.add_field(
            name="🔨 UNTUK ADMIN",
            value=(
                "**`/setup`** - Setup channel bot\n"
                "**`/pending`** - Lihat pendaftaran menunggu\n"
                "**`/approve @user`** - Setujui pendaftaran\n"
                "**`/reject @user`** - Tolak pendaftaran\n"
                "**`/admin_submit @user <url>`** - Submit atas nama clipper\n"
                "**`/edit_clip <id>`** - Edit data clip\n"
                "**`/hapus_clip <id>`** - Hapus clip (termasuk paid)\n"
                "**`/bayar @user`** - Bayar gaji clipper\n"
                "**`/tiket_list`** - Lihat tiket klaim\n"
                "**`/tiket_proses <id>`** - Update tiket\n"
                "**`/warning @user`** - Beri warning\n"
                "**`/blacklist @user`** - Blacklist clipper\n"
                "**`/info_gaji`** - Lihat/edit tier gaji"
            ),
            inline=False
        )
    
    # Channel info
    embed.add_field(
        name="📢 CHANNEL USAGE",
        value=(
            "**Clipper Channel** - Pengumuman welcome clipper baru\n"
            "**Gaji Channel** - Notifikasi gaji sudah dibayar\n"
            "**Rekap Channel** - Rekap mingguan & periode\n"
            "**Log Bot** - Admin stuff (approval, tiket, pembayaran)"
        ),
        inline=False
    )
    
    embed.set_footer(text="Ketik /help anytime untuk bantuan")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR — /clip_saya untuk lihat semua clip yang sudah disubmit
# ��═════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="clip_saya", description="Lihat semua clip yang sudah kamu submit")
@app_commands.describe(
    page="Halaman (default: 1)",
    member="Lihat clip user lain (admin only)"
)
async def clip_saya(interaction: discord.Interaction, page: int = 1, member: discord.Member = None):
    db = load_db()
    
    # Admin bisa lihat clip user lain
    if member and not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin yang bisa lihat clip user lain.", ephemeral=True)
    
    target = member or interaction.user
    did = str(target.id)
    clips = get_clips_by_user(db, did)
    
    if not clips:
        return await interaction.response.send_message(
            f"{'Kamu' if not member else target.display_name} belum punya clip yang disubmit.", 
            ephemeral=True
        )
    
    # Sort by newest first
    clips = sorted(clips, key=lambda x: x.get("submitted_at", ""), reverse=True)
    
    # Pagination
    per_page = 5
    total_pages = (len(clips) + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_clips = clips[start:start + per_page]
    
    embed = discord.Embed(
        title=f"Clip {'Kamu' if not member else target.display_name} ({len(clips)} total)",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    
    for clip in page_clips:
        status = "Paid" if clip.get("gaji_paid") else "Pending"
        platform_icon = "tiktok" if clip.get("platform") == "tiktok" else "youtube"
        submitted = clip.get("submitted_at", "")[:10]
        
        embed.add_field(
            name=f"#{clip['id']} | {fmt_views(clip['views'])} views | {fmt_rp(clip.get('gaji', 0))} ({status})",
            value=(
                f"**Platform:** {platform_icon.title()} (@{clip.get('account_username', '?')})\n"
                f"**Judul:** {clip.get('title', 'Unknown')[:50]}...\n"
                f"**Submitted:** {submitted}\n"
                f"[Link Video]({clip['url']})"
            ),
            inline=False
        )
    
    embed.set_footer(text=f"Halaman {page}/{total_pages} | Gunakan /clip_saya page:<nomor> untuk halaman lain")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# FITUR — /hapus_clip untuk hapus clip
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="hapus_clip", description="Hapus clip yang sudah disubmit")
@app_commands.describe(
    clip_id="ID clip yang mau dihapus (lihat di /clip_saya)",
    alasan="Alasan penghapusan (opsional)"
)
async def hapus_clip(interaction: discord.Interaction, clip_id: int, alasan: str = ""):
    db = load_db()
    did = str(interaction.user.id)
    
    clip = get_clip_by_id(db, clip_id)
    if not clip:
        return await interaction.response.send_message(f"Clip #{clip_id} tidak ditemukan.", ephemeral=True)
    
    # Check ownership (clipper bisa hapus clipnya sendiri, admin bisa hapus semua)
    is_owner = clip["discord_id"] == did
    is_admin_user = is_admin(interaction.user)
    
    if not is_owner and not is_admin_user:
        return await interaction.response.send_message(
            f"Kamu tidak bisa menghapus clip milik orang lain. Clip #{clip_id} milik <@{clip['discord_id']}>.",
            ephemeral=True
        )
    
    # Check if already paid (only admin can delete paid clips)
    if clip.get("gaji_paid") and not is_admin_user:
        return await interaction.response.send_message(
            "Clip yang sudah dibayar tidak bisa dihapus. Hubungi admin jika ada masalah.",
            ephemeral=True
        )
    
    # Delete the clip
    deleted = delete_clip(db, clip_id, str(interaction.user))
    
    embed = discord.Embed(
        title=f"Clip #{clip_id} Dihapus",
        color=0xED4245,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Judul", value=deleted.get("title", "Unknown")[:50], inline=False)
    embed.add_field(name="Views", value=fmt_views(deleted["views"]), inline=True)
    embed.add_field(name="Gaji", value=fmt_rp(deleted.get("gaji", 0)), inline=True)
    embed.add_field(name="Status", value="Sudah Paid" if deleted.get("gaji_paid") else "Pending", inline=True)
    embed.add_field(name="Dihapus oleh", value=interaction.user.mention, inline=True)
    if alasan:
        embed.add_field(name="Alasan", value=alasan, inline=False)
    embed.set_footer(text="Stats clipper sudah diupdate otomatis")
    
    await interaction.response.send_message(embed=embed, ephemeral=not is_admin_user)
    
    # Log to admin channel
    await send_log(interaction.guild, db, embed=discord.Embed(
        title="Clip Dihapus",
        description=(
            f"**Clip ID:** #{clip_id}\n"
            f"**Pemilik:** <@{deleted['discord_id']}>\n"
            f"**Dihapus oleh:** {interaction.user.mention}\n"
            f"**Views:** {fmt_views(deleted['views'])}\n"
            f"**Gaji:** {fmt_rp(deleted.get('gaji', 0))}\n"
            f"**Alasan:** {alasan or 'Tidak disebutkan'}"
        ),
        color=0xED4245,
        timestamp=datetime.now(timezone.utc)
    ))


# ══════════════════════════════════════════════════════════════════════════════
# FITUR — /edit_clip (ADMIN) untuk edit clip
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="edit_clip", description="[ADMIN] Edit data clip")
@app_commands.describe(
    clip_id="ID clip yang mau diedit",
    views="Views baru (opsional)",
    gaji="Gaji baru (opsional)",
    paid="Status pembayaran (opsional)"
)
@app_commands.choices(paid=[
    app_commands.Choice(name="Sudah dibayar", value="true"),
    app_commands.Choice(name="Belum dibayar", value="false"),
])
async def edit_clip_cmd(
    interaction: discord.Interaction, 
    clip_id: int, 
    views: int = None, 
    gaji: int = None,
    paid: app_commands.Choice[str] = None
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    
    db = load_db()
    clip = get_clip_by_id(db, clip_id)
    
    if not clip:
        return await interaction.response.send_message(f"Clip #{clip_id} tidak ditemukan.", ephemeral=True)
    
    updates = {}
    changes = []
    
    if views is not None:
        old_views = clip["views"]
        updates["views"] = views
        changes.append(f"Views: {fmt_views(old_views)} -> {fmt_views(views)}")
    
    if gaji is not None:
        old_gaji = clip.get("gaji", 0)
        updates["gaji"] = gaji
        changes.append(f"Gaji: {fmt_rp(old_gaji)} -> {fmt_rp(gaji)}")
    
    if paid is not None:
        old_paid = clip.get("gaji_paid", False)
        new_paid = paid.value == "true"
        updates["gaji_paid"] = new_paid
        changes.append(f"Status: {'Paid' if old_paid else 'Pending'} -> {'Paid' if new_paid else 'Pending'}")
    
    if not updates:
        return await interaction.response.send_message("Tidak ada yang diubah.", ephemeral=True)
    
    updated = update_clip(db, clip_id, updates)
    
    embed = discord.Embed(
        title=f"Clip #{clip_id} Diupdate",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Pemilik", value=f"<@{clip['discord_id']}>", inline=True)
    embed.add_field(name="Perubahan", value="\n".join(changes), inline=False)
    embed.set_footer(text=f"Diedit oleh {interaction.user.display_name}")
    
    await interaction.response.send_message(embed=embed)
    
    # Log
    await send_log(interaction.guild, db, embed=embed)


# ══════════════════════════════════════════════════════════════════════════════
# FITUR — /stats untuk statistik global
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="stats", description="Lihat statistik global sistem clipper")
async def stats_cmd(interaction: discord.Interaction):
    db = load_db()
    
    clippers = list(db["clippers"].values())
    clips = db["clips"]
    
    total_clippers = len(clippers)
    active_clippers = len([c for c in clippers if c.get("active", True)])
    total_clips = len(clips)
    total_views = sum(c.get("total_views", 0) for c in clippers)
    total_gaji_paid = sum(c.get("total_gaji", 0) for c in clippers)
    total_pending = sum(c.get("pending_gaji", 0) for c in clippers)
    
    # Clips this week
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat()
    clips_this_week = [c for c in clips if c.get("submitted_at", "") >= week_ago]
    views_this_week = sum(c["views"] for c in clips_this_week)
    
    # Clips this month
    month_ago = (now - timedelta(days=30)).isoformat()
    clips_this_month = [c for c in clips if c.get("submitted_at", "") >= month_ago]
    views_this_month = sum(c["views"] for c in clips_this_month)
    
    # Platform breakdown
    tiktok_clips = len([c for c in clips if c.get("platform") == "tiktok"])
    youtube_clips = len([c for c in clips if c.get("platform") == "youtube"])
    
    # Top performer
    top_clipper = max(clippers, key=lambda x: x.get("total_views", 0)) if clippers else None
    
    embed = discord.Embed(
        title="Statistik Global Clipper System",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.add_field(name="Total Clipper", value=f"{active_clippers} aktif / {total_clippers} total", inline=True)
    embed.add_field(name="Total Clip", value=str(total_clips), inline=True)
    embed.add_field(name="Total Views", value=fmt_views(total_views), inline=True)
    
    embed.add_field(name="Gaji Sudah Dibayar", value=fmt_rp(total_gaji_paid), inline=True)
    embed.add_field(name="Gaji Pending", value=fmt_rp(total_pending), inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    
    embed.add_field(name="Clip Minggu Ini", value=f"{len(clips_this_week)} clip ({fmt_views(views_this_week)} views)", inline=True)
    embed.add_field(name="Clip Bulan Ini", value=f"{len(clips_this_month)} clip ({fmt_views(views_this_month)} views)", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    
    embed.add_field(name="Platform", value=f"TikTok: {tiktok_clips}\nYouTube: {youtube_clips}", inline=True)
    
    if top_clipper:
        embed.add_field(
            name="Top Clipper (All Time)", 
            value=f"**{top_clipper['display_name']}**\n{fmt_views(top_clipper['total_views'])} views | {top_clipper['total_clips']} clips", 
            inline=True
        )
    
    # Periode info
    if periode_aktif(db):
        p = db["periode"]
        embed.add_field(
            name="Periode Aktif", 
            value=f"**{p['nama']}**\n{p['mulai'][:10]} - {p['selesai'][:10]}", 
            inline=False
        )
    
    embed.set_footer(text="Campaign Clipper System")
    await interaction.response.send_message(embed=embed)


# ════════════════════════════════════───═════════════════════════════════════════
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
@app_commands.describe(
    nama="Nama periode (misal: April 2025)",
    durasi="Durasi hari (default: 30)",
    note="Catatan/pengumuman untuk clipper (opsional)",
    target_views="Target views periode ini (opsional, misal: 1000000)",
    hadiah_tambahan="Hadiah tambahan selain gaji (opsional, misal: Bonus Rp 100.000)",
)
async def buka_periode_cmd(
    interaction: discord.Interaction,
    nama: str,
    durasi: int = 30,
    note: str = "",
    target_views: str = "",
    hadiah_tambahan: str = "",
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    if periode_aktif(db):
        return await interaction.response.send_message(
            f"⚠️ Periode **{db['periode']['nama']}** masih aktif. Tutup dulu dengan `/tutup_periode`.", ephemeral=True
        )
    buka_periode(db, nama, durasi)

    # Simpan metadata periode tambahan
    db["periode"]["note"] = note
    db["periode"]["target_views"] = target_views
    db["periode"]["hadiah_tambahan"] = hadiah_tambahan
    save_db(db)

    mulai = datetime.now(timezone.utc)
    selesai = mulai + timedelta(days=durasi)

    embed = discord.Embed(
        title=f"🚀 Periode Baru Dimulai!",
        description=f"**{nama}**",
        color=0x57F287,
        timestamp=mulai
    )
    embed.add_field(name="📅 Mulai", value=mulai.strftime("%d %b %Y"), inline=True)
    embed.add_field(name="🏁 Selesai", value=selesai.strftime("%d %b %Y"), inline=True)
    embed.add_field(name="⏳ Durasi", value=f"{durasi} hari", inline=True)

    if target_views:
        embed.add_field(name="🎯 Target Views", value=target_views, inline=True)
    if hadiah_tambahan:
        embed.add_field(name="🎁 Hadiah Tambahan", value=hadiah_tambahan, inline=True)
    if note:
        embed.add_field(name="📋 Catatan", value=note, inline=False)

    embed.add_field(
        name="💰 Sistem Gaji",
        value="100K→50rb | 300K→150rb | 500K→300rb | 1M→700rb",
        inline=False
    )
    embed.set_footer(text=f"Dibuka oleh {interaction.user.display_name} • Gunakan /leaderboard periode")

    # Kirim ke channel rekap jika ada
    rekap_ch_id = db["settings"].get("rekap_channel_id", 0)
    if rekap_ch_id:
        ch = interaction.guild.get_channel(rekap_ch_id)
        if ch:
            await ch.send(embed=embed)

    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# JADWAL LIVE STREAM — MULTI JADWAL (bisa input banyak sekaligus)
# ══════════════════════════════════════════════════════════════════════════════

# Storage sementara per user untuk jadwal yang sedang dikumpulkan
_jadwal_sessions: dict = {}  # discord_id -> list of jadwal dicts


def _parse_jadwal_lines(teks: str) -> list:
    """
    Parse jadwal dari format:
    Senin, 5 Mei | 20.00 WIB | Ngobrol santai, Q&A
    Selasa, 6 Mei | 19.00 WIB | Game bareng
    """
    hasil = []
    for line in teks.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            hasil.append({
                "tanggal": parts[0],
                "jam": parts[1],
                "agenda": parts[2],
                "catatan": parts[3] if len(parts) >= 4 else "",
            })
        elif len(parts) == 2:
            hasil.append({
                "tanggal": parts[0],
                "jam": parts[1],
                "agenda": "-",
                "catatan": "",
            })
    return hasil


class JadwalMingguanModal(discord.ui.Modal, title="Jadwal Live Stream"):
    minggu = discord.ui.TextInput(
        label="Judul Jadwal",
        placeholder="Jadwal Live 1 Mei - 7 Mei 2025",
        max_length=100,
        required=True,
    )
    jadwal_list = discord.ui.TextInput(
        label="Jadwal (Tanggal | Jam | Agenda)",
        placeholder="Senin, 5 Mei | 20.00 WIB | Q&A",
        style=discord.TextStyle.paragraph,
        max_length=1500,
        required=True,
    )
    catatan_umum = discord.ui.TextInput(
        label="Catatan Umum (opsional)",
        placeholder="Misal: Live di TikTok @username",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        jadwals = _parse_jadwal_lines(self.jadwal_list.value)

        if not jadwals:
            return await interaction.response.send_message(
                "❌ Format jadwal salah! Gunakan format:\n`Tanggal | Jam | Agenda`\nContoh: `Senin, 5 Mei | 20.00 WIB | Q&A`",
                ephemeral=True
            )

        WARNA = [0xFF6B6B, 0xFF9F43, 0xFECA57, 0x48DBFB, 0xFF9FF3, 0x54A0FF, 0x5F27CD]
        hari_icons = {
            "senin": "1️⃣", "selasa": "2️⃣", "rabu": "3️⃣",
            "kamis": "4️⃣", "jumat": "5️⃣", "sabtu": "6️⃣", "minggu": "7️⃣",
        }

        # Embed header
        header_embed = discord.Embed(
            title=f"📣 {self.minggu.value}",
            description=f"**{len(jadwals)} jadwal live** minggu ini!\nSimak semua jadwalnya di bawah 👇",
            color=0xFF6B6B,
            timestamp=datetime.now(timezone.utc)
        )
        if self.catatan_umum.value.strip():
            header_embed.add_field(
                name="📌 Catatan Umum",
                value=self.catatan_umum.value.strip(),
                inline=False
            )
        header_embed.set_footer(text=f"Dibuat oleh {interaction.user.display_name}")
        header_embed.set_thumbnail(url=interaction.user.display_avatar.url)

        # Satu embed per jadwal
        jadwal_embeds = []
        for i, j in enumerate(jadwals):
            tanggal_lower = j["tanggal"].lower()
            icon = next((v for k, v in hari_icons.items() if k in tanggal_lower), "📅")
            warna = WARNA[i % len(WARNA)]

            agenda_lines = j["agenda"].split(",")
            agenda_fmt = "\n".join(f"▸ {a.strip()}" for a in agenda_lines if a.strip())

            emb = discord.Embed(
                title=f"{icon} {j['tanggal']}",
                color=warna,
            )
            emb.add_field(name="🕐 Jam", value=j["jam"], inline=True)
            emb.add_field(name="📋 Agenda", value=agenda_fmt or "-", inline=False)
            if j.get("catatan"):
                emb.add_field(name="📌 Catatan", value=j["catatan"], inline=False)

            jadwal_embeds.append(emb)

        # Kirim semua embed sekaligus
        await interaction.response.send_message(
            embeds=[header_embed] + jadwal_embeds[:9]  # Discord max 10 embed per message
        )

        # Kalau lebih dari 9 jadwal, kirim sisanya
        if len(jadwal_embeds) > 9:
            sisa = jadwal_embeds[9:]
            for chunk_start in range(0, len(sisa), 10):
                chunk = sisa[chunk_start:chunk_start+10]
                await interaction.followup.send(embeds=chunk)


@bot.tree.command(name="jadwal_live", description="Buat jadwal live stream (bisa banyak sekaligus, misal seminggu)")
async def jadwal_live_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(JadwalMingguanModal())

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
        _, _ = await remove_clip_role(member, interaction.guild)
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
    _, _ = await remove_clip_role(member, interaction.guild)
    
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

# ═�����════════════════════════════════════════════════════════════════════════════
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
# ══════════════════════════════════════════════════════════════════════��═══════

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
    
    # Otomatis update tiket yang open/processing menjadi completed
    user_tickets = get_user_tickets(db, did)
    tiket_updated = []
    for ticket in user_tickets:
        if ticket["status"] in ("open", "processing"):
            update_ticket_status(db, ticket["id"], "completed")
            tiket_updated.append(ticket["id"])
    
    save_db(db)

    # Response ephemeral untuk admin
    embed = discord.Embed(title="Pembayaran Berhasil!", color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Clipper", value=member.mention, inline=True)
    embed.add_field(name="Gaji Clip", value=fmt_rp(pending), inline=True)
    if bonus > 0:
        embed.add_field(name=f"Bonus ({konsisten_data['label']})", value=fmt_rp(bonus), inline=True)
    embed.add_field(name="Total", value=f"**{fmt_rp(total)}**", inline=False)
    if tiket_updated:
        embed.add_field(name="Tiket Selesai", value=", ".join([f"#{t}" for t in tiket_updated]), inline=False)
    if catatan:
        embed.add_field(name="Catatan", value=catatan, inline=False)
    embed.set_footer(text=f"Approved by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

    # DM to clipper
    try:
        dm = discord.Embed(title="Gajimu Sudah Ditransfer!", description=f"Total: **{fmt_rp(total)}**", color=0x57F287)
        if bonus > 0:
            dm.add_field(name="Bonus Konsisten", value=fmt_rp(bonus))
        if catatan:
            dm.add_field(name="Catatan Admin", value=catatan)
        if tiket_updated:
            dm.add_field(name="Tiket Selesai", value=", ".join([f"#{t}" for t in tiket_updated]), inline=False)
        await member.send(embed=dm)
    except Exception:
        pass
    
    # Log ke log_channel
    await send_log(interaction.guild, db, embed=discord.Embed(
        title="Pembayaran Gaji",
        description=(
            f"**Clipper:** {member.mention}\n"
            f"**Total:** {fmt_rp(total)}\n"
            f"**Approved by:** {interaction.user.mention}"
            + (f"\n**Tiket Selesai:** {', '.join([f'#{t}' for t in tiket_updated])}" if tiket_updated else "")
        ),
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    ))
    
    # Announce ke gaji_channel (public)
    gaji_ch_id = db["settings"].get("gaji_channel_id", 0)
    if gaji_ch_id:
        gaji_ch = interaction.guild.get_channel(gaji_ch_id)
        if gaji_ch:
            announce_embed = discord.Embed(
                title="💰 Gaji Sudah Ditransfer!",
                description=(
                    f"Selamat {member.mention}!\n"
                    f"Gajimu **{fmt_rp(total)}** sudah ditransfer ke rekening kamu.\n\n"
                    f"Total Gaji Sekarang: **{fmt_rp(clipper['total_gaji'])}**"
                ),
                color=0x57F287,
                timestamp=datetime.now(timezone.utc)
            )
            if bonus > 0:
                announce_embed.add_field(name="Bonus Konsisten", value=fmt_rp(bonus), inline=True)
            announce_embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await gaji_ch.send(embed=announce_embed)
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
        
        # Send to log_channel (data sensitif, hanya admin)
        admin_embed = discord.Embed(
            title=f"Tiket Klaim Reward #{ticket['id']}",
            description=f"**User:** {interaction.user.mention}\n**User ID:** {interaction.user.id}\n**Display Name:** {interaction.user.display_name}",
            color=0xFEE75C,
            timestamp=datetime.now(timezone.utc)
        )
        admin_embed.add_field(name="Bank/E-Wallet", value=self.bank_name.value, inline=True)
        admin_embed.add_field(name="Nomor Rekening", value=self.account_number.value, inline=True)
        admin_embed.add_field(name="Atas Nama", value=self.account_holder.value, inline=True)
        admin_embed.add_field(name="WhatsApp", value=self.phone_number.value, inline=True)
        admin_embed.add_field(name="Gaji Pending", value=fmt_rp(clipper["pending_gaji"]), inline=True)
        if self.notes.value:
            admin_embed.add_field(name="Catatan", value=self.notes.value, inline=False)
        admin_embed.set_footer(text="Gunakan /bayar @user untuk memproses pembayaran")
        
        # Kirim ke log_channel
        log_ch_id = db["settings"].get("log_channel_id", 0)
        if log_ch_id:
            log_ch = interaction.guild.get_channel(log_ch_id)
            if log_ch:
                try:
                    await log_ch.send(embed=admin_embed)
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
    
    embed.set_footer(text="Gunakan /bayar @user untuk bayar (tiket otomatis selesai)")
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
    
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # Log ke log_channel
    await send_log(interaction.guild, db, embed=discord.Embed(
        title=f"Tiket #{ticket_id} Diupdate",
        description=f"**User:** {ticket['display_name']}\n**Status:** {old_status.upper()} -> {status.upper()}\n**Oleh:** {interaction.user.mention}",
        color=0x57F287 if status == "completed" else (0xFEE75C if status == "processing" else 0xED4245),
        timestamp=datetime.now(timezone.utc)
    ))
    
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
    clipper_channel="Channel utama clipper (pengumuman, welcome)",
    gaji_channel="Channel notifikasi milestone & gaji",
    rekap_channel="Channel rekap mingguan & periode",
    log_channel="Channel log admin (approval, tiket, pembayaran)",
)
async def setup(
    interaction: discord.Interaction,
    clipper_channel: discord.TextChannel,
    gaji_channel: discord.TextChannel,
    rekap_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    db = load_db()
    db["settings"]["clipper_channel_id"] = clipper_channel.id
    db["settings"]["gaji_channel_id"] = gaji_channel.id
    db["settings"]["rekap_channel_id"] = rekap_channel.id
    db["settings"]["log_channel_id"] = log_channel.id
    save_db(db)

    embed = discord.Embed(title="Setup Berhasil!", color=0x57F287)
    embed.add_field(name="Clipper", value=f"{clipper_channel.mention}\n(Pengumuman, Welcome)", inline=True)
    embed.add_field(name="Gaji", value=f"{gaji_channel.mention}\n(Milestone)", inline=True)
    embed.add_field(name="Rekap", value=f"{rekap_channel.mention}\n(Mingguan, Periode)", inline=True)
    embed.add_field(name="Log Bot", value=f"{log_channel.mention}\n(Approval, Tiket, Pembayaran)", inline=True)
    embed.set_footer(text="Semua data sensitif (approval, tiket, rekening) hanya masuk ke Log Bot")
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
    db = load_db()
    active_tiers = get_active_gaji_tiers(db)
    active_konsisten = get_active_konsisten_tiers(db)
    is_custom = db.get("settings", {}).get("custom_gaji_tiers") is not None
    is_custom_k = db.get("settings", {}).get("custom_konsisten_tiers") is not None

    embed = discord.Embed(
        title="💰 Sistem Gaji Campaign Clipper",
        description="Custom" if is_custom else "Default",
        color=0xFEE75C
    )
    tabel = "\n".join(
        f"{t.get('emoji','⭐')} **{t['label']}** → {fmt_rp(t['gaji'])}"
        for t in active_tiers if t["gaji"] > 0
    )
    embed.add_field(name=f"📊 Gaji per Views {'(Custom ✏️)' if is_custom else ''}", value=tabel, inline=False)
    bonus = "\n".join(
        f"**{t['label']}** ({t['min_clips']}+ clip) → +{fmt_rp(t['hadiah'])}"
        for t in active_konsisten
    )
    embed.add_field(name=f"🏅 Bonus Konsisten {'(Custom ✏️)' if is_custom_k else ''}", value=bonus, inline=False)
    embed.add_field(name="📌 Cara Kerja", value=(
        "1. `!daftar tiktok/youtube @username` (tunggu approval)\n"
        "2. Setelah di-approve, dapat role **Clip**\n"
        "3. `/submit <link>` — bot verifikasi & hitung gaji\n"
        "4. Views auto-update tiap 6 jam\n"
        "5. Admin bayar gaji via `/bayar`"
    ), inline=False)
    if is_admin(interaction.user):
        embed.set_footer(text="Admin: gunakan /set_gaji atau /set_bonus untuk ubah struktur gaji")
    else:
        embed.set_footer(text="Anti-duplikat & verifikasi aktif")
    await interaction.response.send_message(embed=embed)


# ═════════════════════════════════════════��════════════════════════════════════
# CUSTOM GAJI TIERS — Admin bisa ubah struktur gaji tanpa edit file
# ══════════════════════════════════════════════════════════════════════════════

EMOJI_TIERS = ["💎", "🥇", "🥈", "🥉", "🏅", "⭐", "✨"]

def _parse_views_int(s: str) -> int:
    return int(s.strip().replace(".", "").replace(",", "").replace("_", "").replace("K","000").replace("k","000").replace("M","000000").replace("m","000000"))

def _make_gaji_label(views: int) -> str:
    if views >= 1_000_000: return f"{views//1_000_000}M+"
    if views >= 1_000: return f"{views//1_000}K+"
    return f"{views}+"

def _rebuild_gaji_tiers(tiers: list) -> list:
    """Sort, label, emoji ulang setelah modifikasi."""
    tiers = [t for t in tiers if t["min"] > 0]
    tiers = sorted(tiers, key=lambda x: x["min"], reverse=True)
    for i, t in enumerate(tiers):
        t["label"] = _make_gaji_label(t["min"])
        t["emoji"] = EMOJI_TIERS[i] if i < len(EMOJI_TIERS) else "⭐"
    tiers.append({"min": 0, "gaji": 0, "label": f"< {_make_gaji_label(tiers[-1]['min']) if tiers else '0'}", "emoji": "⬜"})
    return tiers


@bot.tree.command(name="set_gaji", description="[ADMIN] Ubah gaji satu tier tertentu (tidak hapus tier lain)")
@app_commands.describe(
    views="Batas minimum views tier ini (contoh: 500000 atau 500.000)",
    gaji="Nominal gaji dalam rupiah (contoh: 300000 atau 300.000)",
)
async def set_gaji_cmd(interaction: discord.Interaction, views: str, gaji: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    try:
        views_val = _parse_views_int(views)
        gaji_val = _parse_views_int(gaji)
    except Exception:
        return await interaction.response.send_message("❌ Format angka salah. Contoh: views=`500000` gaji=`300000`", ephemeral=True)

    db = load_db()
    current = list(db["settings"].get("custom_gaji_tiers") or GAJI_TIERS)
    # Hapus tier 0 sentinel dulu
    current = [t for t in current if t["min"] != 0]
    # Update tier yang sama min-nya, atau tambah baru
    found = False
    for t in current:
        if t["min"] == views_val:
            t["gaji"] = gaji_val
            found = True
            break
    if not found:
        current.append({"min": views_val, "gaji": gaji_val})

    current = _rebuild_gaji_tiers(current)
    db["settings"]["custom_gaji_tiers"] = current
    save_db(db)

    preview = "\n".join(f"{t['emoji']} **{t['label']} views** → {fmt_rp(t['gaji'])}" for t in current if t["gaji"] > 0)
    embed = discord.Embed(title="✅ Tier Gaji Diupdate!", color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.add_field(name=f"{'✏️ Diubah' if found else '➕ Ditambahkan'}", value=f"**{_make_gaji_label(views_val)} views** → {fmt_rp(gaji_val)}", inline=False)
    embed.add_field(name="📊 Semua Tier Gaji Sekarang", value=preview, inline=False)
    embed.set_footer(text=f"Oleh {interaction.user.display_name} • /tambah_gaji untuk tambah tier baru • /reset_gaji untuk reset")
    await interaction.response.send_message(embed=embed)
    await send_log(interaction.guild, db, embed=discord.Embed(
        title="💰 Tier Gaji Diubah",
        description=f"**Oleh:** {interaction.user.mention}\n**Tier:** {_make_gaji_label(views_val)} → {fmt_rp(gaji_val)}",
        color=0xFEE75C, timestamp=datetime.now(timezone.utc)
    ))


@bot.tree.command(name="tambah_gaji", description="[ADMIN] Tambah tier gaji baru tanpa hapus yang lain")
@app_commands.describe(
    views="Batas minimum views (contoh: 750000)",
    gaji="Nominal gaji rupiah (contoh: 400000)",
)
async def tambah_gaji_cmd(interaction: discord.Interaction, views: str, gaji: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    try:
        views_val = _parse_views_int(views)
        gaji_val = _parse_views_int(gaji)
    except Exception:
        return await interaction.response.send_message("❌ Format angka salah.", ephemeral=True)

    db = load_db()
    current = list(db["settings"].get("custom_gaji_tiers") or GAJI_TIERS)
    current = [t for t in current if t["min"] != 0]
    # Cek duplikat
    if any(t["min"] == views_val for t in current):
        return await interaction.response.send_message(
            f"⚠️ Tier **{_make_gaji_label(views_val)}** sudah ada. Gunakan `/set_gaji` untuk mengubah nilainya.",
            ephemeral=True
        )
    current.append({"min": views_val, "gaji": gaji_val})
    current = _rebuild_gaji_tiers(current)
    db["settings"]["custom_gaji_tiers"] = current
    save_db(db)

    preview = "\n".join(f"{t['emoji']} **{t['label']} views** → {fmt_rp(t['gaji'])}" for t in current if t["gaji"] > 0)
    embed = discord.Embed(title="➕ Tier Gaji Baru Ditambahkan!", color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Tier Baru", value=f"**{_make_gaji_label(views_val)} views** → {fmt_rp(gaji_val)}", inline=False)
    embed.add_field(name="📊 Semua Tier Gaji Sekarang", value=preview, inline=False)
    embed.set_footer(text=f"Oleh {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="hapus_tier_gaji", description="[ADMIN] Hapus satu tier gaji tertentu")
@app_commands.describe(views="Batas views tier yang ingin dihapus (contoh: 500000)")
async def hapus_tier_gaji_cmd(interaction: discord.Interaction, views: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    try:
        views_val = _parse_views_int(views)
    except Exception:
        return await interaction.response.send_message("❌ Format angka salah.", ephemeral=True)

    db = load_db()
    current = list(db["settings"].get("custom_gaji_tiers") or GAJI_TIERS)
    current = [t for t in current if t["min"] != 0]
    new = [t for t in current if t["min"] != views_val]
    if len(new) == len(current):
        return await interaction.response.send_message(f"❌ Tier **{_make_gaji_label(views_val)}** tidak ditemukan.", ephemeral=True)
    if not new:
        return await interaction.response.send_message("❌ Tidak bisa hapus semua tier.", ephemeral=True)
    new = _rebuild_gaji_tiers(new)
    db["settings"]["custom_gaji_tiers"] = new
    save_db(db)

    preview = "\n".join(f"{t['emoji']} **{t['label']}** → {fmt_rp(t['gaji'])}" for t in new if t["gaji"] > 0)
    embed = discord.Embed(title="🗑️ Tier Gaji Dihapus", color=0xED4245, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Dihapus", value=f"Tier **{_make_gaji_label(views_val)}**", inline=False)
    embed.add_field(name="📊 Tier Tersisa", value=preview, inline=False)
    await interaction.response.send_message(embed=embed)


def _rebuild_konsisten_tiers(tiers: list) -> list:
    medals = ["🥇", "🥈", "🥉", "🏅", "⭐"]
    labels = ["Super Konsisten", "Konsisten", "Cukup Konsisten", "Aktif", "Pemula"]
    tiers = sorted(tiers, key=lambda x: x["min_clips"], reverse=True)
    for i, t in enumerate(tiers):
        t["label"] = f"{medals[i] if i < len(medals) else '⭐'} {labels[i] if i < len(labels) else f'Level {i+1}'}"
    return tiers


@bot.tree.command(name="set_bonus", description="[ADMIN] Ubah bonus satu tier konsisten (tidak hapus tier lain)")
@app_commands.describe(
    clips="Jumlah minimum clip (contoh: 20)",
    hadiah="Nominal bonus rupiah (contoh: 50000)",
)
async def set_bonus_cmd(interaction: discord.Interaction, clips: int, hadiah: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    try:
        hadiah_val = _parse_views_int(hadiah)
    except Exception:
        return await interaction.response.send_message("❌ Format hadiah salah. Contoh: `50000`", ephemeral=True)

    db = load_db()
    current = list(db["settings"].get("custom_konsisten_tiers") or KONSISTEN_TIERS)
    found = False
    for t in current:
        if t["min_clips"] == clips:
            t["hadiah"] = hadiah_val
            found = True
            break
    if not found:
        current.append({"min_clips": clips, "hadiah": hadiah_val})
    current = _rebuild_konsisten_tiers(current)
    db["settings"]["custom_konsisten_tiers"] = current
    save_db(db)

    preview = "\n".join(f"**{t['label']}** ({t['min_clips']}+ clip) → +{fmt_rp(t['hadiah'])}" for t in current)
    embed = discord.Embed(title="✅ Bonus Konsisten Diupdate!", color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.add_field(name=f"{'✏️ Diubah' if found else '➕ Ditambahkan'}", value=f"**{clips}+ clip** → +{fmt_rp(hadiah_val)}", inline=False)
    embed.add_field(name="🏅 Semua Tier Bonus Sekarang", value=preview, inline=False)
    embed.set_footer(text=f"Oleh {interaction.user.display_name} • /tambah_bonus untuk tambah tier baru")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="tambah_bonus", description="[ADMIN] Tambah tier bonus konsisten baru")
@app_commands.describe(
    clips="Jumlah minimum clip untuk dapat bonus (contoh: 30)",
    hadiah="Nominal bonus rupiah (contoh: 75000)",
)
async def tambah_bonus_cmd(interaction: discord.Interaction, clips: int, hadiah: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    try:
        hadiah_val = _parse_views_int(hadiah)
    except Exception:
        return await interaction.response.send_message("❌ Format hadiah salah.", ephemeral=True)

    db = load_db()
    current = list(db["settings"].get("custom_konsisten_tiers") or KONSISTEN_TIERS)
    if any(t["min_clips"] == clips for t in current):
        return await interaction.response.send_message(
            f"⚠️ Tier **{clips}+ clip** sudah ada. Gunakan `/set_bonus` untuk mengubah nilainya.",
            ephemeral=True
        )
    current.append({"min_clips": clips, "hadiah": hadiah_val})
    current = _rebuild_konsisten_tiers(current)
    db["settings"]["custom_konsisten_tiers"] = current
    save_db(db)

    preview = "\n".join(f"**{t['label']}** ({t['min_clips']}+ clip) → +{fmt_rp(t['hadiah'])}" for t in current)
    embed = discord.Embed(title="➕ Tier Bonus Baru Ditambahkan!", color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Tier Baru", value=f"**{clips}+ clip** → +{fmt_rp(hadiah_val)}", inline=False)
    embed.add_field(name="🏅 Semua Tier Bonus Sekarang", value=preview, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="hapus_tier_bonus", description="[ADMIN] Hapus satu tier bonus konsisten")
@app_commands.describe(clips="Jumlah minimum clip tier yang ingin dihapus")
async def hapus_tier_bonus_cmd(interaction: discord.Interaction, clips: int):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    current = list(db["settings"].get("custom_konsisten_tiers") or KONSISTEN_TIERS)
    new = [t for t in current if t["min_clips"] != clips]
    if len(new) == len(current):
        return await interaction.response.send_message(f"❌ Tier **{clips}+ clip** tidak ditemukan.", ephemeral=True)
    if not new:
        return await interaction.response.send_message("❌ Tidak bisa hapus semua tier.", ephemeral=True)
    new = _rebuild_konsisten_tiers(new)
    db["settings"]["custom_konsisten_tiers"] = new
    save_db(db)
    preview = "\n".join(f"**{t['label']}** ({t['min_clips']}+ clip) → +{fmt_rp(t['hadiah'])}" for t in new)
    embed = discord.Embed(title="🗑️ Tier Bonus Dihapus", color=0xED4245, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Dihapus", value=f"Tier **{clips}+ clip**", inline=False)
    embed.add_field(name="🏅 Tier Tersisa", value=preview or "-", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="reset_gaji", description="[ADMIN] Reset struktur gaji ke default")
@app_commands.describe(tipe="Reset gaji, bonus, atau keduanya")
@app_commands.choices(tipe=[
    app_commands.Choice(name="Gaji Views (reset ke default)", value="gaji"),
    app_commands.Choice(name="Bonus Konsisten (reset ke default)", value="bonus"),
    app_commands.Choice(name="Semua (gaji + bonus)", value="semua"),
])
async def reset_gaji_cmd(interaction: discord.Interaction, tipe: str = "semua"):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)

    db = load_db()
    if tipe in ("gaji", "semua"):
        db["settings"]["custom_gaji_tiers"] = None
    if tipe in ("bonus", "semua"):
        db["settings"]["custom_konsisten_tiers"] = None
    save_db(db)

    embed = discord.Embed(
        title="🔄 Struktur Gaji Direset ke Default",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    if tipe in ("gaji", "semua"):
        tabel = "\n".join(f"{t['emoji']} **{t['label']}** → {fmt_rp(t['gaji'])}" for t in GAJI_TIERS if t["gaji"] > 0)
        embed.add_field(name="📊 Gaji Default", value=tabel, inline=False)
    if tipe in ("bonus", "semua"):
        bonus = "\n".join(f"**{t['label']}** ({t['min_clips']}+ clip) → +{fmt_rp(t['hadiah'])}" for t in KONSISTEN_TIERS)
        embed.add_field(name="🏅 Bonus Default", value=bonus, inline=False)
    embed.set_footer(text=f"Reset oleh {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

# ════════════════════════════════════───═══════════════════════���═════════════════
# FITUR — /init_guides untuk post panduan di setiap channel
# ════════════════════════════════════════��═════════════════════════════════════

@bot.tree.command(name="info_channel", description="[ADMIN] Post info guide di channel")
@app_commands.describe(
    channel="Channel untuk post info",
    guide_type="Tipe guide yang mau dipost"
)
@app_commands.choices(guide_type=[
    app_commands.Choice(name="Cara Daftar", value="daftar"),
    app_commands.Choice(name="Struktur Gaji", value="gaji"),
    app_commands.Choice(name="Cara Submit", value="submit"),
    app_commands.Choice(name="Klaim Reward", value="reward"),
    app_commands.Choice(name="Rekap", value="rekap"),
    app_commands.Choice(name="Pengumuman", value="announce"),
])
async def info_channel(interaction: discord.Interaction, channel: discord.TextChannel, guide_type: str):
    """Post info guide ke channel"""
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Hanya admin.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    guides = {
        "daftar": (
            "Cara Daftar Sebagai Clipper",
            "Ikuti langkah-langkah berikut untuk menjadi clipper resmi:\n\n"
            "Step 1: Daftar Akun\n"
            "Gunakan command: !daftar <platform> <@username>\n"
            "Contoh: !daftar tiktok @myusername\n\n"
            "Step 2: Tunggu Approval\n"
            "Admin akan review dan approve di log-bot.\n"
            "Kamu akan dapat notif jika disetujui.\n\n"
            "Step 3: Terima Role Clip\n"
            "Setelah diapprove, kamu dapat role Clip dan akses penuh.\n\n"
            "Step 4: Submit Clip\n"
            "Gunakan /submit <link> untuk submit clip pertama.\n\n"
            "Tips:\n"
            "- Pastikan username sesuai akun asli\n"
            "- Hanya daftar akun siap produksi\n"
            "- Bisa punya multiple akun"
        ),
        "gaji": (
            "Struktur Gaji Clipper",
            "Gaji dihitung berdasarkan views video yang kamu submit.\n\n"
            "Tier Gaji:\n"
            "Lihat tier dan bonus dengan /info_gaji\n\n"
            "Cara Kerja:\n"
            "1. Submit clip dengan /submit <link>\n"
            "2. Admin verifikasi dan approve\n"
            "3. Views auto-update setiap 6 jam\n"
            "4. Gaji auto-naik sesuai tier\n"
            "5. Dapatkan bonus konsisten jika target terpenuhi\n\n"
            "Bonus Konsisten:\n"
            "- 5 clip/periode = Bonus 5%\n"
            "- 10 clip/periode = Bonus 10%\n"
            "- 20+ clip/periode = Bonus 15%\n\n"
            "Klaim Gaji:\n"
            "Admin akan bayar otomatis via /bayar"
        ),
        "submit": (
            "Cara Submit Clip",
            "Submit clip untuk mendapatkan gaji berdasarkan views.\n\n"
            "Cara Submit:\n"
            "1. Copy link video (TikTok/YouTube)\n"
            "2. Gunakan: /submit <link>\n"
            "Contoh: /submit https://tiktok.com/video/1234567890\n\n"
            "Syarat Clip:\n"
            "- Video milik akun clipper kamu sendiri\n"
            "- Durasi minimal 10 detik\n"
            "- Tidak ada watermark channel lain\n"
            "- Belum pernah di-submit sebelumnya\n\n"
            "Proses Approval:\n"
            "1. Admin verifikasi video dalam 24 jam\n"
            "2. Jika approve = gaji mulai dihitung\n"
            "3. Jika reject = bisa submit ulang\n\n"
            "Tips:\n"
            "- Gunakan /akun untuk lihat semua akun\n"
            "- Pastikan akun di-mention saat daftar\n"
            "- Submit video quality terbaik"
        ),
        "reward": (
            "Cara Klaim Reward / Gaji",
            "Proses klaim reward ada 2 step: Tiket -> Pembayaran\n\n"
            "Step 1: Buat Tiket\n"
            "Gunakan command: /tiket\n"
            "Isi form dengan data:\n"
            "- Bank/E-Wallet (BCA, Mandiri, GCash, dll)\n"
            "- Nomor Rekening / Nomor Tujuan\n"
            "- Nama Pemilik Rekening\n"
            "- Nomor WhatsApp (untuk konfirmasi)\n"
            "- Catatan (opsional)\n\n"
            "Step 2: Tunggu Admin Bayar\n"
            "- Tiket kamu masuk ke log-admin\n"
            "- Admin akan memproses via /bayar\n"
            "- Gaji otomatis ditransfer ke rekening\n"
            "- Kamu dapat notif DM + pengumuman di gaji channel\n\n"
            "Check Status Tiket:\n"
            "Gunakan /tiket_saya untuk lihat status.\n\n"
            "Catatan Penting:\n"
            "- Data rekening HANYA admin yang lihat (rahasia)\n"
            "- Jangan share nomor rekening di public chat"
        ),
        "rekap": (
            "Rekap Clipper",
            "Channel ini untuk laporan berkala.\n\n"
            "Rekap Mingguan:\n"
            "- Top 5 clipper dengan views terbanyak\n"
            "- Clip dengan views tertinggi\n"
            "- Bonus milestone yang tercapai\n\n"
            "Rekap Periode:\n"
            "- Total views & gaji per clipper\n"
            "- Bonus konsisten yang diberikan\n"
            "- Ranking final periode\n\n"
            "Info lengkap bisa dilihat dengan /leaderboard dan /info_gaji"
        ),
        "announce": (
            "Pengumuman Penting",
            "Channel ini untuk:\n"
            "- Selamat datang clipper baru\n"
            "- Update sistem & policy\n"
            "- Challenge & event spesial\n"
            "- Penting fix bugs & maintenance\n\n"
            "Aktifkan notification untuk channel ini!"
        ),
    }
    
    if guide_type not in guides:
        return await interaction.followup.send(
            embed=discord.Embed(title="Error", description="Guide type tidak valid", color=0xED4245),
            ephemeral=True
        )
    
    title, description = guides[guide_type]
    embed = discord.Embed(
        title=title,
        description=description,
        color=0x5865F2 if guide_type != "reward" else 0xFEE75C,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text="Type /help untuk melihat semua command")
    
    try:
        print(f"[v0] Posting ke channel: {channel.name} (ID: {channel.id})")
        print(f"[v0] Bot permissions di channel: {channel.permissions_for(interaction.guild.me)}")
        
        msg = await channel.send(embed=embed)
        print(f"[v0] Berhasil posted message ID: {msg.id}")
        
        result = discord.Embed(
            title="Sukses",
            description=f"Guide '{title}' posted ke {channel.mention}",
            color=0x57F287
        )
    except discord.Forbidden as e:
        print(f"[v0] Forbidden Error: {e}")
        result = discord.Embed(
            title="Error: Forbidden",
            description=f"Bot tidak punya permission send_messages di {channel.mention}.\nPastikan bot punya role yang tepat dan channel permission sudah benar.",
            color=0xED4245
        )
    except Exception as e:
        print(f"[v0] Error: {type(e).__name__}: {e}")
        result = discord.Embed(
            title="Error",
            description=f"{type(e).__name__}: {str(e)}",
            color=0xED4245
        )
    
    await interaction.followup.send(embed=result, ephemeral=True)

# ════════════════════════════════════───═════════════════════════════════════════
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

# ═════════════════════════════════════════════════════��════════════════════════
# REKAP OTOMATIS MINGGUAN — setiap Senin pagi
# ═════════════���════════════════════════════════════════════════════════════════

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

# ════════════════���═════════════════════════════════════════════════════════════
# RUN
# ═════════════════��════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not TOKEN:
        print("[BOT] Set DISCORD_TOKEN di environment variable!")
    else:
        bot.run(TOKEN)
