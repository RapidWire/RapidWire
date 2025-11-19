import discord
from discord import app_commands
from discord.ext import tasks
import config
import bot_commands
from RapidWire import RapidWire
from time import time

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
        claims = Rapid.Claims.get_claims_created_after(last_check_timestamp)
        for claim in claims:
            if Rapid.NotificationPermissions.check(claim.payer_id, claim.claimant_id):
                try:
                    payer = await client.fetch_user(claim.payer_id)
                    claimant = await client.fetch_user(claim.claimant_id)
                    currency = Rapid.Currencies.get(claim.currency_id)

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

@client.event
async def on_ready():
    if not check_claims_and_notify.is_running():
        check_claims_and_notify.start()
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

    if message.author.id in config.Discord.admins:
        args = message.content.split(' ')
        if len(args) > 1 and args[1] == "kill":
            print("シャットダウンコマンドを受け取りました。")
            await client.close()
            return

    content_after_mention = message.content.replace(f"<@{client.user.id}>", "")

    if content_after_mention.strip():
        try:
            Rapid.Contracts.set(message.author.id, content_after_mention)
            await message.reply("コントラクトを登録しました。")
        except Exception as e:
            await message.reply(f"コントラクトの登録中にエラーが発生しました: `{e}`")
    else:
        try:
            api_key_obj = Rapid.APIKeys.create(message.author.id)
            try:
                dm_channel = await message.author.create_dm()
                await dm_channel.send(f"あなたのAPIキーです。大切に保管してください:\n`{api_key_obj.api_key}`")
                await message.reply("APIキーをDMに送信しました。")
            except discord.Forbidden:
                await message.reply("APIキーをDMに送信できませんでした。DMの受信設定を確認してください。")
        except Exception as e:
            await message.reply(f"APIキーの処理中にエラーが発生しました: `{e}`")

def main():
    global Rapid
    Rapid = RapidWire(db_config=config.MySQL.to_dict())
    Rapid.Config.Contract.max_cost = 100
    bot_commands.setup(tree)
    client.run(config.Discord.token)

if __name__ == "__main__":
    main()
