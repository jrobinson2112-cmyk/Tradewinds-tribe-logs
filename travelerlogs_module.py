# =====================
# COMMAND REGISTRATION
# =====================
def setup_travelerlog_commands(
    tree: app_commands.CommandTree,
    guild_id: int,
):
    """
    Registers /writelog for the specified guild.
    Not admin-locked: everyone can use it.
    """
    _load_lock_config()
    guild_obj = discord.Object(id=int(guild_id))

    @tree.command(name="writelog", guild=guild_obj, description="Write a Traveler Log (auto-stamps Day/Year).")
    async def writelog_cmd(interaction: discord.Interaction):
        # Optional: You can restrict usage to locked channels only by uncommenting:
        # if not _is_locked_channel(interaction.channel):
        #     await interaction.response.send_message("❌ Use this in the Traveler Logs channels.", ephemeral=True)
        #     return

        modal = TravelerLogModal(interaction)
        await interaction.response.send_modal(modal)

    print("[travelerlogs_module] ✅ /writelog registered")

# =====================
# OPTIONAL: CHANNEL LOCK ENFORCEMENT
# =====================
async def enforce_travelerlog_lock(message: discord.Message):
    """
    Deletes non-bot messages in locked channels/category.
    Call from main.py on_message.
    """
    # Ignore bots (including ourselves)
    if message.author.bot:
        return

    ch = message.channel
    if not _is_locked_channel(ch):
        return

    # If someone tries to type normally, delete it
    try:
        await message.delete()
    except Exception:
        # can't delete (missing perms) — silently ignore
        return

    if LOCK_DELETE_NOTICE:
        try:
            warn = await ch.send(f"📝 Traveler Logs are locked. Please use **/writelog** to post.", delete_after=6)
        except Exception:
            pass

# =====================
# Optional helper if you want to reload config without restart
# =====================
def reload_config():
    _load_lock_config()