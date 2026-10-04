"""コマンドやボタンで予期しないエラーが起きたときに、黙らずに知らせる。

Discord は何も返さないと「アプリケーションが応答しませんでした」と
表示するだけなので、本人に短いメッセージを返し、詳しい内容は
studybot.log（標準出力）に残す。
"""

import traceback

import discord
from discord import app_commands
from discord.ext import commands


ERROR_MESSAGE = (
    "⚠️ 処理中にエラーが発生しました。"
    "もう一度試しても直らない場合は、studybot.log を確認してください。"
)


def log_error(where, error):
    print(f"[error] {where} でエラー: {error!r}")
    traceback.print_exception(type(error), error, error.__traceback__)


async def notify_interaction_error(interaction):
    """ボタンやスラッシュコマンドの相手に、本人だけ見えるエラー表示を返す。"""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(ERROR_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(
                ERROR_MESSAGE, ephemeral=True
            )
    except discord.HTTPException as error:
        print(f"[error] エラーの通知も送れませんでした: {error!r}")


def _input_error_message(error):
    """入力ミスなど、利用者が直せるエラーなら案内文。そうでなければ None。"""
    if isinstance(error, commands.MissingRequiredArgument):
        return f"⚠️ `{error.param.name}` を指定してください。"
    if isinstance(error, commands.BadArgument):
        return "⚠️ 入力の形式が正しくありません。もう一度確認してください。"
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return "⚠️ このコマンドはここでは使えません。"
    return None


async def on_command_error(ctx, error):
    """!コマンドと、/コマンド（ハイブリッド）のエラー。"""
    if isinstance(error, commands.CommandNotFound):
        return
    original = getattr(error, "original", error)
    message = _input_error_message(original)
    if message is None:
        log_error(f"コマンド {ctx.command}", original)
        message = ERROR_MESSAGE
    try:
        await ctx.send(message, ephemeral=True)
    except discord.HTTPException as send_error:
        print(f"[error] エラーの通知も送れませんでした: {send_error!r}")


async def on_app_command_error(interaction, error):
    """/data や /setup など、スラッシュ専用コマンドのエラー。"""
    original = getattr(error, "original", error)
    message = _input_error_message(original)
    if message is None:
        log_error(f"コマンド /{getattr(interaction.command, 'name', '?')}", original)
        await notify_interaction_error(interaction)
        return
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def _view_on_error(self, interaction, error, item):
    log_error(f"ボタン・選択肢（{type(self).__name__}）", error)
    await notify_interaction_error(interaction)


async def _modal_on_error(self, interaction, error):
    log_error(f"入力フォーム（{type(self).__name__}）", error)
    await notify_interaction_error(interaction)


def install_error_handlers(bot):
    bot.add_listener(on_command_error)
    bot.tree.on_error = on_app_command_error
    # すべてのボタン・選択肢・入力フォームに同じエラー処理を使う
    discord.ui.View.on_error = _view_on_error
    discord.ui.Modal.on_error = _modal_on_error
