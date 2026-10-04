"""Botが使うチャンネルの設定と検索。

/setup で選んだチャンネルをサーバーごとにIDで覚える。
未設定のときは config の名前（勉強部屋・勉強ログ・SG用語集）で探す。
"""

import sqlite3
from contextlib import closing

import discord

from studybot import config


# 種類 -> (説明, 既定の名前, "voice" か "text")
CHANNEL_KINDS = {
    "study_voice": (
        "勉強時間を記録するボイスチャンネル",
        config.STUDY_VOICE_CHANNEL_NAME,
        "voice",
    ),
    "study_log": (
        "勉強ログと通知のテキストチャンネル",
        config.STUDY_LOG_CHANNEL_NAME,
        "text",
    ),
    "glossary": (
        "SG用語集を使うテキストチャンネル",
        config.SG_GLOSSARY_CHANNEL_NAME,
        "text",
    ),
    "ai_report": (
        "週間レポートとNotionの通知のテキストチャンネル",
        config.AI_REPORT_CHANNEL_NAME,
        "text",
    ),
}


def init_channel_settings_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_channel_settings (
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, kind)
        )
    """)


def get_channel_setting(db_path, guild_id, kind):
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("""
            SELECT channel_id FROM guild_channel_settings
            WHERE guild_id = ? AND kind = ?
        """, (guild_id, kind)).fetchone()
    return row[0] if row else None


def set_channel_setting(db_path, guild_id, kind, channel_id):
    """channel_id が None なら設定を消して名前検索に戻す。"""
    if kind not in CHANNEL_KINDS:
        raise ValueError(f"不明なチャンネル種類: {kind}")
    with closing(sqlite3.connect(db_path)) as conn:
        with conn:
            if channel_id is None:
                conn.execute("""
                    DELETE FROM guild_channel_settings
                    WHERE guild_id = ? AND kind = ?
                """, (guild_id, kind))
            else:
                conn.execute("""
                    INSERT INTO guild_channel_settings (
                        guild_id, kind, channel_id
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(guild_id, kind) DO UPDATE SET
                        channel_id = excluded.channel_id
                """, (guild_id, kind, channel_id))


def _configured_channel(guild, kind):
    """/setup で選ばれ、今もサーバーにあるチャンネル。なければ None。"""
    try:
        channel_id = get_channel_setting(config.DB_PATH, guild.id, kind)
    except sqlite3.Error:
        return None
    if not channel_id:
        return None
    return guild.get_channel(channel_id)


def find_channel(guild, kind):
    if guild is None:
        return None

    channel = _configured_channel(guild, kind)
    if channel is not None:
        return channel

    _, name, channel_type = CHANNEL_KINDS[kind]
    candidates = (
        guild.voice_channels if channel_type == "voice"
        else guild.text_channels
    )
    return discord.utils.get(candidates, name=name)


def is_channel(channel, kind, guild=None):
    """channel がその用途のチャンネルか。設定がなければ名前で判定する。"""
    guild = guild or getattr(channel, "guild", None)
    if channel is None or guild is None:
        return False

    configured = _configured_channel(guild, kind)
    if configured is not None:
        return channel.id == configured.id

    name = CHANNEL_KINDS[kind][1]
    return getattr(channel, "name", "").casefold() == name.casefold()


def channel_label(guild, kind):
    """メッセージに書く表記。/setup で選んだチャンネルならリンクにする。"""
    channel = _configured_channel(guild, kind) if guild is not None else None
    if channel is not None:
        return channel.mention
    return f"#{CHANNEL_KINDS[kind][1]}"
