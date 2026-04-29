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
    GAJI_TIERS, KONSISTEN_TIERS, MAX_WARNINGS
)
from views_fetcher import fetch_views
from verify_clip import verify_clip

TOKEN = os.environ.get("DISCORD_TOKEN", "")
ADMIN_ROLE_NAME = os.environ.get("ADMIN_ROLE_NAME", "Admin")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── HELPERS ───────────────────────────────────────────────────────────────────

def is_admin(member: discord.Member) -> bool:
    return any(r.name == ADMIN_ROLE_NAME for r in member.roles) or member.guild_permissions.administrator

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

# ── ON READY ──────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅ {bot.user} online!")
    try:
        synced = await bot.tree.sync()
        print(f"🔄 {len(synced)} commands synced")
    except Exception as e:
        print(f"❌ {e}")
    auto_update_views.start()
    weekly_recap.start()

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 1 — !daftar dengan akses channel otomatis
# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="daftar")
async def cmd_daftar(ctx, *, args: str = ""):
    db = load_db()

    parts = args.strip().split()
    if len(parts) < 2:
        embed = discord.Embed(
            title="❌ Format Salah",
            description=(
                "**Cara daftar:**\n"
                "`!daftar tiktok @username`\n"
                "`!daftar youtube NamaChannel`\n\n"
                "Contoh: `!daftar tiktok @budi.clips`"
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
            title="❌ Platform tidak valid",
            description="Gunakan `tiktok` atau `youtube`",
            color=0xED4245
        ))

    did = str(ctx.author.id)

    # Cek blacklist
    if is_blacklisted(db, did):
        bl = db["blacklist"][did]
        return await ctx.reply(embed=discord.Embed(
            title="🚫 Kamu Di-blacklist",
            description=f"**Alasan:** {bl['alasan']}\nHubungi admin untuk banding.",
            color=0xED4245
        ))

    if get_clipper(db, did):
        ex = get_clipper(db, did)
        return await ctx.reply(embed=discord.Embed(
            title="⚠️ Sudah Terdaftar",
            description=f"Platform: **{ex['platform'].title()}**\nUsername: **@{ex['username']}**\n\nHubungi admin untuk ubah data.",
            color=0xFEE75C
        ))

    register_clipper(db, did, username, platform, ctx.author.display_name)

    # Beri akses channel clipper
    access_msg = ""
    ch_id = db["settings"].get("clipper_channel_id", 0)
    if ch_id:
        ch = ctx.guild.get_channel(ch_id)
        if ch:
            try:
                await ch.set_permissions(ctx.author, read_messages=True, send_messages=True)
                access_msg = f"\n✅ Akses diberikan ke {ch.mention}"
            except Exception:
                access_msg = "\n⚠️ Gagal beri akses, hubungi admin"

    embed = discord.Embed(
        title="🎉 Pendaftaran Berhasil!",
        description=(
            f"Selamat datang **{ctx.author.display_name}**!\n\n"
            f"📱 Platform: **{platform.title()}**\n"
            f"🎭 Username: **@{username}**"
            f"{access_msg}\n\n"
            f"Gunakan `/submit <link>` untuk submit clip pertamamu!"
        ),
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=ctx.author.display_avatar.url)
    embed.set_footer(text="Campaign Clipper System")
    await ctx.reply(embed=embed)

    await send_log(ctx.guild, db, embed=discord.Embed(
        title="📝 Clipper Baru Daftar",
        description=f"{ctx.author.mention} (@{username} | {platform.title()})",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    ))

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 2 — /submit dengan verifikasi + anti-duplikat + milestone
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="submit", description="Submit clip campaign TikTok/YouTube kamu")
@app_commands.describe(url="Link video TikTok atau YouTube")
async def submit_clip(interaction: discord.Interaction, url: str):
    db = load_db()
    did = str(interaction.user.id)
    clipper = get_clipper(db, did)

    if not clipper:
        return await interaction.response.send_message(
            "❌ Belum terdaftar! Ketik `!daftar tiktok @username` dulu.", ephemeral=True
        )

    if is_blacklisted(db, did):
        return await interaction.response.send_message("🚫 Kamu di-blacklist. Hubungi admin.", ephemeral=True)

    # ── Anti-duplikat ─────────────────────────────────────────────────────────
    if is_duplicate_url(db, url):
        existing = next((c for c in db["clips"] if c["url"].strip().rstrip("/").lower() == url.strip().rstrip("/").lower()), None)
        warn_count = add_warning(db, did, f"Submit URL duplikat: {url}", "System")
        embed = discord.Embed(
            title="🚫 URL Sudah Pernah Disubmit!",
            description=(
                f"Link ini sudah ada di database (Clip #{existing['id']} oleh **{existing['clipper_name']}**).\n\n"
                f"⚠️ Warning kamu: **{warn_count}/{MAX_WARNINGS}**"
                + ("\n🔴 **Satu warning lagi = auto blacklist!**" if warn_count == MAX_WARNINGS - 1 else "")
                + ("\n🔴 **Kamu telah di-blacklist otomatis!**" if warn_count >= MAX_WARNINGS else "")
            ),
            color=0xED4245
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    await interaction.response.defer(thinking=True)

    # ── Verifikasi kepemilikan video ──────────────────────────────────────────
    verify = await verify_clip(url, clipper["username"], clipper["platform"])
    if not verify["match"]:
        # Beri warning jika verifikasi gagal (bukan unknown)
        if verify["confidence"] != "unknown":
            warn_count = add_warning(db, did, f"Submit video bukan miliknya: {url}", "System")
            warn_msg = (
                f"\n\n⚠️ **Warning {warn_count}/{MAX_WARNINGS}** diberikan."
                + ("\n🔴 **Auto-blacklist setelah 1 warning lagi!**" if warn_count == MAX_WARNINGS - 1 else "")
                + ("\n🔴 **Kamu telah di-blacklist!**" if warn_count >= MAX_WARNINGS else "")
            )
        else:
            warn_msg = "\n\n⚠️ Tidak bisa memverifikasi kepemilikan (video mungkin private)."

        embed = discord.Embed(
            title="🚫 Verifikasi Gagal",
            description=(
                f"Video ini **tidak cocok** dengan akun yang kamu daftarkan.\n\n"
                f"**Terdaftar:** @{clipper['username']}\n"
                f"**Ditemukan:** @{verify.get('found_username', '?')}\n"
                f"**Detail:** {verify['reason']}"
                f"{warn_msg}"
            ),
            color=0xED4245
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Fetch views ───────────────────────────────────────────────────────────
    result = await fetch_views(url)
    if not result["success"]:
        return await interaction.followup.send(
            f"❌ Gagal ambil views: `{result['error']}`\nPastikan video tidak private.", ephemeral=True
        )

    views = result["views"]
    gaji = calc_gaji(views)
    tier = get_tier(views)

    clip_data = {
        "id": len(db["clips"]) + 1,
        "discord_id": did,
        "clipper_name": clipper["display_name"],
        "platform": result["platform"],
        "url": url,
        "title": result.get("title", "Unknown"),
        "thumbnail": result.get("thumbnail", ""),
        "views": views,
        "views_milestones": [],   # milestones yang sudah dikirim notifnya
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
        title="✅ Clip Berhasil Disubmit!",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=result.get("thumbnail", ""))
    embed.add_field(name="🎬 Judul", value=result.get("title", "Unknown")[:80], inline=False)
    embed.add_field(name="👁️ Views", value=fmt_views(views), inline=True)
    embed.add_field(name=f"{tier['emoji']} Tier", value=tier["label"], inline=True)
    embed.add_field(name="💰 Estimasi Gaji", value=fmt_rp(gaji) if gaji > 0 else "Belum mencapai 100K", inline=True)
    embed.add_field(name="✔️ Verifikasi", value=f"@{verify.get('found_username', clipper['username'])} ✅", inline=True)
    embed.add_field(name="📌 Clip ID", value=f"#{clip_data['id']}", inline=True)
    embed.set_footer(text=f"Submit oleh {interaction.user.display_name}")

    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 3 — /profil
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="profil", description="Lihat profil dan statistik clipper")
@app_commands.describe(member="Profil clipper lain (opsional)")
async def profil(interaction: discord.Interaction, member: discord.Member = None):
    db = load_db()
    target = member or interaction.user
    clipper = get_clipper(db, str(target.id))

    if not clipper:
        return await interaction.response.send_message(
            f"❌ {'Kamu' if not member else target.display_name} belum terdaftar.", ephemeral=True
        )

    clips = [c for c in db["clips"] if c["discord_id"] == str(target.id)]
    konsisten = calc_konsisten_hadiah(len(clips))
    warnings = get_warnings(db, str(target.id))
    bl = db["blacklist"].get(str(target.id))

    embed = discord.Embed(
        title=f"👤 Profil — {clipper['display_name']}",
        color=0xED4245 if bl else 0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=target.display_avatar.url)

    if bl:
        embed.add_field(name="🚫 Status", value=f"BLACKLIST\n*{bl['alasan']}*", inline=False)

    platform_icon = "🎵" if clipper["platform"] == "tiktok" else "▶️"
    embed.add_field(name=f"{platform_icon} Platform", value=clipper["platform"].title(), inline=True)
    embed.add_field(name="🎭 Username", value=f"@{clipper['username']}", inline=True)
    embed.add_field(name="📅 Bergabung", value=clipper["joined_at"][:10], inline=True)
    embed.add_field(name="🎬 Total Clip", value=str(clipper["total_clips"]), inline=True)
    embed.add_field(name="👁️ Total Views", value=fmt_views(clipper["total_views"]), inline=True)
    embed.add_field(name="💵 Total Gaji", value=fmt_rp(clipper["total_gaji"]), inline=True)
    embed.add_field(name="⏳ Pending", value=fmt_rp(clipper["pending_gaji"]), inline=True)

    if warnings:
        embed.add_field(name=f"⚠️ Warning ({len(warnings)}/{MAX_WARNINGS})",
                        value="\n".join(f"• {w['alasan'][:40]}" for w in warnings[-3:]), inline=False)

    if konsisten:
        embed.add_field(name="🏅 Status Konsisten",
                        value=f"{konsisten['label']} (+{fmt_rp(konsisten['hadiah'])})", inline=False)

    if clips:
        val = "\n".join(
            f"#{c['id']} {fmt_views(c['views'])} views → {fmt_rp(c['gaji'])} {'✅' if c['gaji_paid'] else '⏳'}"
            for c in clips[-3:][::-1]
        )
        embed.add_field(name="📋 3 Clip Terakhir", value=val, inline=False)

    embed.set_footer(text="Campaign Clipper System")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 4 — /daftar_clipper
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="daftar_clipper", description="Lihat semua clipper terdaftar")
async def daftar_clipper(interaction: discord.Interaction):
    db = load_db()
    clippers = sorted(db["clippers"].values(), key=lambda x: x["total_views"], reverse=True)

    if not clippers:
        return await interaction.response.send_message("📭 Belum ada clipper.", ephemeral=True)

    aktif = [c for c in clippers if c.get("active", True)]
    nonaktif = [c for c in clippers if not c.get("active", True)]

    embed = discord.Embed(
        title=f"📋 Daftar Clipper — {len(aktif)} aktif, {len(nonaktif)} nonaktif",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc)
    )

    for i, c in enumerate(aktif[:15], 1):
        p_icon = "🎵" if c["platform"] == "tiktok" else "▶️"
        warnings = get_warnings(db, c["discord_id"])
        warn_badge = f" ⚠️×{len(warnings)}" if warnings else ""
        embed.add_field(
            name=f"{i}. {c['display_name']}{warn_badge}",
            value=f"{p_icon} @{c['username']} • 🎬{c['total_clips']} • 👁️{fmt_views(c['total_views'])} • 💰{fmt_rp(c['total_gaji'])}",
            inline=False
        )

    if nonaktif:
        embed.add_field(name="🚫 Blacklist", value=", ".join(c["display_name"] for c in nonaktif), inline=False)

    embed.set_footer(text="Gunakan /profil @member untuk detail")
    await interaction.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 5 — /leaderboard
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="leaderboard", description="Leaderboard clipper terbaik")
@app_commands.describe(tipe="Pilih jenis leaderboard")
@app_commands.choices(tipe=[
    app_commands.Choice(name="👁️ Views Terbanyak", value="views"),
    app_commands.Choice(name="🎬 Clip Terbanyak (Konsisten)", value="clips"),
    app_commands.Choice(name="📅 Periode Aktif", value="periode"),
])
async def leaderboard(interaction: discord.Interaction, tipe: str = "views"):
    db = load_db()

    if tipe == "clips":
        top = get_konsisten_leaderboard(db)
        title = "🏆 Leaderboard — Clipper Terkonsisten"
        color = 0xFEE75C
    elif tipe == "periode":
        if not periode_aktif(db):
            return await interaction.response.send_message("❌ Tidak ada periode aktif.", ephemeral=True)
        top = get_periode_leaderboard(db)
        title = f"📅 Leaderboard Periode: {db['periode']['nama']}"
        color = 0xEB459E
    else:
        top = get_leaderboard(db)
        title = "🏆 Leaderboard — Views Terbanyak"
        color = 0xEB459E

    if not top:
        return await interaction.response.send_message("📭 Belum ada data.", ephemeral=True)

    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    medals = ["🥇", "🥈", "🥉"]

    for i, c in enumerate(top):
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        konsisten = calc_konsisten_hadiah(c["total_clips"])

        if tipe == "clips":
            bonus = f" +{fmt_rp(konsisten['hadiah'])}" if konsisten else ""
            stat = f"🎬 {c['total_clips']} clips{bonus}"
        elif tipe == "periode":
            stat = f"👁️ {fmt_views(c.get('periode_views',0))} views • 🎬 {c.get('periode_clips',0)} clips"
        else:
            stat = f"👁️ {fmt_views(c['total_views'])} • 💰 {fmt_rp(c['total_gaji'])}"

        p_icon = "🎵" if c["platform"] == "tiktok" else "▶️"
        embed.add_field(
            name=f"{medal} {c['display_name']}",
            value=f"{p_icon} @{c['username']}\n{stat}",
            inline=False
        )

    if tipe == "clips":
        embed.set_footer(text=f"20+ clips = Rp 50.000 bonus | 10+ clips = Rp 25.000 bonus")
    elif tipe == "periode":
        selesai = db["periode"].get("selesai", "")[:10]
        embed.set_footer(text=f"Periode selesai: {selesai}")
    else:
        embed.set_footer(text="100K=50rb • 300K=150rb • 500K=300rb • 1M=700rb")

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
        return await interaction.response.send_message(f"❌ Clip #{clip_id} tidak ditemukan.", ephemeral=True)

    if clip["discord_id"] != str(interaction.user.id) and not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya bisa update clip milikmu sendiri.", ephemeral=True)

    await interaction.response.defer(thinking=True)
    result = await fetch_views(clip["url"])

    if not result["success"]:
        return await interaction.followup.send(f"❌ Gagal: `{result['error']}`")

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

    trend = "📈" if diff > 0 else ("📉" if diff < 0 else "➡️")
    embed = discord.Embed(title=f"🔄 Views Updated — Clip #{clip_id}", color=0x00C9A7)
    embed.add_field(name="👁️ Views Lama", value=fmt_views(old_views), inline=True)
    embed.add_field(name="👁️ Views Baru", value=fmt_views(new_views), inline=True)
    embed.add_field(name=f"{trend} Pertumbuhan", value=f"{'+' if diff>=0 else ''}{fmt_views(diff)}", inline=True)
    embed.add_field(name="💰 Gaji Lama", value=fmt_rp(clip["gaji"]), inline=True)
    embed.add_field(name="💰 Gaji Baru", value=fmt_rp(new_gaji), inline=True)
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════
# FITUR 7 — Periode Gaji
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="buka_periode", description="[ADMIN] Buka periode gaji baru")
@app_commands.describe(nama="Nama periode (misal: April 2025)", durasi="Durasi hari (default: 30)")
async def buka_periode_cmd(interaction: discord.Interaction, nama: str, durasi: int = 30):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    if periode_aktif(db):
        return await interaction.response.send_message(
            f"⚠️ Periode **{db['periode']['nama']}** masih aktif. Tutup dulu dengan `/tutup_periode`.", ephemeral=True
        )
    buka_periode(db, nama, durasi)
    embed = discord.Embed(
        title="📅 Periode Baru Dibuka!",
        description=f"**{nama}** — {durasi} hari\nLeaderboard periode dapat dilihat di `/leaderboard periode`",
        color=0x57F287,
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="tutup_periode", description="[ADMIN] Tutup periode aktif & umumkan pemenang")
async def tutup_periode_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    if not periode_aktif(db):
        return await interaction.response.send_message("⚠️ Tidak ada periode aktif.", ephemeral=True)

    top = get_periode_leaderboard(db, top_n=5)
    nama = db["periode"]["nama"]
    tutup_periode(db)

    embed = discord.Embed(
        title=f"🏁 Periode **{nama}** Selesai!",
        description="Berikut hasil akhir periode:",
        color=0xFEE75C,
        timestamp=datetime.now(timezone.utc)
    )
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, c in enumerate(top):
        konsisten = calc_konsisten_hadiah(c.get("periode_clips", 0))
        bonus = f" +{fmt_rp(konsisten['hadiah'])}" if konsisten else ""
        embed.add_field(
            name=f"{medals[i]} {c['display_name']}",
            value=f"👁️ {fmt_views(c.get('periode_views',0))} • 🎬 {c.get('periode_clips',0)} clips{bonus}",
            inline=False
        )

    # Kirim ke channel rekap jika ada
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
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    if not get_clipper(db, str(member.id)):
        return await interaction.response.send_message("❌ Bukan clipper terdaftar.", ephemeral=True)

    count = add_warning(db, str(member.id), alasan, str(interaction.user))
    auto_bl = count >= MAX_WARNINGS

    embed = discord.Embed(
        title="⚠️ Warning Diberikan" + (" → 🚫 AUTO BLACKLIST" if auto_bl else ""),
        color=0xED4245 if auto_bl else 0xFEE75C,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="👤 Clipper", value=member.mention, inline=True)
    embed.add_field(name="⚠️ Warning", value=f"{count}/{MAX_WARNINGS}", inline=True)
    embed.add_field(name="📝 Alasan", value=alasan, inline=False)
    if auto_bl:
        embed.add_field(name="🚫 Status", value="Otomatis di-blacklist!", inline=False)
    await interaction.response.send_message(embed=embed)

    try:
        dm = discord.Embed(
            title="⚠️ Kamu Mendapat Warning" + (" — BLACKLIST" if auto_bl else ""),
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
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    blacklist_clipper(db, str(member.id), alasan, str(interaction.user))

    embed = discord.Embed(
        title="🚫 Clipper Di-Blacklist",
        description=f"{member.mention} telah diblacklist.\n**Alasan:** {alasan}",
        color=0xED4245,
        timestamp=datetime.now(timezone.utc)
    )
    await interaction.response.send_message(embed=embed)

    # Cabut akses channel
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
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    unblacklist_clipper(db, str(member.id))

    # Kembalikan akses channel
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
            title="✅ Blacklist Dicabut",
            description=f"{member.mention} bisa aktif kembali sebagai clipper.",
            color=0x57F287
        )
    )

