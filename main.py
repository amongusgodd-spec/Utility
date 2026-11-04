"""
Combined bot: message-proxy webhooks, per-user profiles, VC text-to-speech,
and a customisable verify panel that collects a user's TikTok handle into a
log channel.

Requirements:
    pip install discord.py gTTS PyNaCl aiohttp
    ffmpeg must be installed and on PATH (for TTS playback).
"""

import asyncio
import io
import json
import random
import re
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from gtts import gTTS

import os
TOKEN = os.environ["TOKEN"]

# Fallback storage/verify channels, only used if an admin hasn't run /setstorage
# or /setverify yet in a given server. 0 means "not set" — leave as 0 and just
# use the slash commands after inviting the bot; no need to edit these.
STORAGE_CHANNEL_ID = 0
VERIFY_LOG_CHANNEL_ID = 0

# Optional: role to hand out once someone submits the verify form. Leave as
# None to skip auto-roling and just log the submission.
VERIFIED_ROLE_ID: Optional[int] = None

PROFILE_FILE = "profiles.json"
AVATAR_CACHE_SECONDS = 3600

# Fill in your own presets for /profile random, e.g.:
# PROFILE_PRESETS = [
#     {"name": "Shadow", "avatar_url": "https://example.com/shadow.png"},
#     {"name": "Sunny", "avatar_url": "https://example.com/sunny.png"},
# ]
PROFILE_PRESETS: list[dict] = []

VERIFY_FILE = "verified.json"
VERIFY_ROLE_FILE = "verify_role.json"

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True  # needed to assign the verified role


# ================================================================== profile storage
def load_profiles() -> dict:
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_profiles() -> None:
    with open(PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)


profiles: dict = load_profiles()
avatar_cache: dict[int, tuple[str, float]] = {}


