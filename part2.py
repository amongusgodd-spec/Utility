

async def send_join_leave_message(member: discord.Member, config: dict) -> None:
    text = (
        config["message"]
        .replace("{user}", member.mention)
        .replace("{username}", str(member))
        .replace("{server}", member.guild.name)
        .replace("{membercount}", str(member.guild.member_count))
    )
    content = member.mention if (config.get("mention_user") and not config.get("use_embed")) else None

    if config.get("use_embed", True):
        color = parse_hex_color(config["color"]) if config.get("color") else discord.Color.blurple()
        embed = discord.Embed(title=config.get("title") or None, description=text, color=color or discord.Color.blurple())
        if config.get("banner_url"):
            embed.set_image(url=config["banner_url"])
        if config.get("thumbnail_url"):
            embed.set_thumbnail(url=config["thumbnail_url"])
        elif not config.get("banner_url"):
            embed.set_thumbnail(url=member.display_avatar.url)
        send_kwargs = {"embed": embed}
        if config.get("mention_user"):
            send_kwargs["content"] = member.mention
    else:
        send_kwargs = {"content": (f"{member.mention} " if config.get("mention_user") else "") + text}

    try:
        if config.get("dm_instead"):
            await member.send(**send_kwargs)
        else:
            channel = member.guild.get_channel(config["channel_id"])
            if channel:
                await channel.send(**send_kwargs)
    except discord.HTTPException:
        pass


@client.tree.command(name="welcomemsg", description="Set a fully customisable message for when someone joins")
@app_commands.describe(
    channel="Where to post it (ignored if dm_instead is on)",
    message="Use {user}, {username}, {server}, {membercount} as placeholders",
    title="Embed title (ignored if use_embed is off)",
    color="Hex color for the embed, e.g. #57F287",
    banner_url="A wide image shown at the bottom",
    thumbnail_url="A small corner image (defaults to the new member's avatar)",
    use_embed="Post as a styled embed instead of plain text (default: yes)",
    dm_instead="DM the message to the new member instead of posting in a channel",
    mention_user="Ping the member alongside the message",
)
@app_commands.checks.has_permissions(administrator=True)
async def welcomemsg(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str,
    title: Optional[str] = None,
    color: Optional[str] = None,
    banner_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    use_embed: bool = True,
    dm_instead: bool = False,
    mention_user: bool = True,
):
    if not await validate_join_leave_inputs(interaction, color, banner_url, thumbnail_url):
        return
    welcome_config[str(interaction.guild_id)] = build_join_leave_config(
        channel, message, title, color, banner_url, thumbnail_url, use_embed, dm_instead, mention_user
    )
    save_json_file(WELCOME_FILE, welcome_config)
    await interaction.response.send_message("Welcome message set.", ephemeral=True)


@welcomemsg.error
async def welcomemsg_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/welcomemsg error: {error}")


@client.tree.command(name="leavemsg", description="Set a fully customisable message for when someone leaves")
@app_commands.describe(
    channel="Where to post it",
    message="Use {user}, {username}, {server}, {membercount} as placeholders",
    title="Embed title (ignored if use_embed is off)",
    color="Hex color for the embed, e.g. #ED4245",
    banner_url="A wide image shown at the bottom",
    thumbnail_url="A small corner image",
    use_embed="Post as a styled embed instead of plain text (default: yes)",
)
@app_commands.checks.has_permissions(administrator=True)
async def leavemsg(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    message: str,
    title: Optional[str] = None,
    color: Optional[str] = None,
    banner_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    use_embed: bool = True,
):
    if not await validate_join_leave_inputs(interaction, color, banner_url, thumbnail_url):
        return
    # Leave messages can't DM (the member is already gone) or @mention (same reason).
    leavemsg_config[str(interaction.guild_id)] = build_join_leave_config(
        channel, message, title, color, banner_url, thumbnail_url, use_embed, False, False
    )
    save_json_file(LEAVEMSG_FILE, leavemsg_config)
    await interaction.response.send_message("Leave message set.", ephemeral=True)


@leavemsg.error
async def leavemsg_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/leavemsg error: {error}")


@client.tree.command(name="rolemessageoff", description="Turn off the role-trigger message")
@app_commands.checks.has_permissions(administrator=True)
async def rolemessageoff(interaction: discord.Interaction):
    had = role_announce_config.pop(interaction.guild_id, None)
    save_role_announce()
    await interaction.response.send_message(
        "Turned off." if had else "It wasn't set up to begin with.", ephemeral=True
    )


@rolemessageoff.error
async def rolemessageoff_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/rolemessageoff error: {error}")


@app_commands.command(
    name="verifyrolesetup", description="Choose the role /verifyuser hands out automatically"
)
@app_commands.describe(role="Must be below your own top role")
@app_commands.checks.has_permissions(administrator=True)
async def verifyrolesetup(interaction: discord.Interaction, role: discord.Role):
    if role.position >= interaction.user.top_role.position and interaction.guild.owner_id != interaction.user.id:
        await interaction.response.send_message(
            "You can only pick a role below your own highest role.", ephemeral=True
        )
        return
    if role.position >= interaction.guild.me.top_role.position:
        await interaction.response.send_message(
            "I can't assign that role — it's above my own highest role. Move my role above it first.",
            ephemeral=True,
        )
        return
    if role.is_default() or role.managed:
        await interaction.response.send_message("That role can't be assigned manually.", ephemeral=True)
        return

    verify_role_config[interaction.guild_id] = role.id
    save_verify_role()
    await interaction.response.send_message(
        f"{role.mention} will now be given to anyone `/verifyuser` is run on.", ephemeral=True
    )