@bot.tree.command(name="hapus_warning", description="[ADMIN] Hapus semua warning clipper")
@app_commands.describe(member="Clipper yang warningnya dihapus")
async def hapus_warning_cmd(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    clear_warnings(db, str(member.id))
    await interaction.response.send_message(f"✅ Semua warning {member.mention} dihapus.", ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — Bayar Gaji
# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="bayar", description="[ADMIN] Approve & tandai gaji sudah dibayar")
@app_commands.describe(member="Clipper yang dibayar", catatan="Catatan (opsional)")
async def bayar(interaction: discord.Interaction, member: discord.Member, catatan: str = ""):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)

    db = load_db()
    did = str(member.id)
    clipper = get_clipper(db, did)
    if not clipper:
        return await interaction.response.send_message("❌ Bukan clipper terdaftar.", ephemeral=True)

    pending = clipper["pending_gaji"]
    konsisten_data = calc_konsisten_hadiah(clipper["total_clips"])
    bonus = konsisten_data["hadiah"] if konsisten_data else 0
    total = pending + bonus

    if total == 0:
        return await interaction.response.send_message("⚠️ Tidak ada gaji pending.", ephemeral=True)

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

    embed = discord.Embed(title="💸 Pembayaran Berhasil!", color=0x57F287, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 Clipper", value=member.mention, inline=True)
    embed.add_field(name="💰 Gaji Clip", value=fmt_rp(pending), inline=True)
    if bonus > 0:
        embed.add_field(name=f"🏅 Bonus ({konsisten_data['label']})", value=fmt_rp(bonus), inline=True)
    embed.add_field(name="💵 Total", value=f"**{fmt_rp(total)}**", inline=False)
    if catatan:
        embed.add_field(name="📝 Catatan", value=catatan, inline=False)
    embed.set_footer(text=f"Approved by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)

    try:
        dm = discord.Embed(title="💸 Gajimu Sudah Ditransfer!", description=f"Total: **{fmt_rp(total)}**", color=0x57F287)
        if bonus > 0:
            dm.add_field(name="🏅 Bonus Konsisten", value=fmt_rp(bonus))
        if catatan:
            dm.add_field(name="📝 Catatan Admin", value=catatan)
        await member.send(embed=dm)
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
)
async def setup(
    interaction: discord.Interaction,
    clipper_channel: discord.TextChannel,
    gaji_channel: discord.TextChannel,
    rekap_channel: discord.TextChannel,
    log_channel: discord.TextChannel,
):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    db = load_db()
    db["settings"]["clipper_channel_id"] = clipper_channel.id
    db["settings"]["gaji_channel_id"] = gaji_channel.id
    db["settings"]["rekap_channel_id"] = rekap_channel.id
    db["settings"]["log_channel_id"] = log_channel.id
    save_db(db)

    embed = discord.Embed(title="⚙️ Setup Berhasil!", color=0x57F287)
    embed.add_field(name="📢 Clipper", value=clipper_channel.mention, inline=True)
    embed.add_field(name="💰 Gaji", value=gaji_channel.mention, inline=True)
    embed.add_field(name="📊 Rekap", value=rekap_channel.mention, inline=True)
    embed.add_field(name="📋 Log", value=log_channel.mention, inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="riwayat_gaji", description="Lihat riwayat pembayaran gaji")
@app_commands.describe(member="Lihat riwayat gaji orang lain (admin only)")
async def riwayat_gaji(interaction: discord.Interaction, member: discord.Member = None):
    db = load_db()
    if member and not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Hanya admin.", ephemeral=True)
    target_id = str(member.id) if member else str(interaction.user.id)
    target_name = (member or interaction.user).display_name
    history = [h for h in db["gaji_history"] if h["discord_id"] == target_id]
    if not history:
        return await interaction.response.send_message(f"📭 Belum ada riwayat untuk {target_name}.", ephemeral=True)

    embed = discord.Embed(
        title=f"📜 Riwayat Gaji — {target_name}",
        description=f"Total diterima: **{fmt_rp(sum(h['amount'] for h in history))}**",
        color=0xFEE75C
    )
    for h in reversed(history[-10:]):
        bonus_txt = f" + Bonus {fmt_rp(h.get('bonus_konsisten',0))}" if h.get("bonus_konsisten",0) > 0 else ""
        embed.add_field(
            name=f"💵 {fmt_rp(h['amount'])} — {h['paid_at'][:10]}",
            value=f"Clip: {fmt_rp(h['gaji_clips'])}{bonus_txt} | Oleh: {h['approved_by'].split('#')[0]}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="info_gaji", description="Lihat tabel sistem gaji & bonus")
async def info_gaji(interaction: discord.Interaction):
    embed = discord.Embed(title="💰 Sistem Gaji Campaign Clipper", color=0xFEE75C)
    tabel = "\n".join(f"{t['emoji']} **{t['label']}** → {fmt_rp(t['gaji'])}" for t in GAJI_TIERS if t["gaji"] > 0)
    embed.add_field(name="📊 Gaji per Views", value=tabel, inline=False)
    bonus = "\n".join(f"**{t['label']}** ({t['min_clips']}+ clips) → +{fmt_rp(t['hadiah'])}" for t in KONSISTEN_TIERS)
    embed.add_field(name="🏅 Bonus Konsisten", value=bonus, inline=False)
    embed.add_field(name="📌 Cara Kerja", value=(
        "1. `!daftar tiktok/youtube @username`\n"
        "2. `/submit <link>` — bot verifikasi & hitung gaji\n"
        "3. Views auto-update tiap 6 jam\n"
        "4. Admin bayar gaji via `/bayar`\n"
        "5. Submit banyak clip = dapat bonus konsisten!"
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
        # Update milestone list
        for i, c in enumerate(db["clips"]):
            if c["id"] == clip["id"]:
                db["clips"][i].setdefault("views_milestones", []).append(m)
                break

        # Ping di channel gaji
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
                title=f"🎉 Milestone {fmt_views(m)} Views Tercapai!",
                description=f"{mention} video-nya tembus **{fmt_views(m)} views**!",
                color=0xFEE75C,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="🎬 Video", value=clip["title"][:60], inline=False)
            embed.add_field(name=f"{tier['emoji']} Tier Baru", value=tier["label"], inline=True)
            embed.add_field(name="💰 Gaji Naik Ke", value=fmt_rp(gaji), inline=True)
            embed.add_field(name="🔗 Link", value=clip["url"], inline=False)
            await ch.send(embed=embed)
        except Exception as e:
            print(f"Milestone notif error: {e}")

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

            # Cek milestone sebelum update
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
            print(f"Auto-update error clip #{clip['id']}: {e}")

    if updated > 0:
        save_db(db)
        print(f"[Auto-Update] {updated} clips updated")

@auto_update_views.before_loop
async def before_update():
    await bot.wait_until_ready()

# ══════════════════════════════════════════════════════════════════════════════
# REKAP OTOMATIS MINGGUAN — setiap Senin pagi
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(hours=24)
async def weekly_recap():
    now = datetime.now(timezone.utc)
    if now.weekday() != 0:  # Hanya Senin
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

    # Hitung statistik minggu ini dari clips 7 hari terakhir
    seminggu_lalu = (now - timedelta(days=7)).isoformat()
    clips_minggu = [c for c in db["clips"] if c.get("submitted_at", "") >= seminggu_lalu]

    total_views_minggu = sum(c["views"] for c in clips_minggu)
    total_clips_minggu = len(clips_minggu)
    total_gaji_pending = sum(c.get("pending_gaji", 0) for c in clippers)

    # Top clipper minggu ini by views
    views_per_clipper = {}
    clips_per_clipper = {}
    for c in clips_minggu:
        did = c["discord_id"]
        views_per_clipper[did] = views_per_clipper.get(did, 0) + c["views"]
        clips_per_clipper[did] = clips_per_clipper.get(did, 0) + 1

    top_minggu = sorted(views_per_clipper.items(), key=lambda x: x[1], reverse=True)[:3]

    embed = discord.Embed(
        title=f"📊 Rekap Mingguan — {now.strftime('%d %b %Y')}",
        description=f"Ringkasan aktivitas clipper 7 hari terakhir",
        color=0x5865F2,
        timestamp=now
    )
    embed.add_field(name="🎬 Total Clip Minggu Ini", value=str(total_clips_minggu), inline=True)
    embed.add_field(name="👁️ Total Views Minggu Ini", value=fmt_views(total_views_minggu), inline=True)
    embed.add_field(name="💰 Total Gaji Pending", value=fmt_rp(total_gaji_pending), inline=True)
    embed.add_field(name="👥 Total Clipper Aktif", value=str(len([c for c in clippers if c.get("active", True)])), inline=True)

    if top_minggu:
        top_txt = ""
        medals = ["🥇", "🥈", "🥉"]
        for i, (did, views) in enumerate(top_minggu):
            clipper = db["clippers"].get(did, {})
            name = clipper.get("display_name", "Unknown")
            clips_count = clips_per_clipper.get(did, 0)
            top_txt += f"{medals[i]} **{name}** — {fmt_views(views)} views ({clips_count} clips)\n"
        embed.add_field(name="🏆 Top Clipper Minggu Ini", value=top_txt, inline=False)

    # Clipper dengan gaji terbesar pending
    top_pending = sorted(clippers, key=lambda x: x.get("pending_gaji", 0), reverse=True)[:3]
    if any(c["pending_gaji"] > 0 for c in top_pending):
        pending_txt = "\n".join(
            f"• **{c['display_name']}** — {fmt_rp(c['pending_gaji'])}"
            for c in top_pending if c["pending_gaji"] > 0
        )
        embed.add_field(name="⏳ Gaji Menunggu Approval", value=pending_txt, inline=False)

    embed.set_footer(text="Rekap otomatis setiap Senin • Campaign Clipper System")
    await ch.send(embed=embed)
    print(f"[Rekap Mingguan] Terkirim ke #{ch.name}")

@weekly_recap.before_loop
async def before_rekap():
    await bot.wait_until_ready()

# ══════════════════════════════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if not TOKEN:
        print("❌ Set DISCORD_TOKEN di environment variable!")
    else:
        bot.run(TOKEN)