def pkey(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def load_verified() -> dict:
    try:
        with open(VERIFY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_verified() -> None:
    with open(VERIFY_FILE, "w", encoding="utf-8") as f:
        json.dump(verified, f, indent=2)


verified: dict = load_verified()  # "guild:user" -> {"tiktok": ..., "verified_at": ...}


def load_verify_role() -> dict:
    try:
        with open(VERIFY_ROLE_FILE, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_verify_role() -> None:
    with open(VERIFY_ROLE_FILE, "w", encoding="utf-8") as f:
        json.dump(verify_role_config, f, indent=2)


verify_role_config: dict[int, int] = load_verify_role()  # guild_id -> role_id


def load_id_map(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_id_map(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


VERIFY_SUBMIT_CHANNEL_FILE = "verify_submit_channel.json"
VERIFYUSER_LOG_CHANNEL_FILE = "verifyuser_log_channel.json"
STORAGE_CHANNEL_FILE = "storage_channel.json"

verify_submit_channel: dict[int, int] = load_id_map(VERIFY_SUBMIT_CHANNEL_FILE)  # guild_id -> channel_id
verifyuser_log_channel: dict[int, int] = load_id_map(VERIFYUSER_LOG_CHANNEL_FILE)  # guild_id -> channel_id
storage_channel_config: dict[int, int] = load_id_map(STORAGE_CHANNEL_FILE)  # guild_id -> channel_id


# ================================================================== voice (TTS only)
voice_clients: dict[int, discord.VoiceClient] = {}
tts_channels: set[int] = set()
tts_lang: dict[int, str] = {}  # guild_id -> gTTS language code, default "en"

# Maps a relayed (webhook) message id -> the id of the user who originally sent it,
# so a ❌ reaction can be checked against the right person before deleting.
relay_authors: dict[int, int] = {}
CANCEL_EMOJIS = ("❌", "❌️")

# For /edit and /snipe.
relay_content: dict[int, dict] = {}  # message_id -> {"channel_id", "author_id", "content"}
user_last_relay: dict[tuple[int, int], dict] = {}  # (channel_id, user_id) -> {"message_id", "thread_id"}
last_snipe: dict[int, dict] = {}  # channel_id -> {"author_id", "content", "deleted_at"}

# For /afk.
afk_users: dict[str, dict] = {}  # "guild:user" -> {"note", "since"}


# ================================================================== bot
class CombinedBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session: Optional[aiohttp.ClientSession] = None
        self.webhooks: dict[int, discord.Webhook] = {}

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.tree.add_command(profile_group)
        self.tree.add_command(verify_group)
        self.add_view(VerifyView())  # keeps the button working after a restart
        await self.tree.sync()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


client = CombinedBot()


async def get_webhook(channel: discord.TextChannel) -> discord.Webhook:
    if channel.id in client.webhooks:
        return client.webhooks[channel.id]
    for wh in await channel.webhooks():
        if wh.user == client.user:
            client.webhooks[channel.id] = wh
            return wh
    wh = await channel.create_webhook(name="Message Proxy")
    client.webhooks[channel.id] = wh
    return wh


async def get_storage_channel(guild_id: int) -> discord.abc.Messageable:
    channel_id = storage_channel_config.get(guild_id, STORAGE_CHANNEL_ID)
    if not channel_id:
        raise RuntimeError("No storage channel set — an admin needs to run /setstorage first.")
    return client.get_channel(channel_id) or await client.fetch_channel(channel_id)


async def resolve_avatar(profile: dict, member: discord.Member) -> str:
    msg_id = profile.get("avatar_msg")
    if msg_id:
        cached = avatar_cache.get(msg_id)
        if cached and time.time() - cached[1] < AVATAR_CACHE_SECONDS:
            return cached[0]
        try:
            ch = await get_storage_channel(member.guild.id)
            msg = await ch.fetch_message(msg_id)
            url = msg.attachments[0].url
            avatar_cache[msg_id] = (url, time.time())
            return url
        except (discord.HTTPException, IndexError):
            pass
    return profile.get("avatar_url") or member.display_avatar.url


# ================================================================== proxy helpers
async def build_reply_line(message: discord.Message) -> str:
    ref = message.reference
    if not ref or not ref.message_id:
        return ""
    target = ref.resolved if isinstance(ref.resolved, discord.Message) else None
    if target is None:
        try:
            target = await message.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return ""

    snippet = target.content.replace("\n", " ")
    snippet = re.sub(r"<@[!&]?\d+>", "@user", snippet)
    snippet = discord.utils.escape_mentions(snippet)
    if len(snippet) > 60:
        snippet = snippet[:57] + "..."
    if not snippet and (target.attachments or target.stickers):
        snippet = "*attachment*"

    name = discord.utils.escape_markdown(target.author.display_name)
    return f"-# ↪ [{name}]({target.jump_url}) {snippet}\n"


async def build_files(message: discord.Message) -> tuple[list[discord.File], list[str]]:
    files: list[discord.File] = []
    notes: list[str] = []

    for att in message.attachments:
        data = await att.read()
        files.append(
            discord.File(
                io.BytesIO(data),
                filename=att.filename,
                description=att.description,
                spoiler=att.is_spoiler(),
            )
        )

    for sticker in message.stickers:
        if sticker.format is discord.StickerFormatType.lottie:
            notes.append(f"*[sticker: {sticker.name}]*")
            continue
        async with client.session.get(sticker.url) as resp:
            if resp.status != 200:
                notes.append(f"*[sticker: {sticker.name}]*")
                continue
            ext = "gif" if sticker.format is discord.StickerFormatType.gif else "png"
            files.append(discord.File(io.BytesIO(await resp.read()), filename=f"{sticker.name}.{ext}"))

    return files[:10], notes


async def proxy_message(message: discord.Message):
    channel = message.channel
    thread = None
    if isinstance(channel, discord.Thread):
        thread = channel
        channel = channel.parent
    if not isinstance(channel, discord.TextChannel):
        return

    if not (message.content or message.attachments or message.stickers):
        return

    try:
        webhook = await get_webhook(channel)
        files, notes = await build_files(message)
        reply_line = await build_reply_line(message)

        text = reply_line + (message.content or "")
        if notes:
            text += ("\n" if text and not text.endswith("\n") else "") + " ".join(notes)

        embeds = [e for e in message.embeds if e.type == "rich" and not e.url]

        profile = profiles.get(pkey(message.guild.id, message.author.id), {})
        username = profile.get("name") or message.author.display_name
        avatar_url = await resolve_avatar(profile, message.author)

        base = dict(
            username=username[:80],
            avatar_url=avatar_url,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            wait=True,
        )
        if thread:
            base["thread"] = thread

        chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)] or [None]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            sent = await webhook.send(
                content=chunk,
                files=files if last else [],
                embeds=embeds if last else [],
                **base,
            )
            relay_authors[sent.id] = message.author.id
            relay_content[sent.id] = {
                "channel_id": message.channel.id,
                "author_id": message.author.id,
                "content": chunk or "",
            }
            user_last_relay[(message.channel.id, message.author.id)] = {
                "message_id": sent.id,
                "thread_id": thread.id if thread else None,
            }

        await message.delete()

    except discord.Forbidden:
        print("Missing permissions (need Manage Webhooks + Manage Messages).")
    except discord.HTTPException as e:
        print(f"Failed to proxy message: {e}")


# ================================================================== profile commands
profile_group = app_commands.Group(
    name="profile", description="Customise how your messages appear in this server", guild_only=True
)


def valid_name(name: str) -> Optional[str]:
    if not 1 <= len(name) <= 80:
        return "Name must be 1-80 characters."
    if any(bad in name.lower() for bad in ("discord", "clyde")):
        return 'Webhook names can\'t contain "discord" or "clyde".'
    return None


@profile_group.command(name="set", description="Set your custom name and/or avatar")
@app_commands.describe(
    name="Your display name", avatar="Upload an avatar image", avatar_url="Or a permanent image link"
)
async def profile_set(
    interaction: discord.Interaction,
    name: Optional[str] = None,
    avatar: Optional[discord.Attachment] = None,
    avatar_url: Optional[str] = None,
):
    if not (name or avatar or avatar_url):
        await interaction.response.send_message("Give me a name and/or an avatar.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    if name:
        err = valid_name(name)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return
    if avatar and not (avatar.content_type or "").startswith("image/"):
        await interaction.followup.send("That attachment isn't an image.", ephemeral=True)
        return
    if avatar_url and not avatar_url.startswith(("http://", "https://")):
        await interaction.followup.send("Avatar link must start with http:// or https://", ephemeral=True)
        return

    key = pkey(interaction.guild_id, interaction.user.id)
    data = profiles.get(key, {})

    if name:
        data["name"] = name
    if avatar:
        try:
            ch = await get_storage_channel(interaction.guild_id)
            stored = await ch.send(content=key, file=await avatar.to_file())
        except RuntimeError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send(
                "I couldn't store that avatar — check my permissions in the storage channel.",
                ephemeral=True,
            )
            return
        data["avatar_msg"] = stored.id
        data.pop("avatar_url", None)
    elif avatar_url:
        data["avatar_url"] = avatar_url
        data.pop("avatar_msg", None)

    profiles[key] = data
    save_profiles()
    await interaction.followup.send("Profile updated! Use /profile view to preview it.", ephemeral=True)


@profile_group.command(name="view", description="Preview how your messages will look")
async def profile_view(interaction: discord.Interaction):
    key = pkey(interaction.guild_id, interaction.user.id)
    data = profiles.get(key, {})
    name = data.get("name") or interaction.user.display_name
    avatar = await resolve_avatar(data, interaction.user)
    embed = discord.Embed(description="This is how your messages appear.")
    embed.set_author(name=name, icon_url=avatar)
    embed.set_thumbnail(url=avatar)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@profile_group.command(name="random", description="Switch to a random preset name/avatar")
async def profile_random(interaction: discord.Interaction):
    if not PROFILE_PRESETS:
        await interaction.response.send_message("No presets have been set up yet.", ephemeral=True)
        return
    preset = random.choice(PROFILE_PRESETS)
    key = pkey(interaction.guild_id, interaction.user.id)
    data = profiles.get(key, {})
    data["name"] = preset["name"]
    data["avatar_url"] = preset["avatar_url"]
    data.pop("avatar_msg", None)
    profiles[key] = data
    save_profiles()
    await interaction.response.send_message(f"Switched to preset **{preset['name']}**.", ephemeral=True)


@profile_group.command(name="reset", description="Go back to your normal name and avatar")
async def profile_reset(interaction: discord.Interaction):
    key = pkey(interaction.guild_id, interaction.user.id)
    if profiles.pop(key, None) is not None:
        save_profiles()
    await interaction.response.send_message("Profile reset.", ephemeral=True)


# ================================================================== TTS
@client.tree.command(name="tts", description="Toggle text-to-speech for this voice channel's text chat")
async def tts_toggle(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        return

    vc = interaction.user.voice.channel
    gid = interaction.guild_id
    voice_client = voice_clients.get(gid)
    if voice_client is None or not voice_client.is_connected():
        voice_clients[gid] = await vc.connect()
    elif voice_client.channel.id != vc.id:
        await voice_client.move_to(vc)

    if vc.id in tts_channels:
        tts_channels.discard(vc.id)
        await interaction.response.send_message(f"TTS disabled in {vc.mention}'s chat.")
    else:
        tts_channels.add(vc.id)
        await interaction.response.send_message(
            f"TTS enabled. Messages sent in {vc.mention}'s text chat will be read aloud here.\n"
            f"Start a message with `-` to send it without it being spoken."
        )


@client.tree.command(name="leave", description="Disconnect the bot from voice and turn off TTS here")
async def leave(interaction: discord.Interaction):
    gid = interaction.guild_id
    voice_client = voice_clients.get(gid)
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    tts_channels.discard(voice_client.channel.id)
    await voice_client.disconnect()
    voice_clients.pop(gid, None)
    await interaction.response.send_message("Left the voice channel.")


@client.tree.command(name="ttslang", description="Set the TTS voice language for this server")
@app_commands.describe(lang="A gTTS language code, e.g. en, es, fr, ja, de")
async def ttslang(interaction: discord.Interaction, lang: str):
    tts_lang[interaction.guild_id] = lang.lower().strip()
    await interaction.response.send_message(f"TTS language set to `{lang.lower().strip()}`.")


@client.tree.command(name="messagemute", description="Stop the TTS clip currently playing, without leaving")
async def messagemute(interaction: discord.Interaction):
    voice_client = voice_clients.get(interaction.guild_id)
    if not voice_client or not voice_client.is_playing():
        await interaction.response.send_message("Nothing is currently being read.", ephemeral=True)
        return
    voice_client.stop()
    await interaction.response.send_message("Stopped reading that message.")


@client.tree.command(name="edit", description="Edit your most recent relayed message in this channel")
@app_commands.describe(new_text="The new message text")
async def edit_cmd(interaction: discord.Interaction, new_text: str):
    record = user_last_relay.get((interaction.channel_id, interaction.user.id))
    if not record:
        await interaction.response.send_message("You don't have a recent relayed message here.", ephemeral=True)
        return

    channel = interaction.channel
    base_channel = channel.parent if isinstance(channel, discord.Thread) else channel
    try:
        webhook = await get_webhook(base_channel)
        kwargs = {"content": new_text}
        if record.get("thread_id"):
            kwargs["thread"] = discord.Object(id=record["thread_id"])
        await webhook.edit_message(record["message_id"], **kwargs)
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Couldn't edit that message: {e}", ephemeral=True)
        return

    relay_content.setdefault(record["message_id"], {})["content"] = new_text
    await interaction.response.send_message("Edited.", ephemeral=True)


@client.tree.command(name="snipe", description="Show the last ❌-deleted relay message in this channel")
async def snipe(interaction: discord.Interaction):
    record = last_snipe.get(interaction.channel_id)
    if not record:
        await interaction.response.send_message("Nothing to snipe here.", ephemeral=True)
        return

    member = interaction.guild.get_member(record["author_id"])
    embed = discord.Embed(description=record["content"] or "*attachment / empty message*", color=discord.Color.orange())
    embed.set_author(
        name=member.display_name if member else "Unknown user",
        icon_url=member.display_avatar.url if member else discord.Embed.Empty,
    )
    embed.timestamp = record["deleted_at"]
    await interaction.response.send_message(embed=embed)


# ================================================================== verify panel
TIKTOK_HANDLE_RE = re.compile(r"^@?[\w.]{1,24}$")


class TikTokModal(discord.ui.Modal, title="Verify with TikTok"):
    tiktok = discord.ui.TextInput(
        label="Your TikTok username",
        placeholder="@yourname",
        min_length=2,
        max_length=30,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        handle = self.tiktok.value.strip()
        if not TIKTOK_HANDLE_RE.match(handle):
            await interaction.response.send_message(
                "That doesn't look like a valid TikTok username. Try again, e.g. `@yourname`.",
                ephemeral=True,
            )
            return

        log_channel_id = verify_submit_channel.get(interaction.guild_id, VERIFY_LOG_CHANNEL_ID)
        if not log_channel_id:
            await interaction.response.send_message(
                "Verification isn't fully set up yet — ask an admin to run /setverify.", ephemeral=True
            )
            return
        try:
            log_channel = client.get_channel(log_channel_id) or await client.fetch_channel(log_channel_id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "The configured verify channel couldn't be found — ask an admin to run /setverify again.",
                ephemeral=True,
            )
            return

        normalized = handle if handle.startswith("@") else f"@{handle}"
        embed = discord.Embed(title="New verification", color=discord.Color.blurple())
        embed.add_field(name="Member", value=f"{interaction.user.mention} ({interaction.user})", inline=False)
        embed.add_field(name="TikTok", value=f"[{normalized}](https://tiktok.com/{normalized})", inline=False)
        embed.timestamp = discord.utils.utcnow()
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await log_channel.send(embed=embed)

        verified[pkey(interaction.guild_id, interaction.user.id)] = {
            "tiktok": normalized,
            "verified_at": discord.utils.utcnow().isoformat(),
        }
        save_verified()

        if VERIFIED_ROLE_ID:
            role = interaction.guild.get_role(VERIFIED_ROLE_ID)
            if role:
                try:
                    await interaction.user.add_roles(role, reason="Completed TikTok verification")
                except discord.Forbidden:
                    print("Missing permission to assign the verified role (check role position).")

        await interaction.response.send_message("Thanks, you're verified!", ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent: survives restarts

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="verify_panel:verify")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TikTokModal())


@app_commands.command(name="verifypanel", description="Post a customisable verify panel in this channel")
@app_commands.describe(
    title="Panel title",
    description="Panel body text",
    button_label="Text shown on the verify button",
)
@app_commands.checks.has_permissions(administrator=True)
async def verifypanel(
    interaction: discord.Interaction,
    title: str = "Verification",
    description: str = "Click below and enter your TikTok username to verify.",
    button_label: str = "Verify",
):
    embed = discord.Embed(title=title, description=description, color=discord.Color.green())
    view = VerifyView()
    view.children[0].label = button_label  # customise just this panel's button text
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Verify panel posted.", ephemeral=True)


@verifypanel.error
async def verifypanel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/verifypanel error: {error}")


verify_group = app_commands.Group(name="verify", description="Verification lookup tools", guild_only=True)


@verify_group.command(name="lookup", description="Look up a member's submitted TikTok")
@app_commands.describe(user="The member to look up")
@app_commands.checks.has_permissions(manage_messages=True)
async def verify_lookup(interaction: discord.Interaction, user: discord.Member):
    record = verified.get(pkey(interaction.guild_id, user.id))
    if not record:
        await interaction.response.send_message(f"{user.mention} hasn't verified.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"{user.mention} verified with **{record['tiktok']}**.", ephemeral=True
    )


@verify_group.error
async def verify_group_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Messages to do that.", ephemeral=True)
    else:
        print(f"/verify error: {error}")


@app_commands.command(name="setverify", description="Set the channel TikTok verify submissions get logged to")
@app_commands.describe(channel="The channel to send verify submissions to")
@app_commands.checks.has_permissions(administrator=True)
async def setverify(interaction: discord.Interaction, channel: discord.TextChannel):
    verify_submit_channel[interaction.guild_id] = channel.id
    save_id_map(VERIFY_SUBMIT_CHANNEL_FILE, verify_submit_channel)
    await interaction.response.send_message(
        f"Verify submissions will now be sent to {channel.mention}.", ephemeral=True
    )


@setverify.error
async def setverify_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/setverify error: {error}")


@app_commands.command(name="setlog", description="Set the channel /verifyuser runs are logged to")
@app_commands.describe(channel="The channel to log /verifyuser usage to")
@app_commands.checks.has_permissions(administrator=True)
async def setlog(interaction: discord.Interaction, channel: discord.TextChannel):
    verifyuser_log_channel[interaction.guild_id] = channel.id
    save_id_map(VERIFYUSER_LOG_CHANNEL_FILE, verifyuser_log_channel)
    await interaction.response.send_message(
        f"/verifyuser usage will now be logged to {channel.mention}.", ephemeral=True
    )


@setlog.error
async def setlog_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/setlog error: {error}")


@app_commands.command(
    name="setstorage", description="Set the private channel used to store avatars/images"
)
@app_commands.describe(channel="A channel only the bot (and maybe staff) can see")
@app_commands.checks.has_permissions(administrator=True)
async def setstorage(interaction: discord.Interaction, channel: discord.TextChannel):
    storage_channel_config[interaction.guild_id] = channel.id
    save_id_map(STORAGE_CHANNEL_FILE, storage_channel_config)
    await interaction.response.send_message(
        f"Avatars and /imagelink uploads will now be stored in {channel.mention}.", ephemeral=True
    )


@setstorage.error
async def setstorage_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/setstorage error: {error}")


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
        app_commands.Choice(name="Utility", value="utility"),
    ]
)
async def help_cmd(interaction: discord.Interaction, category: Optional[app_commands.Choice[str]] = None):
    sections = {
        "profile": (
            "Profile",
            "This server shows your relayed messages under a name/avatar you can customise.\n"
            "`/profile set` — set a custom display name and/or avatar (upload an image or paste a link)\n"
            "`/profile view` — preview how your messages currently look\n"
            "`/profile random` — switch to a random preset look, if any are set up\n"
            "`/profile reset` — go back to your normal Discord name and avatar",
        ),
        "voice": (
            "Voice / TTS",
            "Join a voice channel, then:\n"
            "`/tts` — turn text-to-speech on/off for that channel's own text chat; once on, messages typed "
            "there get read aloud. Start a message with `-` to send it without it being spoken.\n"
            "`/ttslang` — change the TTS voice language (e.g. en, es, fr)\n"
            "`/messagemute` — stop whatever's currently being read, without leaving\n"
            "`/leave` — disconnect the bot from voice entirely",
        ),
        "relay": (
            "Message relay",
            "Every normal message you send here is automatically reposted under your profile name/avatar.\n"
            "React with ❌ on one of your own relayed messages to delete it (mods can do this on anyone's).\n"
            "`/edit` — change the text of your most recent relayed message in this channel\n"
            "`/snipe` — show the last message that got ❌-deleted in this channel",
        ),
        "verify": (
            "Verification",
            "`/verifypanel` *(admin)* — post a button members click to submit their TikTok handle\n"
            "`/verify lookup` *(mods)* — look up a current member's submitted TikTok\n"
            "`/verifyuser` *(mods)* — look up by mention or raw user ID; also grants the verify role\n"
            "`/verifyrolesetup` *(admin)* — choose which role `/verifyuser` hands out\n"
            "`/setverify` *(admin)* — set the channel verify submissions get posted to\n"
            "`/setlog` *(admin)* — set the channel `/verifyuser` usage gets logged to\n"
            "`/unverify` *(admin)* — remove someone's verified record and role",
        ),
        "utility": (
            "Utility",
            "`/imagelink` — upload an image to get a link for it that won't expire\n"
            "`/avatar` — show a member's full-size avatar\n"
            "`/userinfo` — join date, account age, and roles for a member\n"
            "`/serverinfo` — member count, creation date, and boost level for this server\n"
            "`/ping` — check the bot's latency\n"
            "`/afk` — mark yourself away with a note; it clears automatically next time you talk, and "
            "anyone who @mentions you gets told you're away",
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
    if message.author.bot or message.webhook_id or not message.guild:
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

        text = message.content[:200]
        lang = tts_lang.get(message.guild.id, "en")
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

    # Branch 2: a normal text channel/thread -> proxy through the webhook
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return
    await proxy_message(message)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.member is None or payload.member.bot:
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
client.run(TOKEN)