@verifyrolesetup.error
async def verifyrolesetup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/verifyrolesetup error: {error}")


@app_commands.command(
    name="verifyuser", description="Look up a submitted TikTok by @mention or raw user ID"
)
@app_commands.describe(
    user="A current member to look up",
    user_id="A raw user ID — use this for someone who's left the server",
)
@app_commands.checks.has_permissions(manage_messages=True)
async def verifyuser(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
    user_id: Optional[str] = None,
):
    if not user and not user_id:
        await interaction.response.send_message("Give me a member or a user ID.", ephemeral=True)
        return

    if user:
        uid = user.id
        label = user.mention
    else:
        try:
            uid = int(user_id.strip())
        except ValueError:
            await interaction.response.send_message("That doesn't look like a valid user ID.", ephemeral=True)
            return
        label = f"<@{uid}>"

    record = verified.get(pkey(interaction.guild_id, uid))
    lines = (
        [f"{label} verified with **{record['tiktok']}**."]
        if record
        else [f"{label} hasn't verified."]
    )

    role_id = verify_role_config.get(interaction.guild_id)
    if role_id:
        role = interaction.guild.get_role(role_id)
        if not role:
            lines.append("(Configured verify role no longer exists — run /verifyrolesetup again.)")
        elif not user:
            lines.append("(Role not assigned — that user isn't currently in the server.)")
        elif role in user.roles:
            pass  # already has it
        else:
            try:
                await user.add_roles(role, reason=f"/verifyuser run by {interaction.user}")
                lines.append(f"Gave them {role.mention}.")
            except discord.Forbidden:
                lines.append("(Couldn't assign the role — check my permissions and role position.)")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)

    log_id = verifyuser_log_channel.get(interaction.guild_id)
    if log_id:
        try:
            log_ch = client.get_channel(log_id) or await client.fetch_channel(log_id)
            embed = discord.Embed(title="/verifyuser used", color=discord.Color.blurple())
            embed.add_field(name="Run by", value=interaction.user.mention, inline=False)
            embed.add_field(name="Target", value=label, inline=False)
            embed.add_field(name="Result", value="\n".join(lines), inline=False)
            embed.timestamp = discord.utils.utcnow()
            await log_ch.send(embed=embed)
        except discord.HTTPException:
            pass


@verifyuser.error
async def verifyuser_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Messages to do that.", ephemeral=True)
    else:
        print(f"/verifyuser error: {error}")


@app_commands.command(name="unverify", description="Remove a member's verified status")
@app_commands.describe(user="The member to unverify")
@app_commands.checks.has_permissions(administrator=True)
async def unverify(interaction: discord.Interaction, user: discord.Member):
    had = verified.pop(pkey(interaction.guild_id, user.id), None)
    save_verified()

    if VERIFIED_ROLE_ID:
        role = interaction.guild.get_role(VERIFIED_ROLE_ID)
        if role and role in user.roles:
            try:
                await user.remove_roles(role, reason=f"Unverified by {interaction.user}")
            except discord.Forbidden:
                pass

    msg = f"{user.mention} has been unverified." if had else f"{user.mention} wasn't on record, but I checked for the role too."
    await interaction.response.send_message(msg, ephemeral=True)


@unverify.error
async def unverify_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/unverify error: {error}")


# ================================================================== utility commands
@client.tree.command(name="avatar", description="Show a member's avatar")
@app_commands.describe(user="Whose avatar to show (defaults to you)")
async def avatar_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    member = user or interaction.user
    embed = discord.Embed(title=f"{member.display_name}'s avatar", color=discord.Color.blurple())
    embed.set_image(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="userinfo", description="Show info about a member")
@app_commands.describe(user="Whose info to show (defaults to you)")
async def userinfo(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    member = user or interaction.user
    embed = discord.Embed(title=str(member), color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="Joined server",
        value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown",
    )
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    embed.add_field(name=f"Roles ({len(roles)})", value=", ".join(roles) if roles else "None", inline=False)
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="serverinfo", description="Show info about this server")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Members", value=str(guild.member_count))
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "R"))
    embed.add_field(name="Boost level", value=str(guild.premium_tier))
    embed.add_field(name="Boosts", value=str(guild.premium_subscription_count))
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(client.latency * 1000)}ms")


@client.tree.command(name="purge", description="Delete a batch of recent messages in this channel")
@app_commands.describe(amount="How many messages to delete (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"Deleted {len(deleted)} message(s).", ephemeral=True)


@purge.error
async def purge_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Messages to do that.", ephemeral=True)
    else:
        print(f"/purge error: {error}")


class ConfirmPurgeView(discord.ui.View):
    def __init__(self, author_id: int, channel: discord.TextChannel):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.channel = channel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran the command can confirm this.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Confirm — delete everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Purging — recreating the channel now...", view=self)

        try:
            new_channel = await self.channel.clone(reason=f"/fullpurge by {interaction.user}")
            await new_channel.edit(position=self.channel.position)
            await self.channel.delete(reason=f"/fullpurge by {interaction.user}")
            await new_channel.send(f"✅ Channel purged by {interaction.user.mention}.")
        except discord.Forbidden:
            try:
                await interaction.followup.send(
                    "I don't have permission to manage this channel.", ephemeral=True
                )
            except discord.HTTPException:
                pass
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self)
        self.stop()


