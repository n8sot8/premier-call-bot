async def set_status(self, interaction: discord.Interaction, status: str):
    mid = interaction.message.id
    uid = interaction.user.id

    # まず締切チェック（ここはdefer前にOK）
    with db() as conn:
        row = conn.execute("SELECT status FROM meta WHERE message_id=?", (mid,)).fetchone()
    if row and row[0] == "closed":
        return await interaction.response.send_message("この点呼は締切済み！", ephemeral=True)

    # 先にACK（タイムアウト対策）
    await interaction.response.defer(thinking=False)

    try:
        with db() as conn:
            cur = conn.execute(
                "UPDATE attendance SET status=? WHERE message_id=? AND user_id=?",
                (status, mid, uid)
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO attendance(message_id,user_id,status) VALUES(?,?,?)",
                    (mid, uid, status)
                )

            row2 = conn.execute("SELECT close_at FROM meta WHERE message_id=?", (mid,)).fetchone()
            close_at = parse_iso(row2[0]) if row2 else None

        await interaction.message.edit(
            embed=tenko_embed(mid, closed=False, close_at=close_at),
            view=TenkoView(disabled=False)
        )
        await interaction.followup.send(
            "参加にしたよ！" if status == "yes" else "不参加にしたよ！",
            ephemeral=True
        )
    except Exception as e:
        # これがあると「失敗しました」じゃなく理由が見える
        await interaction.followup.send(f"エラー起きた: {e}", ephemeral=True)
