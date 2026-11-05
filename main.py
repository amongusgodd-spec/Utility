"""
Combined bot: message-proxy webhooks, per-user profiles, VC text-to-speech,
and a customisable verify panel that collects a user's TikTok handle into a
log channel.

Requirements:
    pip install discord.py gTTS PyNaCl aiohttp
    ffmpeg must be installed and on PATH (for TTS playback).
"""

import asyncio
import datetime
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
ROLE_ANNOUNCE_FILE = "role_announce.json"


def load_role_announce() -> dict:
    try:
        with open(ROLE_ANNOUNCE_FILE, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_role_announce() -> None:
    with open(ROLE_ANNOUNCE_FILE, "w", encoding="utf-8") as f:
        json.dump(role_announce_config, f, indent=2)


# guild_id -> {"role_id", "channel_id", "message"}
role_announce_config: dict[int, dict] = load_role_announce()

WARNINGS_FILE = "warnings.json"
BLOCKWORDS_FILE = "blockwords.json"
BLOCKINVITES_FILE = "blockinvites.json"
RELAY_BLOCKLIST_FILE = "relay_blocklist.json"
MYTTS_FILE = "mytts.json"
TTS_MAXLEN_FILE = "tts_maxlen.json"
SUGGEST_CHANNEL_FILE = "suggest_channel.json"
STARBOARD_FILE = "starboard.json"
WELCOME_FILE = "welcome.json"
LEAVEMSG_FILE = "leavemsg.json"


def load_json_file(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json_file(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


warnings_data: dict = load_json_file(WARNINGS_FILE, {})  # "guild:user" -> [ {reason, by, at}, ... ]
blockwords: dict = load_json_file(BLOCKWORDS_FILE, {})  # "guild_id" -> [word, ...]
blockinvites: dict = load_json_file(BLOCKINVITES_FILE, {})  # "guild_id" -> True/False
relay_blocklist: dict = load_json_file(RELAY_BLOCKLIST_FILE, {})  # "guild_id" -> [channel_id, ...]
mytts_lang: dict = load_json_file(MYTTS_FILE, {})  # "guild:user" -> lang code
tts_maxlen: dict = load_json_file(TTS_MAXLEN_FILE, {})  # "guild_id" -> int
suggest_channel: dict = load_json_file(SUGGEST_CHANNEL_FILE, {})  # "guild_id" -> channel_id
starboard_config: dict = load_json_file(STARBOARD_FILE, {})  # "guild_id" -> {"channel_id","threshold"}
starboard_posted: dict[int, int] = {}  # original message_id -> starboard message_id (in-memory only)
welcome_config: dict = load_json_file(WELCOME_FILE, {})  # "guild_id" -> {"channel_id","message"}
leavemsg_config: dict = load_json_file(LEAVEMSG_FILE, {})  # "guild_id" -> {"channel_id","message"}
INVITE_RE = re.compile(r"(discord\.gg/|discord(?:app)?\.com/invite/)[\w-]+", re.IGNORECASE)

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
        self.tree.add_command(blockword_group)
        self.add_view(VerifyView())  # keeps the Verify button working after a restart
        self.add_view(VerifyReviewView())  # keeps Approve/Deny working after a restart
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

PROFILE_SLOTS_FILE = "profile_slots.json"
PROFILE_ACTIVE_FILE = "profile_active.json"
MAX_PROFILE_SLOTS = 30

profile_slots: dict = load_json_file(PROFILE_SLOTS_FILE, {})  # "guild:user" -> {slot_name: {name, avatar_url/avatar_msg}}
profile_active_slot: dict = load_json_file(PROFILE_ACTIVE_FILE, {})  # "guild:user" -> slot_name currently in use


def valid_name(name: str) -> Optional[str]:
    if not 1 <= len(name) <= 80:
        return "Name must be 1-80 characters."
    if any(bad in name.lower() for bad in ("discord", "clyde")):
        return 'Webhook names can\'t contain "discord" or "clyde".'
    return None


def valid_slot(slot: str) -> Optional[str]:
    if not 1 <= len(slot) <= 32:
        return "Profile slot names must be 1-32 characters."
    return None


async def build_avatar_fields(
    interaction: discord.Interaction, key: str, data: dict, avatar: Optional[discord.Attachment], avatar_url: Optional[str]
) -> Optional[str]:
    """Validates + stores an avatar into `data` in place. Returns an error message, or None on success."""
    if avatar and not (avatar.content_type or "").startswith("image/"):
        return "That attachment isn't an image."
    if avatar_url and not avatar_url.startswith(("http://", "https://")):
        return "Avatar link must start with http:// or https://"

    if avatar:
        try:
            ch = await get_storage_channel(interaction.guild_id)
            stored = await ch.send(content=key, file=await avatar.to_file())
        except RuntimeError as e:
            return str(e)
        except discord.HTTPException:
            return "I couldn't store that avatar — check my permissions in the storage channel."
        data["avatar_msg"] = stored.id
        data.pop("avatar_url", None)
    elif avatar_url:
        data["avatar_url"] = avatar_url
        data.pop("avatar_msg", None)
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


@profile_group.command(name="create", description="Save a new named relay profile (up to 30 per person)")
@app_commands.describe(
    slot="A short name for this profile, e.g. 'formal' or 'gremlin mode'",
    name="Display name for this profile",
    avatar="Upload an avatar image",
    avatar_url="Or a permanent image link",
)
async def profile_create(
    interaction: discord.Interaction,
    slot: str,
    name: Optional[str] = None,
    avatar: Optional[discord.Attachment] = None,
    avatar_url: Optional[str] = None,
):
    if not (name or avatar or avatar_url):
        await interaction.response.send_message("Give this profile a name and/or an avatar.", ephemeral=True)
        return
    slot_err = valid_slot(slot)
    if slot_err:
        await interaction.response.send_message(slot_err, ephemeral=True)
        return
    if name:
        name_err = valid_name(name)
        if name_err:
            await interaction.response.send_message(name_err, ephemeral=True)
            return

    await interaction.response.defer(ephemeral=True)
    key = pkey(interaction.guild_id, interaction.user.id)
    slots = profile_slots.setdefault(key, {})

    if slot not in slots and len(slots) >= MAX_PROFILE_SLOTS:
        await interaction.followup.send(
            f"You've hit the {MAX_PROFILE_SLOTS}-profile limit — delete one with /profile delete first.",
            ephemeral=True,
        )
        return

    data = dict(slots.get(slot, {}))
    if name:
        data["name"] = name
    err = await build_avatar_fields(interaction, key, data, avatar, avatar_url)
    if err:
        await interaction.followup.send(err, ephemeral=True)
        return

    slots[slot] = data
    save_json_file(PROFILE_SLOTS_FILE, profile_slots)

    # Creating a profile makes it the active one right away.
    profiles[key] = dict(data)
    save_profiles()
    profile_active_slot[key] = slot
    save_json_file(PROFILE_ACTIVE_FILE, profile_active_slot)

    await interaction.followup.send(
        f"Saved profile **{slot}** ({len(slots)}/{MAX_PROFILE_SLOTS}) and switched to it.", ephemeral=True
    )


@profile_group.command(name="use", description="Switch your active relay profile to a saved one")
@app_commands.describe(slot="Which saved profile to switch to")
async def profile_use(interaction: discord.Interaction, slot: str):
    key = pkey(interaction.guild_id, interaction.user.id)
    slots = profile_slots.get(key, {})
    if slot not in slots:
        await interaction.response.send_message(
            f"No saved profile called **{slot}**. Use /profile list to see what you've got.", ephemeral=True
        )
        return
    profiles[key] = dict(slots[slot])
    save_profiles()
    profile_active_slot[key] = slot
    save_json_file(PROFILE_ACTIVE_FILE, profile_active_slot)
    await interaction.response.send_message(f"Switched to **{slot}**.", ephemeral=True)


@profile_use.autocomplete("slot")
async def profile_use_autocomplete(interaction: discord.Interaction, current: str):
    key = pkey(interaction.guild_id, interaction.user.id)
    slots = profile_slots.get(key, {})
    return [
        app_commands.Choice(name=s, value=s) for s in slots if current.lower() in s.lower()
    ][:25]


@profile_group.command(name="list", description="List your saved relay profiles")
async def profile_list(interaction: discord.Interaction):
    key = pkey(interaction.guild_id, interaction.user.id)
    slots = profile_slots.get(key, {})
    if not slots:
        await interaction.response.send_message("You don't have any saved profiles yet — try /profile create.", ephemeral=True)
        return
    active = profile_active_slot.get(key)
    lines = [f"{'⭐ ' if s == active else ''}**{s}** — {d.get('name', '(no name set)')}" for s, d in slots.items()]
    await interaction.response.send_message(
        f"**Your profiles ({len(slots)}/{MAX_PROFILE_SLOTS}):**\n" + "\n".join(lines), ephemeral=True
    )


@profile_group.command(name="delete", description="Delete a saved relay profile")
@app_commands.describe(slot="Which saved profile to delete")
async def profile_delete(interaction: discord.Interaction, slot: str):
    key = pkey(interaction.guild_id, interaction.user.id)
    slots = profile_slots.get(key, {})
    if slot not in slots:
        await interaction.response.send_message(f"No saved profile called **{slot}**.", ephemeral=True)
        return
    del slots[slot]
    save_json_file(PROFILE_SLOTS_FILE, profile_slots)

    if profile_active_slot.get(key) == slot:
        profile_active_slot.pop(key, None)
        save_json_file(PROFILE_ACTIVE_FILE, profile_active_slot)
        profiles.pop(key, None)
        save_profiles()
        await interaction.response.send_message(
            f"Deleted **{slot}** — that was your active profile, so you're back to your normal name/avatar.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(f"Deleted **{slot}**.", ephemeral=True)


@profile_delete.autocomplete("slot")
async def profile_delete_autocomplete(interaction: discord.Interaction, current: str):
    key = pkey(interaction.guild_id, interaction.user.id)
    slots = profile_slots.get(key, {})
    return [
        app_commands.Choice(name=s, value=s) for s in slots if current.lower() in s.lower()
    ][:25]


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


@client.tree.command(name="mytts", description="Set your personal TTS voice language (overrides the server default)")
@app_commands.describe(lang="A gTTS language code, e.g. en, es, fr, ja, de")
async def mytts(interaction: discord.Interaction, lang: str):
    mytts_lang[pkey(interaction.guild_id, interaction.user.id)] = lang.lower().strip()
    save_json_file(MYTTS_FILE, mytts_lang)
    await interaction.response.send_message(f"Your TTS voice is now `{lang.lower().strip()}`.", ephemeral=True)


@client.tree.command(name="ttsmaxlen", description="Set the max characters read aloud per TTS message")
@app_commands.describe(length="Max characters (10-500)")
@app_commands.checks.has_permissions(administrator=True)
async def ttsmaxlen(interaction: discord.Interaction, length: app_commands.Range[int, 10, 500]):
    tts_maxlen[str(interaction.guild_id)] = length
    save_json_file(TTS_MAXLEN_FILE, tts_maxlen)
    await interaction.response.send_message(f"TTS messages are now capped at {length} characters.", ephemeral=True)


@ttsmaxlen.error
async def ttsmaxlen_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/ttsmaxlen error: {error}")


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


@client.tree.command(name="relaytoggle", description="Turn message relaying on/off for a channel")
@app_commands.describe(channel="Which channel (defaults to this one)")
@app_commands.checks.has_permissions(manage_channels=True)
async def relaytoggle(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target = channel or interaction.channel
    key = str(interaction.guild_id)
    blocked = relay_blocklist.setdefault(key, [])
    if target.id in blocked:
        blocked.remove(target.id)
        msg = f"Relay turned back ON in {target.mention}."
    else:
        blocked.append(target.id)
        msg = f"Relay turned OFF in {target.mention}."
    save_json_file(RELAY_BLOCKLIST_FILE, relay_blocklist)
    await interaction.response.send_message(msg, ephemeral=True)


@relaytoggle.error
async def relaytoggle_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Channels to do that.", ephemeral=True)
    else:
        print(f"/relaytoggle error: {error}")


@client.tree.command(name="pin", description="Pin your most recent relayed message in this channel")
async def pin_cmd(interaction: discord.Interaction):
    record = user_last_relay.get((interaction.channel_id, interaction.user.id))
    if not record:
        await interaction.response.send_message("You don't have a recent relayed message here.", ephemeral=True)
        return
    try:
        msg = await interaction.channel.fetch_message(record["message_id"])
        await msg.pin(reason=f"Pinned via /pin by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("I don't have permission to pin messages here.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Couldn't pin that: {e}", ephemeral=True)
        return
    await interaction.response.send_message("Pinned.", ephemeral=True)


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
VERIFY_COOLDOWN_SECONDS = 300
verify_last_submit: dict[str, float] = {}  # "guild:user" -> unix timestamp
pending_dm_verify: dict[int, int] = {}  # user_id -> guild_id, while awaiting their DM reply

VERIFY_EMOJI_PANELS_FILE = "verify_emoji_panels.json"
verify_emoji_panels: dict = load_json_file(VERIFY_EMOJI_PANELS_FILE, {})  # "message_id" -> {"guild_id","emoji"}

VERIFY_STATS_FILE = "verify_stats.json"
verify_stats: dict = load_json_file(VERIFY_STATS_FILE, {})  # "guild_id" -> {"approved": n, "denied": n}


async def submit_tiktok_verification(guild_id: int, user: discord.abc.User, raw_handle: str) -> str:
    """Shared by the button's modal and the emoji-reaction DM flow. Returns the
    message to show/send back to the user."""
    cd_key = pkey(guild_id, user.id)
    last = verify_last_submit.get(cd_key, 0)
    remaining = VERIFY_COOLDOWN_SECONDS - (time.time() - last)
    if remaining > 0:
        return f"You can resubmit in {int(remaining // 60) + 1} minute(s)."

    handle = raw_handle.strip()
    if not TIKTOK_HANDLE_RE.match(handle):
        return "That doesn't look like a valid TikTok username. Try again, e.g. `@yourname`."

    review_channel_id = verify_submit_channel.get(guild_id, VERIFY_LOG_CHANNEL_ID)
    if not review_channel_id:
        return "Verification isn't fully set up yet — ask an admin to run /setverify."
    try:
        review_channel = client.get_channel(review_channel_id) or await client.fetch_channel(review_channel_id)
    except discord.HTTPException:
        return "The configured verify channel couldn't be found — ask an admin to run /setverify again."

    normalized = handle if handle.startswith("@") else f"@{handle}"
    embed = discord.Embed(title="Verification pending review", color=discord.Color.gold())
    embed.add_field(name="Member", value=f"{user.mention} ({user})", inline=False)
    embed.add_field(name="TikTok", value=f"[{normalized}](https://tiktok.com/{normalized})", inline=False)
    embed.timestamp = discord.utils.utcnow()
    embed.set_thumbnail(url=user.display_avatar.url)
    # Machine-readable record, parsed back out by the Approve/Deny buttons —
    # keeps this stateless so the buttons survive a bot restart.
    embed.set_footer(text=f"verify|{guild_id}|{user.id}|{normalized}")

    await review_channel.send(embed=embed, view=VerifyReviewView())
    verify_last_submit[cd_key] = time.time()
    return "Thanks! Your submission has been sent to the mods for review."


class TikTokModal(discord.ui.Modal, title="Verify with TikTok"):
    tiktok = discord.ui.TextInput(
        label="Your TikTok username",
        placeholder="@yourname",
        min_length=2,
        max_length=30,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        result = await submit_tiktok_verification(interaction.guild_id, interaction.user, self.tiktok.value)
        await interaction.response.send_message(result, ephemeral=True)


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent: survives restarts

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="verify_panel:verify")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TikTokModal())


class VerifyReviewView(discord.ui.View):
    """Attached to each pending-review message. Stateless: reads everything it
    needs from the message's own embed footer, so it keeps working after a
    bot restart as long as it's re-registered via client.add_view()."""

    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    def _parse(message: discord.Message) -> Optional[tuple[int, int, str]]:
        if not message.embeds or not message.embeds[0].footer or not message.embeds[0].footer.text:
            return None
        parts = message.embeds[0].footer.text.split("|")
        if len(parts) != 4 or parts[0] != "verify":
            return None
        try:
            return int(parts[1]), int(parts[2]), parts[3]
        except ValueError:
            return None

    async def _finish(
        self, interaction: discord.Interaction, approved: bool, color: discord.Color, status: str
    ):
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = color
        embed.add_field(name="Status", value=status, inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="verify_review:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("You need Manage Messages to do that.", ephemeral=True)
            return

        parsed = self._parse(interaction.message)
        if not parsed:
            await interaction.response.send_message("Couldn't read this submission.", ephemeral=True)
            return
        guild_id, user_id, tiktok = parsed

        verified[pkey(guild_id, user_id)] = {
            "tiktok": tiktok,
            "verified_at": discord.utils.utcnow().isoformat(),
        }
        save_verified()

        role_id = verify_role_config.get(guild_id)
        if role_id:
            member = interaction.guild.get_member(user_id)
            role = interaction.guild.get_role(role_id)
            if member and role:
                try:
                    await member.add_roles(role, reason=f"TikTok verification approved by {interaction.user}")
                except discord.Forbidden:
                    pass

        stats = verify_stats.setdefault(str(guild_id), {"approved": 0, "denied": 0})
        stats["approved"] += 1
        save_json_file(VERIFY_STATS_FILE, verify_stats)

        await self._finish(interaction, True, discord.Color.green(), f"✅ Approved by {interaction.user.mention}")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="verify_review:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("You need Manage Messages to do that.", ephemeral=True)
            return

        parsed = self._parse(interaction.message)
        if not parsed:
            await interaction.response.send_message("Couldn't read this submission.", ephemeral=True)
            return
        guild_id, _, _ = parsed

        stats = verify_stats.setdefault(str(guild_id), {"approved": 0, "denied": 0})
        stats["denied"] += 1
        save_json_file(VERIFY_STATS_FILE, verify_stats)

        await self._finish(interaction, False, discord.Color.red(), f"❌ Denied by {interaction.user.mention}")


def parse_hex_color(value: str) -> Optional[discord.Color]:
    s = value.strip().lstrip("#")
    if len(s) != 6:
        return None
    try:
        return discord.Color(int(s, 16))
    except ValueError:
        return None


BUTTON_STYLES = {
    "green": discord.ButtonStyle.success,
    "red": discord.ButtonStyle.danger,
    "grey": discord.ButtonStyle.secondary,
    "blurple": discord.ButtonStyle.primary,
}


@app_commands.command(name="verifypanel", description="Post a customisable verify panel in this channel")
@app_commands.describe(
    title="Panel title",
    description="Panel body text",
    button_label="Text shown on the verify button (ignored if trigger_emoji is set)",
    button_style="Color of the verify button (ignored if trigger_emoji is set)",
    color="Hex color for the panel, e.g. #57F287",
    banner_url="A wide image shown at the bottom of the panel",
    thumbnail_url="A small image shown in the top-right corner of the panel",
    trigger_emoji="Use an emoji reaction instead of a button — a normal emoji or a server emoji like :name:",
)
@app_commands.choices(
    button_style=[
        app_commands.Choice(name="Green", value="green"),
        app_commands.Choice(name="Red", value="red"),
        app_commands.Choice(name="Grey", value="grey"),
        app_commands.Choice(name="Blurple", value="blurple"),
    ]
)
@app_commands.checks.has_permissions(administrator=True)
async def verifypanel(
    interaction: discord.Interaction,
    title: str = "Verification",
    description: str = "Click below and enter your TikTok username to verify.",
    button_label: str = "Verify",
    button_style: Optional[app_commands.Choice[str]] = None,
    color: Optional[str] = None,
    banner_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    trigger_emoji: Optional[str] = None,
):
    embed_color = discord.Color.green()
    if color:
        parsed_color = parse_hex_color(color)
        if not parsed_color:
            await interaction.response.send_message(
                "That doesn't look like a valid hex color. Try something like `#57F287`.", ephemeral=True
            )
            return
        embed_color = parsed_color

    for label, url in (("banner_url", banner_url), ("thumbnail_url", thumbnail_url)):
        if url and not url.startswith(("http://", "https://")):
            await interaction.response.send_message(f"`{label}` must be a link starting with http(s)://", ephemeral=True)
            return

    parsed_emoji = None
    if trigger_emoji:
        try:
            parsed_emoji = discord.PartialEmoji.from_str(trigger_emoji.strip())
        except Exception:
            await interaction.response.send_message("That doesn't look like a valid emoji.", ephemeral=True)
            return

    embed = discord.Embed(title=title, description=description, color=embed_color)
    if banner_url:
        embed.set_image(url=banner_url)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    if parsed_emoji:
        embed.set_footer(text=f"React with {parsed_emoji} below to start — I'll DM you to collect your TikTok username.")
        msg = await interaction.channel.send(embed=embed)
        try:
            await msg.add_reaction(parsed_emoji)
        except discord.HTTPException:
            await interaction.response.send_message(
                "Posted the panel, but I couldn't add that reaction myself — "
                "make sure it's an emoji from this server, or add it manually.",
                ephemeral=True,
            )
            return
        verify_emoji_panels[str(msg.id)] = {"guild_id": interaction.guild_id, "emoji": str(parsed_emoji)}
        save_json_file(VERIFY_EMOJI_PANELS_FILE, verify_emoji_panels)
    else:
        view = VerifyView()
        view.children[0].label = button_label  # customise just this panel's button text
        view.children[0].style = BUTTON_STYLES[button_style.value] if button_style else discord.ButtonStyle.success
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


@verify_group.command(name="list", description="Show submissions still waiting for approval")
@app_commands.checks.has_permissions(manage_messages=True)
async def verify_list(interaction: discord.Interaction):
    channel_id = verify_submit_channel.get(interaction.guild_id, VERIFY_LOG_CHANNEL_ID)
    if not channel_id:
        await interaction.response.send_message("Verification isn't set up yet.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    except discord.HTTPException:
        await interaction.followup.send("Couldn't find the verify channel.", ephemeral=True)
        return

    pending = []
    async for msg in channel.history(limit=100):
        if not msg.embeds or not msg.embeds[0].footer or not msg.embeds[0].footer.text:
            continue
        if not msg.embeds[0].footer.text.startswith("verify|"):
            continue
        has_status = any(f.name == "Status" for f in msg.embeds[0].fields)
        if not has_status:
            pending.append(msg)

    if not pending:
        await interaction.followup.send("No pending submissions.", ephemeral=True)
        return
    lines = [f"[{m.embeds[0].fields[0].value}]({m.jump_url})" for m in pending[:15]]
    await interaction.followup.send(f"**Pending ({len(pending)}):**\n" + "\n".join(lines), ephemeral=True)


@verify_group.command(name="stats", description="Show verification counts for this server")
@app_commands.checks.has_permissions(manage_messages=True)
async def verify_stats_cmd(interaction: discord.Interaction):
    channel_id = verify_submit_channel.get(interaction.guild_id, VERIFY_LOG_CHANNEL_ID)
    await interaction.response.defer(ephemeral=True)

    stats = verify_stats.get(str(interaction.guild_id), {"approved": 0, "denied": 0})
    total_records = sum(1 for k in verified if k.startswith(f"{interaction.guild_id}:"))

    pending_count = 0
    if channel_id:
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            async for msg in channel.history(limit=100):
                if not msg.embeds or not msg.embeds[0].footer or not msg.embeds[0].footer.text:
                    continue
                if not msg.embeds[0].footer.text.startswith("verify|"):
                    continue
                if not any(f.name == "Status" for f in msg.embeds[0].fields):
                    pending_count += 1
        except discord.HTTPException:
            pass

    embed = discord.Embed(title="Verification stats", color=discord.Color.blurple())
    embed.add_field(name="Approved (all time)", value=str(stats.get("approved", 0)))
    embed.add_field(name="Denied (all time)", value=str(stats.get("denied", 0)))
    embed.add_field(name="Currently pending", value=str(pending_count))
    embed.add_field(name="Verified records on file", value=str(total_records), inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@verify_group.command(name="export", description="Export all verified TikTok records for this server as a file")
@app_commands.checks.has_permissions(manage_messages=True)
async def verify_export(interaction: discord.Interaction):
    prefix = f"{interaction.guild_id}:"
    rows = [
        f"{user_id}\t{data['tiktok']}\t{data['verified_at']}"
        for key, data in verified.items()
        if key.startswith(prefix)
        for user_id in [key.split(":", 1)[1]]
    ]
    if not rows:
        await interaction.response.send_message("No verified records to export.", ephemeral=True)
        return

    content = "user_id\ttiktok\tverified_at\n" + "\n".join(rows)
    buffer = io.BytesIO(content.encode("utf-8"))
    await interaction.response.send_message(
        f"{len(rows)} record(s).",
        file=discord.File(buffer, filename=f"verified_{interaction.guild_id}.tsv"),
        ephemeral=True,
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


@client.tree.command(
    name="rolemessage", description="Post a custom message when someone is given a specific role"
)
@app_commands.describe(
    role="The role that triggers the message",
    channel="Which channel to post the message in",
    message="Use {user} for their mention, {role} for the role's mention, {server} for the server name",
)
@app_commands.checks.has_permissions(administrator=True)
async def rolemessage(
    interaction: discord.Interaction, role: discord.Role, channel: discord.TextChannel, message: str
):
    role_announce_config[interaction.guild_id] = {
        "role_id": role.id,
        "channel_id": channel.id,
        "message": message,
    }
    save_role_announce()
    await interaction.response.send_message(
        f"Got it — when someone gets {role.mention}, I'll post that message in {channel.mention}.",
        ephemeral=True,
    )


@rolemessage.error
async def rolemessage_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/rolemessage error: {error}")


blockword_group = app_commands.Group(name="blockword", description="Manage auto-deleted words", guild_only=True)


@blockword_group.command(name="add", description="Add a word/phrase that gets auto-deleted")
@app_commands.checks.has_permissions(manage_messages=True)
async def blockword_add(interaction: discord.Interaction, word: str):
    key = str(interaction.guild_id)
    words = blockwords.setdefault(key, [])
    w = word.lower().strip()
    if w in words:
        await interaction.response.send_message("That's already blocked.", ephemeral=True)
        return
    words.append(w)
    save_json_file(BLOCKWORDS_FILE, blockwords)
    await interaction.response.send_message(f"Added `{w}` to the blocked words.", ephemeral=True)


@blockword_group.command(name="remove", description="Remove a blocked word/phrase")
async def blockword_remove(interaction: discord.Interaction, word: str):
    key = str(interaction.guild_id)
    words = blockwords.get(key, [])
    w = word.lower().strip()
    if w not in words:
        await interaction.response.send_message("That isn't blocked.", ephemeral=True)
        return
    words.remove(w)
    save_json_file(BLOCKWORDS_FILE, blockwords)
    await interaction.response.send_message(f"Removed `{w}`.", ephemeral=True)


@blockword_group.command(name="list", description="List blocked words/phrases")
async def blockword_list(interaction: discord.Interaction):
    words = blockwords.get(str(interaction.guild_id), [])
    if not words:
        await interaction.response.send_message("No words are blocked.", ephemeral=True)
        return
    await interaction.response.send_message("Blocked: " + ", ".join(f"`{w}`" for w in words), ephemeral=True)


@client.tree.command(name="blockinvites", description="Toggle auto-deleting Discord invite links")
@app_commands.checks.has_permissions(manage_messages=True)
async def blockinvites_cmd(interaction: discord.Interaction):
    key = str(interaction.guild_id)
    blockinvites[key] = not blockinvites.get(key, False)
    save_json_file(BLOCKINVITES_FILE, blockinvites)
    await interaction.response.send_message(
        "Invite links will now be auto-deleted." if blockinvites[key] else "Invite link filtering turned off.",
        ephemeral=True,
    )


@client.tree.command(name="starboard", description="Set up the starboard: ⭐-reacted messages get reposted here")
@app_commands.describe(channel="Where starred messages get reposted", threshold="How many ⭐ reactions needed")
@app_commands.checks.has_permissions(administrator=True)
async def starboard_cmd(interaction: discord.Interaction, channel: discord.TextChannel, threshold: app_commands.Range[int, 1, 50] = 3):
    starboard_config[str(interaction.guild_id)] = {"channel_id": channel.id, "threshold": threshold}
    save_json_file(STARBOARD_FILE, starboard_config)
    await interaction.response.send_message(
        f"Messages with {threshold}+ ⭐ will now be posted to {channel.mention}.", ephemeral=True
    )


@starboard_cmd.error
async def starboard_cmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Administrator to do that.", ephemeral=True)
    else:
        print(f"/starboard error: {error}")


def build_join_leave_config(
    channel: discord.TextChannel,
    message: str,
    title: Optional[str],
    color: Optional[str],
    banner_url: Optional[str],
    thumbnail_url: Optional[str],
    use_embed: bool,
    dm_instead: bool,
    mention_user: bool,
) -> dict:
    return {
        "channel_id": channel.id,
        "message": message,
        "title": title,
        "color": color,
        "banner_url": banner_url,
        "thumbnail_url": thumbnail_url,
        "use_embed": use_embed,
        "dm_instead": dm_instead,
        "mention_user": mention_user,
    }


async def validate_join_leave_inputs(
    interaction: discord.Interaction, color: Optional[str], banner_url: Optional[str], thumbnail_url: Optional[str]
) -> bool:
    if color and not parse_hex_color(color):
        await interaction.response.send_message(
            "That doesn't look like a valid hex color. Try something like `#57F287`.", ephemeral=True
        )
        return False
    for label, url in (("banner_url", banner_url), ("thumbnail_url", thumbnail_url)):
        if url and not url.startswith(("http://", "https://")):
            await interaction.response.send_message(f"`{label}` must be a link starting with http(s)://", ephemeral=True)
            return False
    return True


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
    await interaction.response.send_message(f"Timed out {user.mention} for {minutes} minute(s).")


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
    await interaction.response.send_message(f"Warned {user.mention} (warning #{count}). Reason: {reason}")


@warn.error
async def warn_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Kick Members to do that.", ephemeral=True)
    else:
        print(f"/warn error: {error}")


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
            "`/warn` and `/warnings` *(Kick Members)* — log and view warnings\n"
            "`/unban` *(Ban Members)* — unban by raw user ID\n"
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
            "`/purge` `/fullpurge` `/kick` `/ban` `/timeout` `/warn` `/warnings` `/unban` "
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
        client.run(TOKEN)