@client.tree.command(
    name="fullpurge", description="Delete EVERY message in this channel, including ones older than 14 days"
)
@app_commands.checks.has_permissions(administrator=True)
async def fullpurge(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("This only works in a normal text channel.", ephemeral=True)
        return

    view = ConfirmPurgeView(interaction.user.id, channel)
    await interaction.response.send_message(
        "⚠️ This clones this channel and deletes the original — the only way to wipe messages older than "
        "14 days, since Discord won't bulk-delete those directly. The new channel keeps the same name, "
        "permissions, and position, but message history, pins, and the channel's ID are gone for good.\n\n"
        "If this channel is used for `/setstorage`, `/setverify`, `/setlog`, or TTS, you'll need to run "
        "that setup command again afterward, since the channel ID changes.\n\nThis can't be undone. Confirm?",
        view=view,
        ephemeral=True,
    )


@fullpurge.error
async def fullpurge_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/fullpurge error: {error}")


@client.tree.command(name="kick", description="Kick a member from the server")
@app_commands.describe(user="Who to kick", reason="Why they're being kicked")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    if user.top_role >= interaction.user.top_role and interaction.guild.owner_id != interaction.user.id:
        await interaction.response.send_message("You can't kick someone with an equal or higher role.", ephemeral=True)
        return
    try:
        await user.kick(reason=reason or f"Kicked by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to kick that member.", ephemeral=True)
        return
    log_mod_action(interaction.guild_id, user.id, "kick", reason, interaction.user)
    await interaction.response.send_message(f"Kicked {user.mention}." + (f" Reason: {reason}" if reason else ""))


@kick.error
async def kick_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Kick Members to do that.", ephemeral=True)
    else:
        print(f"/kick error: {error}")


@client.tree.command(name="ban", description="Ban a member from the server")
@app_commands.describe(user="Who to ban", reason="Why they're being banned")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None):
    if user.top_role >= interaction.user.top_role and interaction.guild.owner_id != interaction.user.id:
        await interaction.response.send_message("You can't ban someone with an equal or higher role.", ephemeral=True)
        return
    try:
        await user.ban(reason=reason or f"Banned by {interaction.user}", delete_message_days=0)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to ban that member.", ephemeral=True)
        return
    log_mod_action(interaction.guild_id, user.id, "ban", reason, interaction.user)
    await interaction.response.send_message(f"Banned {user.mention}." + (f" Reason: {reason}" if reason else ""))


@ban.error
async def ban_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Ban Members to do that.", ephemeral=True)
    else:
        print(f"/ban error: {error}")


