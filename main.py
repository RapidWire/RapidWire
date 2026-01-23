import discord
from discord import app_commands
from discord.ext import tasks
import config
import bot_commands
from RapidWire import RapidWire
from time import time
import asyncio

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
Rapid: RapidWire = None
last_check_timestamp = int(time())

@tasks.loop(seconds=10)
async def check_claims_and_notify():
    global last_check_timestamp
    current_time = int(time())

    try:
        claims = await Rapid.Claims.get_claims_created_after(last_check_timestamp)
        for claim in claims:
            if await Rapid.NotificationPermissions.check(claim.payer_id, claim.claimant_id):
                try:
                    payer = await client.fetch_user(claim.payer_id)
                    claimant = await client.fetch_user(claim.claimant_id)
                    currency = await Rapid.Currencies.get(claim.currency_id)

                    if not payer.dm_channel:
                        await payer.create_dm()

                    embed = bot_commands.create_claim_notification_embed(claim, claimant, currency)
                    view = bot_commands.ClaimNotificationView(claim.claim_id, Rapid)

                    await payer.dm_channel.send(embed=embed, view=view)
                except Exception as e:
                    print(f"請求通知の送信中にエラー: {e}")

    except Exception as e:
        print(f"請求の確認中にエラー: {e}")

    last_check_timestamp = current_time

@tasks.loop(minutes=30)
async def update_stakes_task():
    try:
        await Rapid.update_stale_stakes()
    except Exception as e:
        print(f"ステーキング更新タスクでエラーが発生しました: {e}")

@client.event
async def on_ready():
    await Rapid.initialize()
    Rapid.Config = config.RapidWireConfig
    if not check_claims_and_notify.is_running():
        check_claims_and_notify.start()
    if not update_stakes_task.is_running():
        update_stakes_task.start()
    print(f'"{client.user}" としてログインしました')
    try:
        await tree.sync()
        print("スラッシュコマンドを同期しました。")
    except Exception as e:
        print(f"コマンドの同期中にエラーが発生しました: {e}")

@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    if not message.content.startswith(f"<@{client.user.id}>"):
        return

    # Remove the mention from the start to process arguments
    content_after_mention = message.content[len(f"<@{client.user.id}>"):].strip()
    args = content_after_mention.split()
    if len(args) > 0:
        args[0] = args[0].upper()

    if message.author.id in config.Discord.admins:
        if len(args) > 0 and args[0] == "KILL":
            print("シャットダウンコマンドを受け取りました。")
            await Rapid.close()
            await client.close()
            return

    if len(args) > 0 and args[0] == "API":
        channel_id = None
        if len(args) > 1:
            if args[1].isdigit():
                channel_id = int(args[1])
            else:
                await message.reply("チャンネルIDは数値で指定してください。")
                return

        if message.author.bot and channel_id is None:
            await message.reply("BotがAPIキーを発行する場合は、チャンネルIDを指定する必要があります。")
            return

        target_channel = None
        if channel_id:
            target_channel = client.get_channel(channel_id)
            if not target_channel:
                try:
                    target_channel = await client.fetch_channel(channel_id)
                except discord.NotFound:
                    await message.reply("指定されたチャンネルが見つかりませんでした。")
                    return
                except discord.Forbidden:
                    await message.reply("指定されたチャンネルへのアクセス権がありません。")
                    return
                except Exception as e:
                    await message.reply(f"チャンネルの取得中にエラーが発生しました: `{e}`")
                    return
            else:
                if message.guild is None:
                    await message.reply("チャンネルを指定してAPIキーを発行する場合は、サーバー内でコマンドを実行してください。")
                    return
                if not isinstance(target_channel, (discord.abc.GuildChannel, discord.threads.Thread)) or target_channel.guild is None:
                    await message.reply("指定されたチャンネルはサーバーのチャンネルではありません。")
                    return
                if target_channel.guild.id != message.guild.id:
                    await message.reply("指定されたチャンネルは、メッセージを送信したサーバーと同一のサーバーにある必要があります。")
                    return

        try:
            api_key_obj = await Rapid.APIKeys.create(message.author.id)
            key_message = f"{message.author.mention} のAPIキーです。大切に保管してください。\n```{api_key_obj.api_key}```"

            if target_channel:
                try:
                    await target_channel.send(key_message)
                    await message.reply(f"<#{channel_id}> にAPIキーを送信しました。")
                except discord.Forbidden:
                    await message.reply(f"<#{channel_id}> にメッセージを送信できませんでした。権限を確認してください。")
                except Exception as e:
                    await message.reply(f"メッセージの送信中にエラーが発生しました: `{e}`")
            else:
                try:
                    dm_channel = await message.author.create_dm()
                    await dm_channel.send(key_message)
                    await message.reply("APIキーをDMに送信しました。")
                except discord.Forbidden:
                    await message.reply("APIキーをDMに送信できませんでした。DMの受信設定を確認してください。")
                except Exception as e:
                    await message.reply(f"DMの送信中にエラーが発生しました: `{e}`")

        except Exception as e:
            await message.reply(f"APIキーの処理中にエラーが発生しました: `{e}`")

def main():
    global Rapid
    Rapid = RapidWire(db_config=config.MySQL.to_dict())
    bot_commands.setup(tree, Rapid)
    client.run(config.Discord.token)

if __name__ == "__main__":
    main()
