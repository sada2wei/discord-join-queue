# main.py
import os
import asyncio
from collections import deque
from dotenv import load_dotenv
import discord
from discord.ext import tasks

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # optional

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = discord.Bot(intents=intents)

# シンプルな FIFO キュー（user.id を入れる）
join_queue = deque()
queue_lock = asyncio.Lock()

# 設定（サーバに合わせてチャンネル名を変更）
WAIT_VOICE_NAME = "待機"        # ユーザーが先に入っておくボイス
TARGET_VOICES = ["参加-1", "参加-2", "参加-3"]  # 最大3つの参加用ボイス
QUEUE_TEXT_CHANNEL = "参加希望チャンネル"  # キュー表示用のテキストチャンネル

class JoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="参加希望", style=discord.ButtonStyle.primary, custom_id="join_button")
    async def join_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        user = interaction.user
        async with queue_lock:
            # 重複登録防止
            if user.id in join_queue:
                await interaction.response.send_message(f"既にキューに登録済み（順位: {list(join_queue).index(user.id)+1}）", ephemeral=True)
                return
            join_queue.append(user.id)
            pos = len(join_queue)
        # 投稿（参加希望チャンネルへ）
        guild = interaction.guild
        txt = discord.utils.get(guild.text_channels, name=QUEUE_TEXT_CHANNEL)
        if txt:
            await txt.send(f"{user.mention} が参加希望に登録されました。番号: {pos}")
        # 即レス（ephemeral）
        await interaction.response.send_message(f"参加希望を受け付けました。あなたの番号: {pos}\n※自動でボイスに移動するには先に「{WAIT_VOICE_NAME}」に入ってください。", ephemeral=True)
        # もし空きがあれば昇格処理を呼ぶ（非同期）
        bot.loop.create_task(try_fill_slots(guild))

async def get_target_voice_channels(guild: discord.Guild):
    channels = []
    for name in TARGET_VOICES:
        ch = discord.utils.get(guild.voice_channels, name=name)
        if ch:
            channels.append(ch)
    return channels

async def find_available_voice_slot(guild: discord.Guild):
    targets = await get_target_voice_channels(guild)
    for ch in targets:
        if len(ch.members) < 3:  # 各チャンネル3人上限を想定
            return ch
    return None

async def try_fill_slots(guild: discord.Guild):
    async with queue_lock:
        if not join_queue:
            return
    # ループして空きに入れていく
    while True:
        async with queue_lock:
            if not join_queue:
                break
            next_user_id = join_queue[0]
        member = guild.get_member(next_user_id)
        if not member:
            # サーバにいない or キャッシュ外 -> キューから削除
            async with queue_lock:
                join_queue.popleft()
            continue
        # ユーザーが待機ボイスにいるか確認（Discord の仕様上必要）
        if not member.voice or not member.voice.channel:
            # ユーザーがどのボイスにもいない -> skip（通知しても良い）
            # ここでは一旦取り出さずに待機。（運用上、一定時間後に自動キャンセル等の実装を推奨）
            break

        slot = await find_available_voice_slot(guild)
        if not slot:
            break  # 全て満員
        # 移動（bot に Move Members 権限が必要）
        try:
            await member.move_to(slot)
            async with queue_lock:
                join_queue.popleft()
            # 通知
            txt = discord.utils.get(guild.text_channels, name=QUEUE_TEXT_CHANNEL)
            if txt:
                await txt.send(f"{member.mention} を {slot.name} に移動しました。")
            # 少し待ってから次へ
            await asyncio.sleep(1)
        except discord.Forbidden:
            # 権限エラー
            txt = discord.utils.get(guild.text_channels, name=QUEUE_TEXT_CHANNEL)
            if txt:
                await txt.send(f"権限エラー: {member.mention} を移動できませんでした。Bot の Move Members 権限を確認してください。")
            break
        except Exception as e:
            txt = discord.utils.get(guild.text_channels, name=QUEUE_TEXT_CHANNEL)
            if txt:
                await txt.send(f"移動中にエラーが発生しました: {e}")
            break

@bot.event
async def on_ready():
    print("Bot ready:", bot.user)
    # ボタンを常駐メッセージに置くため、最初にチャンネルを探して送信（既にあるなら編集する）
    for guild in bot.guilds:
        txt = discord.utils.get(guild.text_channels, name=QUEUE_TEXT_CHANNEL)
        if txt:
            # すでにボタン付きメッセージがあるか探す（簡易）
            await txt.send("参加希望ボタン：", view=JoinView())

# voice からメンバーが抜けたことを検出して空きがあれば補充
@bot.event
async def on_voice_state_update(member, before, after):
    # もし before.channel が参加用チャンネルで、after.channel が None または別チャンネルなら空きが出た可能性
    guild = member.guild
    # 非同期に処理
    bot.loop.create_task(try_fill_slots(guild))

if __name__ == "__main__":
    bot.run(TOKEN)
