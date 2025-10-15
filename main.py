import discord
from discord import app_commands
import config
import bot_commands
from RapidWire import RapidWire

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
Rapid: RapidWire = None

@client.event
async def on_ready():
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
    bot_commands.setup(tree)
    client.run(config.Discord.token)

if __name__ == "__main__":
    main()
