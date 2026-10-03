"""スラッシュコマンドなら本人だけに返す、などの送信の共通処理。"""

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
