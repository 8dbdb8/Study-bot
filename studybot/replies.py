"""スラッシュコマンドなら本人だけに返す、などの送信の共通処理。"""

from io import BytesIO

import discord


def _message_kwargs(content=None, view=None, embed=None):
    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    if view is not None:
        kwargs["view"] = view
    if embed is not None:
        kwargs["embed"] = embed
    return kwargs


async def send_private(ctx, content=None, view=None, embed=None):
    """スラッシュなら本人だけに、!コマンドなら通常どおり送る。"""
    kwargs = _message_kwargs(content, view, embed)
    if getattr(ctx, "interaction", None) is not None:
        kwargs["ephemeral"] = True
    await ctx.send(**kwargs)


async def respond_private(interaction, content=None, view=None, embed=None):
    await interaction.response.send_message(
        ephemeral=True, **_message_kwargs(content, view, embed)
    )


async def send_png(ctx, png, filename):
    """PNG画像（bytes）をファイルとして送る。"""
    await ctx.send(file=discord.File(BytesIO(png), filename=filename))


# Discord の1メッセージの上限は2000文字。余裕を持って分ける
MESSAGE_LIMIT = 1900


def split_message(text, limit=MESSAGE_LIMIT):
    """長い文章を、なるべく改行の位置で limit 文字以内に分ける。"""
    chunks = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip("\n"))
        rest = rest[cut:].lstrip("\n")
    if rest or not chunks:
        chunks.append(rest)
    return chunks


async def send_long(ctx, text):
    """2000文字を超える文章（AIの返答など）を分けて送る。"""
    for chunk in split_message(text):
        await ctx.send(chunk)


def _is_reference_gone(error):
    """返信先のメッセージが消えていて送れなかったエラーか。"""
    if isinstance(error, discord.NotFound):
        return True
    return (
        isinstance(error, discord.HTTPException)
        and error.code in (10008, 50035)
        and "message_reference" in str(error.text)
    )


async def safe_reply(message, content):
    """message に返信する。返信先が消えていたら送らずに None を返す。"""
    if len(content) > 2000:
        content = content[:1990] + "\n…（長いため省略）"
    try:
        return await message.reply(content, mention_author=False)
    except discord.HTTPException as error:
        if _is_reference_gone(error):
            print("[reply] 返信先の投稿が削除されていたため、返信しませんでした")
            return None
        raise