@client.tree.command(name="timeout", description="Temporarily mute a member")
@app_commands.describe(user="Who to time out", minutes="How long, in minutes (max 40320 = 28 days)", reason="Why")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout_cmd(
    interaction: discord.Interaction,
    user: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: Optional[str] = None,
):
    if user.top_role >= interaction.user.top_role and interaction.guild.owner_id != interaction.user.id:
        await interaction.response.send_message("You can't time out someone with an equal or higher role.", ephemeral=True)
        return
    try:
        await user.timeout(discord.utils.utcnow() + datetime.timedelta(minutes=minutes), reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to time out that member.", ephemeral=True)
        return
    log_mod_action(interaction.guild_id, user.id, "timeout", f"{minutes}m" + (f" — {reason}" if reason else ""), interaction.user)
    await interaction.response.send_message(f"Timed out {user.mention} for {minutes} minute(s).")


@client.tree.command(name="untimeout", description="Remove a member's timeout early")
@app_commands.describe(user="Who to un-timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, user: discord.Member):
    if not user.is_timed_out():
        await interaction.response.send_message(f"{user.mention} isn't timed out.", ephemeral=True)
        return
    try:
        await user.timeout(None, reason=f"Timeout removed by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to do that.", ephemeral=True)
        return
    log_mod_action(interaction.guild_id, user.id, "untimeout", None, interaction.user)
    await interaction.response.send_message(f"Removed {user.mention}'s timeout.")


@untimeout.error
async def untimeout_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Timeout Members to do that.", ephemeral=True)
    else:
        print(f"/untimeout error: {error}")


@timeout_cmd.error
async def timeout_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Timeout Members to do that.", ephemeral=True)
    else:
        print(f"/timeout error: {error}")


@client.tree.command(name="warn", description="Give a member a logged warning")
@app_commands.describe(user="Who to warn", reason="Why they're being warned")
@app_commands.checks.has_permissions(kick_members=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    key = pkey(interaction.guild_id, user.id)
    entry = {"reason": reason, "by": str(interaction.user), "at": discord.utils.utcnow().isoformat()}
    warnings_data.setdefault(key, []).append(entry)
    save_json_file(WARNINGS_FILE, warnings_data)
    count = len(warnings_data[key])
    log_mod_action(interaction.guild_id, user.id, "warn", reason, interaction.user)
    await interaction.response.send_message(f"Warned {user.mention} (warning #{count}). Reason: {reason}")


@warn.error
async def warn_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Kick Members to do that.", ephemeral=True)
    else:
        print(f"/warn error: {error}")


@client.tree.command(name="clearwarn", description="Remove one of a member's warnings by number")
@app_commands.describe(user="Whose warning to remove", index="The warning number, from /warnings")
@app_commands.checks.has_permissions(kick_members=True)
async def clearwarn(interaction: discord.Interaction, user: discord.Member, index: app_commands.Range[int, 1, 999]):
    key = pkey(interaction.guild_id, user.id)
    entries = warnings_data.get(key, [])
    if index > len(entries):
        await interaction.response.send_message(f"{user.mention} only has {len(entries)} warning(s).", ephemeral=True)
        return
    removed = entries.pop(index - 1)
    save_json_file(WARNINGS_FILE, warnings_data)
    await interaction.response.send_message(f"Removed warning #{index} for {user.mention}: {removed['reason']}")


@clearwarn.error
async def clearwarn_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Kick Members to do that.", ephemeral=True)
    else:
        print(f"/clearwarn error: {error}")


@client.tree.command(name="warnings", description="Show a member's warning history")
@app_commands.describe(user="Whose warnings to show")
@app_commands.checks.has_permissions(kick_members=True)
async def warnings_cmd(interaction: discord.Interaction, user: discord.Member):
    entries = warnings_data.get(pkey(interaction.guild_id, user.id), [])
    if not entries:
        await interaction.response.send_message(f"{user.mention} has no warnings.", ephemeral=True)
        return
    lines = [f"{i+1}. {e['reason']} — by {e['by']}" for i, e in enumerate(entries)]
    await interaction.response.send_message(
        f"**Warnings for {user.mention} ({len(entries)}):**\n" + "\n".join(lines), ephemeral=True
    )


@warnings_cmd.error
async def warnings_cmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Kick Members to do that.", ephemeral=True)
    else:
        print(f"/warnings error: {error}")


@client.tree.command(name="modlogs", description="Show a member's full moderation history")
@app_commands.describe(user="Whose history to show")
@app_commands.checks.has_permissions(kick_members=True)
async def modlogs(interaction: discord.Interaction, user: discord.Member):
    entries = mod_actions.get(pkey(interaction.guild_id, user.id), [])
    if not entries:
        await interaction.response.send_message(f"{user.mention} has a clean record.", ephemeral=True)
        return
    icons = {"warn": "⚠️", "kick": "👢", "ban": "🔨", "timeout": "🔇", "untimeout": "🔊"}
    lines = [
        f"{icons.get(e['type'], '•')} **{e['type']}** — {e['reason']} *(by {e['by']})*"
        for e in entries[-20:]
    ]
    await interaction.response.send_message(
        f"**Moderation history for {user.mention} ({len(entries)} total):**\n" + "\n".join(lines),
        ephemeral=True,
    )


@modlogs.error
async def modlogs_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Kick Members to do that.", ephemeral=True)
    else:
        print(f"/modlogs error: {error}")


@client.tree.command(name="lockdown", description="Stop everyone except staff from sending messages in a channel")
@app_commands.describe(channel="Which channel (defaults to this one)")
@app_commands.checks.has_permissions(manage_channels=True)
async def lockdown(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    try:
        await target.set_permissions(
            interaction.guild.default_role, send_messages=False, reason=f"Lockdown by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to edit that channel.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔒 {target.mention} is locked down.")


@lockdown.error
async def lockdown_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Channels to do that.", ephemeral=True)
    else:
        print(f"/lockdown error: {error}")


@client.tree.command(name="unlock", description="Undo a /lockdown on a channel")
@app_commands.describe(channel="Which channel (defaults to this one)")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    try:
        await target.set_permissions(
            interaction.guild.default_role, send_messages=None, reason=f"Unlocked by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to edit that channel.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔓 {target.mention} is unlocked.")


@unlock.error
async def unlock_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Channels to do that.", ephemeral=True)
    else:
        print(f"/unlock error: {error}")


@client.tree.command(name="unban", description="Unban a user by their ID")
@app_commands.describe(user_id="The raw user ID to unban")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        uid = int(user_id.strip())
    except ValueError:
        await interaction.response.send_message("That doesn't look like a valid user ID.", ephemeral=True)
        return
    try:
        await interaction.guild.unban(discord.Object(id=uid), reason=f"Unbanned by {interaction.user}")
    except discord.NotFound:
        await interaction.response.send_message("That user isn't banned.", ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to unban.", ephemeral=True)
        return
    log_mod_action(interaction.guild_id, uid, "unban", None, interaction.user)
    await interaction.response.send_message(f"Unbanned user ID `{uid}`.")


@unban.error
async def unban_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Ban Members to do that.", ephemeral=True)
    else:
        print(f"/unban error: {error}")


@client.tree.command(name="slowmode", description="Set slowmode for this channel")
@app_commands.describe(seconds="Delay between messages, in seconds (0 to turn off, max 21600)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    await interaction.channel.edit(slowmode_delay=seconds)
    if seconds == 0:
        await interaction.response.send_message("Slowmode turned off.")
    else:
        await interaction.response.send_message(f"Slowmode set to {seconds}s.")


@slowmode.error
async def slowmode_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Channels to do that.", ephemeral=True)
    else:
        print(f"/slowmode error: {error}")


@client.tree.command(name="remindme", description="Get pinged with a reminder after a delay")
@app_commands.describe(minutes="How many minutes from now", message="What to remind you about")
async def remindme(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 10080], message: str):
    await interaction.response.send_message(f"Got it — I'll remind you in {minutes} minute(s).", ephemeral=True)

    async def fire():
        await asyncio.sleep(minutes * 60)
        try:
            await interaction.channel.send(f"{interaction.user.mention} reminder: {message}")
        except discord.HTTPException:
            pass

    asyncio.create_task(fire())


@client.tree.command(name="poll", description="Post a quick reaction poll")
@app_commands.describe(
    question="The poll question",
    option1="First option", option2="Second option",
    option3="Third option (optional)", option4="Fourth option (optional)",
)
async def poll(
    interaction: discord.Interaction,
    question: str,
    option1: Optional[str] = None,
    option2: Optional[str] = None,
    option3: Optional[str] = None,
    option4: Optional[str] = None,
):
    options = [o for o in (option1, option2, option3, option4) if o]
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    embed = discord.Embed(title="📊 " + question, color=discord.Color.blurple())
    if options:
        embed.description = "\n".join(f"{emojis[i]} {opt}" for i, opt in enumerate(options))
    embed.set_footer(text=f"Poll by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    if options:
        for i in range(len(options)):
            await msg.add_reaction(emojis[i])
    else:
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")


@app_commands.command(name="setsuggestions", description="Set the channel /suggest posts to")
@app_commands.describe(channel="Where suggestions should be posted")
@app_commands.checks.has_permissions(administrator=True)
async def setsuggestions(interaction: discord.Interaction, channel: discord.TextChannel):
    suggest_channel[str(interaction.guild_id)] = channel.id
    save_json_file(SUGGEST_CHANNEL_FILE, suggest_channel)
    await interaction.response.send_message(f"Suggestions will now go to {channel.mention}.", ephemeral=True)


@setsuggestions.error
async def setsuggestions_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/setsuggestions error: {error}")


@client.tree.command(name="suggest", description="Submit a suggestion for the server")
@app_commands.describe(suggestion="Your suggestion")
async def suggest(interaction: discord.Interaction, suggestion: str):
    channel_id = suggest_channel.get(str(interaction.guild_id))
    if not channel_id:
        await interaction.response.send_message(
            "Suggestions aren't set up yet — ask an admin to run /setsuggestions.", ephemeral=True
        )
        return
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    embed = discord.Embed(description=suggestion, color=discord.Color.gold())
    embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
    embed.timestamp = discord.utils.utcnow()
    msg = await channel.send(embed=embed)
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")
    await interaction.response.send_message("Suggestion submitted!", ephemeral=True)


@client.tree.command(name="8ball", description="Ask the magic 8-ball a question")
@app_commands.describe(question="Your question")
async def eightball(interaction: discord.Interaction, question: str):
    answers = [
        "Yes.", "No.", "Definitely.", "Absolutely not.", "Ask again later.",
        "It is certain.", "Very doubtful.", "Signs point to yes.", "Cannot predict now.",
    ]
    await interaction.response.send_message(f"🎱 **{question}**\n{random.choice(answers)}")


@client.tree.command(name="coinflip", description="Flip a coin")
async def coinflip(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 {random.choice(['Heads', 'Tails'])}!")


@client.tree.command(name="roll", description="Roll a dice")
@app_commands.describe(sides="Number of sides (default 6)")
async def roll(interaction: discord.Interaction, sides: app_commands.Range[int, 2, 1000] = 6):
    await interaction.response.send_message(f"🎲 You rolled a **{random.randint(1, sides)}** (d{sides})")


@client.tree.command(name="afk", description="Mark yourself AFK with an optional note")
@app_commands.describe(note="Why you're away")
async def afk_cmd(interaction: discord.Interaction, note: str = "AFK"):
    afk_users[pkey(interaction.guild_id, interaction.user.id)] = {
        "note": note,
        "since": discord.utils.utcnow(),
    }
    await interaction.response.send_message(f"You're now AFK: {note}")


@client.tree.command(name="help", description="Show how to use this bot's commands")
@app_commands.describe(category="Show just one section instead of everything")
@app_commands.choices(
    category=[
        app_commands.Choice(name="Profile", value="profile"),
        app_commands.Choice(name="Voice / TTS", value="voice"),
        app_commands.Choice(name="Message relay", value="relay"),
        app_commands.Choice(name="Verification", value="verify"),
        app_commands.Choice(name="Moderation", value="moderation"),
        app_commands.Choice(name="Utility", value="utility"),
    ]
)
async def help_cmd(interaction: discord.Interaction, category: Optional[app_commands.Choice[str]] = None):
    sections = {
        "profile": (
            "Profile",
            "This server shows your relayed messages under a name/avatar you can customise.\n"
            "`/profile set` — quickly change your active name/avatar without saving it as a slot\n"
            "`/profile create` — save a new named profile (up to 30 per person) and switch to it\n"
            "`/profile use` — switch your active profile to one you've already saved\n"
            "`/profile list` — see all your saved profiles (⭐ marks the active one)\n"
            "`/profile delete` — remove a saved profile\n"
            "`/profile view` — preview how your messages currently look\n"
            "`/profile random` — switch to a random preset look, if an admin has set any up\n"
            "`/profile reset` — go back to your normal Discord name and avatar",
        ),
        "voice": (
            "Voice / TTS",
            "Join a voice channel, then:\n"
            "`/tts` — turn text-to-speech on/off for that channel's own text chat; once on, messages typed "
            "there get read aloud. Start a message with `-` to send it without it being spoken.\n"
            "`/ttslang` — change the server's default TTS voice language (e.g. en, es, fr)\n"
            "`/mytts` — override that with your own personal TTS language\n"
            "`/ttsmaxlen` *(admin)* — set how many characters get read aloud per message\n"
            "`/messagemute` — stop whatever's currently being read, without leaving\n"
            "`/leave` — disconnect the bot from voice entirely",
        ),
        "relay": (
            "Message relay",
            "Every normal message you send here is automatically reposted under your profile name/avatar.\n"
            "React with ❌ on one of your own relayed messages to delete it (mods can do this on anyone's).\n"
            "`/edit` — change the text of your most recent relayed message in this channel\n"
            "`/snipe` — show the last message that got ❌-deleted in this channel\n"
            "`/pin` — pin your most recent relayed message in this channel\n"
            "`/relaytoggle` *(Manage Channels)* — turn relaying on/off for a channel",
        ),
        "verify": (
            "Verification",
            "`/verifypanel` *(admin)* — post a button members click to submit their TikTok handle\n"
            "`/verify lookup` *(mods)* — look up a current member's submitted TikTok\n"
            "`/verifyuser` *(mods)* — look up by mention or raw user ID; also grants the verify role\n"
            "`/verifyrolesetup` *(admin)* — choose which role `/verifyuser` hands out\n"
            "`/setverify` *(admin)* — set the channel verify submissions get posted to\n"
            "`/setlog` *(admin)* — set the channel `/verifyuser` usage gets logged to\n"
            "`/unverify` *(admin)* — remove someone's verified record and role\n"
            "`/verify list` *(mods)* — show submissions still waiting for Approve/Deny\n"
            "`/verify stats` *(mods)* — approved/denied/pending counts for this server\n"
            "`/verify export` *(mods)* — download all verified TikTok records as a file\n"
            "`/verifypanel` can use an emoji (including server emojis) instead of a button — react to "
            "start, then reply to the bot's DM with your TikTok username\n"
            "There's a 5-minute cooldown on resubmitting the TikTok form.",
        ),
        "moderation": (
            "Moderation",
            "`/purge` *(Manage Messages)* — bulk-delete recent messages in this channel\n"
            "`/fullpurge` *(admin)* — delete every message in this channel, including ones older than "
            "14 days, by recreating the channel (asks for confirmation first)\n"
            "`/kick` *(Kick Members)* — remove a member from the server\n"
            "`/ban` *(Ban Members)* — ban a member from the server\n"
            "`/slowmode` *(Manage Channels)* — set or clear slowmode for this channel\n"
            "`/rolemessage` *(admin)* — post a custom message in a set channel whenever someone is given "
            "a specific role (use `{user}`, `{role}`, `{server}` as placeholders)\n"
            "`/rolemessageoff` *(admin)* — turn that off\n"
            "`/timeout` *(Timeout Members)* — mute a member temporarily\n"
            "`/untimeout` *(Timeout Members)* — lift a timeout early\n"
            "`/warn`, `/warnings`, `/clearwarn` *(Kick Members)* — log, view, and remove warnings\n"
            "`/modlogs` *(Kick Members)* — a member's full warn/kick/ban/timeout history in one view\n"
            "`/unban` *(Ban Members)* — unban by raw user ID\n"
            "`/lockdown` / `/unlock` *(Manage Channels)* — stop/allow everyone sending in a channel\n"
            "`/blockword add/remove/list` *(Manage Messages)* — auto-delete messages with blocked words\n"
            "`/blockinvites` *(Manage Messages)* — toggle auto-deleting Discord invite links",
        ),
        "utility": (
            "Utility",
            "`/imagelink` — upload an image to get a link for it that won't expire\n"
            "`/avatar` — show a member's full-size avatar\n"
            "`/userinfo` — join date, account age, and roles for a member\n"
            "`/serverinfo` — member count, creation date, and boost level for this server\n"
            "`/ping` — check the bot's latency\n"
            "`/remindme` — get pinged with a reminder after a delay\n"
            "`/afk` — mark yourself away with a note; it clears automatically next time you talk, and "
            "anyone who @mentions you gets told you're away\n"
            "`/poll` — post a quick reaction poll\n"
            "`/suggest` — submit a suggestion (needs `/setsuggestions` set up first)\n"
            "`/setsuggestions` *(admin)* — set where suggestions get posted\n"
            "`/starboard` *(admin)* — repost ⭐-reacted messages to a channel once they hit a threshold\n"
            "`/welcomemsg` / `/leavemsg` *(admin)* — customisable join/leave messages: embed or plain text, "
            "title, color, banner/thumbnail images, mention toggle, and (join only) DM-instead-of-channel\n"
            "`/8ball`, `/coinflip`, `/roll` — just for fun",
        ),
    }

    embed = discord.Embed(color=discord.Color.blurple())
    if category:
        title, body = sections[category.value]
        embed.title = title
        embed.description = body
    else:
        embed.title = "Commands"
        embed.description = "Use `/help category:` to see just one section."
        for title, body in sections.values():
            embed.add_field(name=title, value=body, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.tree.command(name="commands", description="Show a quick list of every command")
async def commands_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="All commands", color=discord.Color.blurple())
    embed.add_field(
        name="Profile",
        value=(
            "`/profile set` `/profile create` `/profile use` `/profile list` `/profile delete` "
            "`/profile view` `/profile random` `/profile reset`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Voice / TTS",
        value="`/tts` `/ttslang` `/mytts` `/ttsmaxlen` `/messagemute` `/leave`",
        inline=False,
    )
    embed.add_field(
        name="Message relay",
        value="`/edit` `/snipe` `/pin` `/relaytoggle`  —  plus auto-relay and a ❌ reaction to delete",
        inline=False,
    )
    embed.add_field(
        name="Verification",
        value=(
            "`/verifypanel` `/verify lookup` `/verify list` `/verify stats` `/verify export` `/verifyuser` "
            "`/verifyrolesetup` `/setverify` `/setlog` `/unverify`  —  plus Approve/Deny buttons"
        ),
        inline=False,
    )
    embed.add_field(
        name="Moderation",
        value=(
            "`/purge` `/fullpurge` `/kick` `/ban` `/timeout` `/untimeout` `/warn` `/warnings` "
            "`/clearwarn` `/modlogs` `/unban` `/lockdown` `/unlock` "
            "`/slowmode` `/rolemessage` `/rolemessageoff` `/blockword` `/blockinvites`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Utility",
        value=(
            "`/setstorage` `/imagelink` `/avatar` `/userinfo` `/serverinfo` `/ping` `/remindme` `/afk` "
            "`/poll` `/suggest` `/setsuggestions` `/starboard` `/welcomemsg` `/leavemsg` "
            "`/8ball` `/coinflip` `/roll`"
        ),
        inline=False,
    )
    embed.set_footer(text="Use /help for an explanation of what each one does.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ================================================================== image link
@app_commands.command(name="imagelink", description="Get a direct link for an uploaded image")
@app_commands.describe(image="The image to get a link for")
async def imagelink(interaction: discord.Interaction, image: discord.Attachment):
    if not (image.content_type or "").startswith("image/"):
        await interaction.response.send_message("That attachment isn't an image.", ephemeral=True)
        return

    # Discord attachment URLs on the CDN expire; re-hosting it in a channel the
    # bot controls gives back a link that stays valid indefinitely.
    try:
        ch = await get_storage_channel(interaction.guild_id)
        data = await image.read()
        stored = await ch.send(file=discord.File(io.BytesIO(data), filename=image.filename))
        link = stored.attachments[0].url
    except RuntimeError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Couldn't process that image: {e}", ephemeral=True)
        return

    await interaction.response.send_message(f"Here's your image's valid link:\n{link}")


# ================================================================== events
@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id:
        return

    if message.guild is None:
        # A DM reply to the emoji-panel verify prompt
        guild_id = pending_dm_verify.get(message.author.id)
        if guild_id:
            result = await submit_tiktok_verification(guild_id, message.author, message.content)
            pending_dm_verify.pop(message.author.id, None)
            try:
                await message.channel.send(result)
            except discord.HTTPException:
                pass
        return

    # AFK: clear the sender's own status if they talk again
    own_key = pkey(message.guild.id, message.author.id)
    if own_key in afk_users:
        del afk_users[own_key]
        try:
            await message.channel.send(
                f"Welcome back {message.author.mention}, I've removed your AFK status.", delete_after=5
            )
        except discord.HTTPException:
            pass

    # AFK: let the sender know if anyone they @mentioned is away
    if message.mentions:
        notes = []
        for mentioned in message.mentions:
            m_key = pkey(message.guild.id, mentioned.id)
            if m_key in afk_users:
                notes.append(f"{mentioned.display_name} is AFK: {afk_users[m_key]['note']}")
        if notes:
            try:
                await message.channel.send("\n".join(notes), delete_after=8)
            except discord.HTTPException:
                pass

    # Branch 1: a voice channel's built-in text chat with TTS switched on
    if isinstance(message.channel, discord.VoiceChannel):
        if message.channel.id not in tts_channels or not message.content:
            return
        if not message.author.voice or message.author.voice.channel.id != message.channel.id:
            return
        if message.content.lstrip().startswith("-"):
            return  # "-" prefix: send the message but don't speak it

        voice_client = voice_clients.get(message.guild.id)
        if not voice_client or not voice_client.is_connected():
            return

        maxlen = tts_maxlen.get(str(message.guild.id), 200)
        text = message.content[:maxlen]
        lang = mytts_lang.get(pkey(message.guild.id, message.author.id)) or tts_lang.get(message.guild.id, "en")
        loop = asyncio.get_running_loop()

        def make_mp3():
            path = f"/tmp/tts_{message.id}.mp3"
            gTTS(text=text, lang=lang).save(path)
            return path

        try:
            path = await loop.run_in_executor(None, make_mp3)
        except Exception as e:
            print(f"TTS generation failed: {e}")
            return

        while voice_client.is_playing():
            await asyncio.sleep(0.5)
        voice_client.play(discord.FFmpegPCMAudio(path))
        return

    # Branch 2: a normal text channel/thread -> automod, then proxy through the webhook
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return

    guild_key = str(message.guild.id)
    content_lower = message.content.lower()

    if blockinvites.get(guild_key) and INVITE_RE.search(message.content):
        try:
            await message.delete()
            notice = await message.channel.send(f"{message.author.mention} invite links aren't allowed here.", delete_after=6)
        except discord.HTTPException:
            pass
        return

    for word in blockwords.get(guild_key, []):
        if word in content_lower:
            try:
                await message.delete()
                await message.channel.send(f"{message.author.mention} that message contained a blocked word.", delete_after=6)
            except discord.HTTPException:
                pass
            return

    blocked_channels = relay_blocklist.get(guild_key, [])
    base_channel_id = message.channel.parent.id if isinstance(message.channel, discord.Thread) else message.channel.id
    if base_channel_id in blocked_channels:
        return  # relay disabled in this channel; leave the message as-is

    await proxy_message(message)


@client.event
async def on_member_join(member: discord.Member):
    config = welcome_config.get(str(member.guild.id))
    if config:
        await send_join_leave_message(member, config)


@client.event
async def on_member_remove(member: discord.Member):
    config = leavemsg_config.get(str(member.guild.id))
    if config:
        await send_join_leave_message(member, config)


@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    config = role_announce_config.get(after.guild.id)
    if not config:
        return

    added_role_ids = {r.id for r in after.roles} - {r.id for r in before.roles}
    if config["role_id"] not in added_role_ids:
        return

    channel = after.guild.get_channel(config["channel_id"])
    if not channel:
        return

    role = after.guild.get_role(config["role_id"])
    text = (
        config["message"]
        .replace("{user}", after.mention)
        .replace("{role}", role.mention if role else "that role")
        .replace("{server}", after.guild.name)
    )
    try:
        await channel.send(text)
    except discord.HTTPException:
        pass


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.member is None or payload.member.bot:
        return

    panel = verify_emoji_panels.get(str(payload.message_id))
    if panel and panel["guild_id"] == payload.guild_id and str(payload.emoji) == panel["emoji"]:
        try:
            channel = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
            msg = await channel.fetch_message(payload.message_id)
            await msg.remove_reaction(payload.emoji, payload.member)
        except discord.HTTPException:
            pass
        try:
            await payload.member.send(
                f"Reply here with your TikTok username (e.g. `@yourname`) to verify for "
                f"**{payload.member.guild.name}**."
            )
            pending_dm_verify[payload.member.id] = payload.guild_id
        except discord.Forbidden:
            pass  # their DMs are closed; nothing more we can do
        return

    if str(payload.emoji) == "⭐":
        config = starboard_config.get(str(payload.guild_id))
        if config and payload.message_id not in starboard_posted:
            try:
                channel = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
                msg = await channel.fetch_message(payload.message_id)
                star_reaction = discord.utils.get(msg.reactions, emoji="⭐")
                count = star_reaction.count if star_reaction else 0
                if count >= config["threshold"]:
                    board = client.get_channel(config["channel_id"]) or await client.fetch_channel(config["channel_id"])
                    embed = discord.Embed(description=msg.content or "*attachment*", color=discord.Color.gold())
                    embed.set_author(name=str(msg.author), icon_url=msg.author.display_avatar.url)
                    embed.add_field(name="Source", value=f"[Jump to message]({msg.jump_url})", inline=False)
                    if msg.attachments:
                        embed.set_image(url=msg.attachments[0].url)
                    posted = await board.send(content=f"⭐ {count}", embed=embed)
                    starboard_posted[payload.message_id] = posted.id
            except discord.HTTPException:
                pass
        return

    if str(payload.emoji) not in CANCEL_EMOJIS:
        return

    author_id = relay_authors.get(payload.message_id)
    if author_id is None:
        return  # not a relayed message we're tracking (or already handled)

    # Only the original sender, or someone who can manage messages, may delete it.
    if payload.user_id != author_id and not payload.member.guild_permissions.manage_messages:
        return

    content_record = relay_content.pop(payload.message_id, None)
    if content_record:
        last_snipe[payload.channel_id] = {
            "author_id": content_record["author_id"],
            "content": content_record["content"],
            "deleted_at": discord.utils.utcnow(),
        }

    try:
        channel = client.get_channel(payload.channel_id) or await client.fetch_channel(payload.channel_id)
        msg = await channel.fetch_message(payload.message_id)
        await msg.delete()
    except discord.HTTPException:
        pass
    relay_authors.pop(payload.message_id, None)


client.tree.add_command(verifypanel)
client.tree.add_command(imagelink)
client.tree.add_command(unverify)
client.tree.add_command(verifyuser)
client.tree.add_command(verifyrolesetup)
client.tree.add_command(setverify)
client.tree.add_command(setlog)
client.tree.add_command(setstorage)
client.tree.add_command(setsuggestions)
