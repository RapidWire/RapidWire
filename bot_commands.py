import discord
from discord import app_commands, Embed, Color, User, File
from typing import Optional, Literal
import io
import re
from decimal import Decimal
from time import time

import config
from RapidWire import RapidWire, exceptions, structs

Rapid = RapidWire(db_config=config.MySQL.to_dict())
Rapid.Config.Contract.max_cost = config.Contract.max_cost
SYSTEM_USER_ID = 0

def create_error_embed(description: str) -> Embed:
    return Embed(title="エラー", description=description, color=Color.red())

def create_success_embed(description: str, title: str = "成功") -> Embed:
    return Embed(title=title, description=description, color=Color.green())

def format_amount(amount: int) -> str:
    return f"{Decimal(amount) / Decimal(10**config.decimal_places):,.{config.decimal_places}f}"

async def _get_currency(interaction: discord.Interaction, symbol: Optional[str]) -> Optional[structs.Currency]:
    if symbol:
        return Rapid.Currencies.get_by_symbol(symbol.upper())
    if interaction.guild:
        return Rapid.Currencies.get(currency_id=interaction.guild.id)
    return None

@app_commands.command(name="balance", description="あなたの保有資産を表示します。")
@app_commands.describe(user="残高を表示するユーザー")
async def balance(interaction: discord.Interaction, user: Optional[User] = None):
    target_user = user or interaction.user
    await interaction.response.defer(thinking=True)
    try:
        user_model = Rapid.get_user(target_user.id)
        balances = user_model.get_all_balances()

        if not balances:
            await interaction.followup.send(embed=create_success_embed(f"{target_user.display_name}は資産を保有していません。", title="残高"))
            return

        embed = Embed(title=f"{target_user.display_name}の保有資産", color=Color.green())
        for bal in balances:
            currency = Rapid.Currencies.get(currency_id=bal.currency_id)
            if currency:
                embed.add_field(
                    name=f"{currency.name} ({currency.symbol})",
                    value=f"`{format_amount(bal.amount)}`",
                    inline=False
                )
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"残高の取得中に予期せぬエラーが発生しました。\n`{e}`"))

@app_commands.command(name="transfer", description="指定したユーザーに通貨を送金します。")
@app_commands.describe(user="送金先のユーザー", amount="送金する量", symbol="送金する通貨のシンボル (任意)", input_data="コントラクトに渡すデータ")
async def transfer(interaction: discord.Interaction, user: User, amount: float, symbol: Optional[str] = None, input_data: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    
    if amount <= 0:
        await interaction.followup.send(embed=create_error_embed("送金額は0より大きい必要があります。"))
        return

    try:
        currency = await _get_currency(interaction, symbol)
        if not currency:
            await interaction.followup.send(embed=create_error_embed("対象の通貨が見つかりませんでした。サーバー内で実行するか、シンボルを正しく指定してください。"))
            return

        int_amount = int(Decimal(str(amount)) * (10**config.decimal_places))
        
        tx, msg = Rapid.transfer(
            source_id=interaction.user.id,
            destination_id=user.id,
            currency_id=currency.currency_id,
            amount=int_amount,
            input_data=input_data
        )

        desc = f"{user.mention} へ `{format_amount(int_amount)} {currency.symbol}` の送金が完了しました。\n\n**トランザクションID:** `{tx.transaction_id}`"
        if msg:
            desc += f"\n\n**コントラクトからのメッセージ:**\n```\n{msg}\n```"
        
        await interaction.followup.send(embed=create_success_embed(desc, title="送金完了"))

    except exceptions.InsufficientFunds:
        await interaction.followup.send(embed=create_error_embed("残高が不足しています。"))
    except exceptions.TransactionCanceledByContract as e:
        await interaction.followup.send(embed=create_error_embed(f"送金は受信者のコントラクトによってキャンセルされました。\n**理由:** `{e}`"))
    except exceptions.ContractError as e:
        await interaction.followup.send(embed=create_error_embed(f"コントラクトの処理中にエラーが発生しました。\n```{e}```"))
    except exceptions.TransactionError as e:
        await interaction.followup.send(embed=create_error_embed(f"取引の処理中にエラーが発生しました。\n`{e}`"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"予期せぬエラーが発生しました。\n`{e}`"))

@app_commands.command(name="history", description="取引履歴を表示します。")
@app_commands.describe(transaction_id="詳細を表示する取引ID (任意)", user="履歴を表示するユーザー (任意)", page="ページ番号")
async def history(interaction: discord.Interaction, transaction_id: Optional[int] = None, user: Optional[User] = None, page: int = 1):
    await interaction.response.defer(thinking=True)
    try:
        if transaction_id:
            tx = Rapid.Transactions.get(transaction_id)
            if not tx:
                await interaction.followup.send(embed=create_error_embed("指定された取引IDは見つかりませんでした。"))
                return

            currency = Rapid.Currencies.get(tx.currency_id)
            source_user = f"<@{tx.source_id}>" if tx.source_id != SYSTEM_USER_ID else "システム"
            dest_user = f"<@{tx.destination_id}>" if tx.destination_id != SYSTEM_USER_ID else "システム"
            
            embed = Embed(title=f"取引詳細: ID {tx.transaction_id}", color=Color.blue())
            embed.add_field(name="日時", value=f"<t:{tx.timestamp}:F>", inline=False)
            embed.add_field(name="From", value=source_user, inline=True)
            embed.add_field(name="To", value=dest_user, inline=True)
            embed.add_field(name="金額", value=f"`{format_amount(tx.amount)} {currency.symbol if currency else '???'}`", inline=False)
            if tx.input_data:
                embed.add_field(name="メモ (Input Data)", value=f"```{tx.input_data}```", inline=False)
            
            await interaction.followup.send(embed=embed)
            return

        target_user = user or interaction.user
        transactions = Rapid.Transactions.get_user_history(target_user.id, page=page)
        if not transactions:
            await interaction.followup.send(embed=create_success_embed(f"{target_user.display_name}の取引履歴はありません。", "取引履歴"))
            return

        embed = Embed(title=f"{target_user.display_name}の取引履歴 (ページ {page})", color=Color.blue())
        for tx in transactions:
            currency = Rapid.Currencies.get(tx.currency_id)
            if not currency: continue

            source_user = f"<@{tx.source_id}>" if tx.source_id != SYSTEM_USER_ID else "システム"
            dest_user = f"<@{tx.destination_id}>" if tx.destination_id != SYSTEM_USER_ID else "システム"
            
            direction_emoji = "↔️"
            direction_text = ""
            if tx.source_id == target_user.id:
                direction_emoji = "📤"
                direction_text = f"to {dest_user}"
            elif tx.destination_id == target_user.id:
                direction_emoji = "📥"
                direction_text = f"from {source_user}"

            field_name = f"{direction_emoji} | ID: {tx.transaction_id} | <t:{tx.timestamp}:R>"
            field_value = f"`{format_amount(tx.amount)} {currency.symbol}` {direction_text}"
            embed.add_field(name=field_name, value=field_value, inline=False)
        
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"履歴の取得中にエラーが発生しました。\n`{e}`"))

currency_group = app_commands.Group(name="currency", description="通貨に関連するコマンド")

@currency_group.command(name="create", description="このサーバーに新しい通貨を発行します。")
@app_commands.describe(name="通貨の名前", symbol="通貨のシンボル", supply="初期供給量", daily_interest_rate="ステーキングの日利(%)")
async def currency_create(interaction: discord.Interaction, name: str, symbol: str, supply: float, daily_interest_rate: float = 0.0):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    try:
        if not re.match(r'^[a-zA-Z]+$', symbol):
            await interaction.followup.send(embed=create_error_embed("シンボルはアルファベット(a-z, A-Z)のみ使用できます。"))
            return
        
        if not re.match(r'^[a-zA-Z][a-zA-Z0-9]*$', name):
            await interaction.followup.send(embed=create_error_embed("名前は英数字のみ使用でき、最初の文字はアルファベットである必要があります。"))
            return

        int_supply = int(Decimal(str(supply)) * (10**config.decimal_places))
        rate_decimal = Decimal(str(daily_interest_rate)) / Decimal(100)
        
        new_currency, tx = Rapid.create_currency(interaction.guild.id, name, symbol.upper(), int_supply, interaction.user.id, rate_decimal)
        
        desc = f"新しい通貨 **{new_currency.name} ({new_currency.symbol})** が発行されました。\n"
        desc += f"総供給量は `{format_amount(new_currency.supply)}` です。\n"
        desc += f"ステーキングの日利は `{daily_interest_rate:.4f}%` です。"
        if tx:
            desc += f"\n初期供給のトランザクションID: `{tx.transaction_id}`"

        await interaction.followup.send(embed=create_success_embed(desc, title="通貨発行成功"))
    except exceptions.DuplicateEntryError:
        await interaction.followup.send(embed=create_error_embed("このサーバーには既に通貨が存在するか、そのシンボルは使用済みです。"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"通貨の作成中にエラーが発生しました。\n`{e}`"))

@currency_group.command(name="info", description="通貨の詳細情報を表示します。")
@app_commands.describe(symbol="情報を表示する通貨のシンボル (任意)")
async def currency_info(interaction: discord.Interaction, symbol: Optional[str] = None):
    await interaction.response.defer(thinking=True)
        
    currency = await _get_currency(interaction, symbol)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("対象の通貨が見つかりませんでした。サーバー内で実行するか、シンボルを正しく指定してください。"))
        return
    
    issuer = await interaction.client.fetch_user(currency.issuer_id)
    embed = Embed(title=f"通貨情報: {currency.name} ({currency.symbol})", color=Color.blue())
    embed.add_field(name="発行サーバーID (通貨ID)", value=f"`{currency.currency_id}`", inline=False)
    embed.add_field(name="発行者", value=issuer.mention, inline=False)
    embed.add_field(name="総供給量", value=f"`{format_amount(currency.supply)}`", inline=False)
    embed.add_field(name="ステーキング日利", value=f"`{currency.daily_interest_rate * 100:.4f}%`", inline=False)
    embed.add_field(name="Mint/利率変更 放棄状態", value="はい" if currency.minting_renounced else "いいえ", inline=True)
    if currency.delete_requested_at:
        embed.add_field(name="削除要求日時", value=f"<t:{currency.delete_requested_at}:F>", inline=True)
    
    await interaction.followup.send(embed=embed)

@currency_group.command(name="mint", description="[管理者] 通貨を追加発行します。")
@app_commands.describe(amount="追加発行する量")
@app_commands.checks.has_permissions(administrator=True)
async def currency_mint(interaction: discord.Interaction, amount: float):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    
    currency = Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return
    if currency.minting_renounced:
        await interaction.followup.send(embed=create_error_embed("この通貨のMint機能は放棄されています。"))
        return

    int_amount = int(Decimal(str(amount)) * (10**config.decimal_places))
    Rapid.mint_currency(currency.currency_id, int_amount, interaction.user.id)
    await interaction.followup.send(embed=create_success_embed(f"`{format_amount(int_amount)} {currency.symbol}` を追加発行しました。", "Mint成功"))

@currency_group.command(name="burn", description="[管理者] 保有する通貨を焼却します。")
@app_commands.describe(amount="焼却する量")
@app_commands.checks.has_permissions(administrator=True)
async def currency_burn(interaction: discord.Interaction, amount: float):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return

    currency = Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return

    int_amount = int(Decimal(str(amount)) * (10**config.decimal_places))
    try:
        Rapid.burn_currency(currency.currency_id, int_amount, interaction.user.id)
        await interaction.followup.send(embed=create_success_embed(f"`{format_amount(int_amount)} {currency.symbol}` を焼却しました。", "Burn成功"))
    except exceptions.InsufficientFunds:
        await interaction.followup.send(embed=create_error_embed("焼却するための残高が不足しています。"))

@currency_group.command(name="renounce", description="[管理者] Mintと利率変更機能を永久に放棄します。")
@app_commands.checks.has_permissions(administrator=True)
async def currency_renounce(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return

    currency = Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return
    if currency.minting_renounced:
        await interaction.followup.send(embed=create_error_embed("この通貨の機能は既に放棄されています。"))
        return
    
    Rapid.Currencies.renounce_minting(currency.currency_id)
    await interaction.followup.send(embed=create_success_embed(f"**{currency.symbol}** のMint機能と利率変更機能を永久に放棄しました。この操作は取り消せません。", "機能放棄"))

@currency_group.command(name="delete", description="[管理者] このサーバーの通貨を削除します。")
@app_commands.checks.has_permissions(administrator=True)
async def currency_delete(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return

    currency = Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return
    
    now = int(time())
    seven_days = 7 * 24 * 60 * 60
    ten_days = 10 * 24 * 60 * 60

    if not currency.delete_requested_at:
        Rapid.Currencies.request_delete(currency.currency_id)
        await interaction.followup.send(embed=create_success_embed(
            f"通貨 **{currency.symbol}** の削除要請を受け付けました。\n"
            f"**7日後から10日後まで**の間に再度このコマンドを実行すると、削除が確定します。",
            "削除要請完了"
        ))
    else:
        time_since_request = now - currency.delete_requested_at
        if time_since_request < seven_days:
            await interaction.followup.send(embed=create_error_embed(
                f"削除の確定には、削除要請から7日間が経過している必要があります。\n"
                f"確定可能になる日時: <t:{currency.delete_requested_at + seven_days}:F>"
            ))
        elif time_since_request > ten_days:
            await interaction.followup.send(embed=create_error_embed(
                "削除要請から10日以上が経過したため、この削除要請は無効になりました。\n"
                "再度削除を要請してください。"
            ))
        else:
            txs = Rapid.delete_currency(currency.currency_id)
            await interaction.followup.send(embed=create_success_embed(
                f"通貨 **{currency.symbol}** を完全に削除しました。\n"
                f"{len(txs)}件の残高焼却トランザクションが作成されました。",
                "通貨削除完了"
            ))

@currency_group.command(name="set-interest", description="[管理者] ステーキングの日利を変更します。")
@app_commands.describe(rate="新しい日利 (%)")
@app_commands.checks.has_permissions(administrator=True)
async def currency_set_interest(interaction: discord.Interaction, rate: float):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return

    currency = Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return
    if currency.minting_renounced:
        await interaction.followup.send(embed=create_error_embed("この通貨の利率変更機能は放棄されています。"))
        return
    
    new_rate_decimal = Decimal(str(rate)) / Decimal(100)
    Rapid.update_daily_interest_rate(currency.currency_id, new_rate_decimal)
    await interaction.followup.send(embed=create_success_embed(f"ステーキングの日利を`{rate:.4f}%`に変更しました。\n既存のステークは自動的に更新されます。", "利率変更完了"))

stake_group = app_commands.Group(name="stake", description="ステーキングに関連するコマンド")

@stake_group.command(name="deposit", description="通貨を預け入れ、ステーキングを開始します。")
@app_commands.describe(amount="預け入れる量", symbol="預け入れる通貨のシンボル (任意)")
async def stake_deposit(interaction: discord.Interaction, amount: float, symbol: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    try:
        currency = await _get_currency(interaction, symbol)
        if not currency:
            await interaction.followup.send(embed=create_error_embed("対象の通貨が見つかりませんでした。"))
            return

        int_amount = int(Decimal(str(amount)) * (10**config.decimal_places))
        stake = Rapid.stake_deposit(interaction.user.id, currency.currency_id, int_amount)
        desc = f"`{format_amount(int_amount)} {currency.symbol}` のステーキングを開始しました。\n**ステークID:** `{stake.stake_id}`"
        await interaction.followup.send(embed=create_success_embed(desc, "ステーキング開始"))
    except exceptions.InsufficientFunds:
        await interaction.followup.send(embed=create_error_embed("ステーキングするための残高が不足しています。"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"エラーが発生しました: {e}"))

@stake_group.command(name="withdraw", description="指定したステークを引き出します。")
@app_commands.describe(stake_id="引き出すステークのID")
async def stake_withdraw(interaction: discord.Interaction, stake_id: int):
    await interaction.response.defer(thinking=True)
    try:
        reward, tx = Rapid.stake_withdraw(stake_id, interaction.user.id)
        currency = Rapid.Currencies.get(tx.currency_id)
        desc = f"ステークID `{stake_id}` を引き出しました。\n"
        desc += f"元本: `{format_amount(tx.amount - reward)} {currency.symbol}`\n"
        desc += f"報酬: `{format_amount(reward)} {currency.symbol}`\n"
        desc += f"合計: `{format_amount(tx.amount)} {currency.symbol}`"
        await interaction.followup.send(embed=create_success_embed(desc, "引き出し完了"))
    except (ValueError, PermissionError) as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"エラーが発生しました: {e}"))

@stake_group.command(name="info", description="あなたのステーキング状況を表示します。")
@app_commands.describe(symbol="表示する通貨のシンボル (任意)")
async def stake_info(interaction: discord.Interaction, symbol: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    
    currency = await _get_currency(interaction, symbol)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("対象の通貨が見つかりませんでした。"))
        return
    
    stakes = Rapid.Stakes.get_for_user(interaction.user.id, currency.currency_id)
    if not stakes:
        await interaction.followup.send(embed=create_success_embed(f"`{currency.symbol}`のステークはありません。", "ステーク情報"))
        return
        
    embed = Embed(title=f"{interaction.user.display_name}のステーク情報 ({currency.symbol})", color=Color.purple())
    
    for stake in stakes:
        field_name = f"ID: {stake.stake_id} | `{format_amount(stake.amount)} {currency.symbol}`"
        field_value = f"開始日時: <t:{stake.staked_at}:F>\n日利: `{stake.daily_interest_rate * 100:.4f}%`"
        embed.add_field(name=field_name, value=field_value, inline=False)
        
    await interaction.followup.send(embed=embed)

contract_group = app_commands.Group(name="contract", description="あなたのアカウントのコントラクトを管理します。")

@contract_group.command(name="set", description="あなたのアカウントにコントラクトを設定します。")
@app_commands.describe(
    script="コントラクトとして実行するPythonコードが書かれたファイル",
    max_cost="このコントラクトの実行を許可する最大コスト (0で無制限)"
)
async def contract_set(interaction: discord.Interaction, script: discord.Attachment, max_cost: Optional[int] = 0):
    await interaction.response.defer(thinking=True)
    try:
        script_content = (await script.read()).decode('utf-8')
        contract = Rapid.set_contract(interaction.user.id, script_content, max_cost)

        embed = create_success_embed("コントラクトを正常に設定しました。")
        embed.add_field(name="計算されたコスト", value=f"`{contract.cost}`", inline=False)
        embed.add_field(name="設定された最大コスト", value=f"`{contract.max_cost}`" if contract.max_cost > 0 else "無制限", inline=False)

        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"コントラクトの設定中にエラーが発生しました。\n`{e}`"))

@contract_group.command(name="get", description="現在設定されているコントラクトを取得します。")
async def contract_get(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    contract = Rapid.Contracts.get(interaction.user.id)
    if contract and contract.script:
        file = File(io.BytesIO(contract.script.encode('utf-8')), filename="contract.py")
        await interaction.followup.send("現在設定されているコントラクト:", file=file)
    else:
        await interaction.followup.send(embed=create_success_embed("現在、コントラクトは設定されていません。", title="コントラクト情報"))

claim_group = app_commands.Group(name="claim", description="請求に関連するコマンド")

@claim_group.command(name="create", description="ユーザーに通貨を請求します。")
@app_commands.describe(user="請求先のユーザー", amount="請求額", description="請求の説明")
async def claim_create(interaction: discord.Interaction, user: User, amount: float, description: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    try:
        currency = Rapid.Currencies.get(currency_id=interaction.guild.id)
        if not currency:
            raise ValueError("このサーバーには通貨が存在しません。")
        
        int_amount = int(Decimal(str(amount)) * (10**config.decimal_places))
        claim = Rapid.Claims.create(interaction.user.id, user.id, currency.currency_id, int_amount, description)
        
        desc = f"{user.mention} への請求を作成しました。\n**請求ID:** `{claim.claim_id}`"
        await interaction.followup.send(embed=create_success_embed(desc, "請求作成完了"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"請求の作成中にエラーが発生しました: {e}"))

@claim_group.command(name="list", description="あなたが関与する請求を一覧表示します。")
async def claim_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        claims = Rapid.Claims.get_for_user(interaction.user.id)
        if not claims:
            await interaction.followup.send(embed=create_success_embed("関連する請求はありません。", "請求一覧"))
            return
        
        embed = Embed(title="請求一覧", color=Color.blue())
        for claim in claims:
            currency = Rapid.Currencies.get(currency_id=claim.currency_id)
            if not currency: continue
            claimant = await interaction.client.fetch_user(claim.claimant_id)
            payer = await interaction.client.fetch_user(claim.payer_id)
            
            field_name = f"ID: {claim.claim_id} | {claim.status.upper()} | {format_amount(claim.amount)} {currency.symbol}"
            field_value = f"請求者: {claimant.mention}\n支払者: {payer.mention}\n説明: {claim.description or 'N/A'}"
            embed.add_field(name=field_name, value=field_value, inline=False)
            
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"請求の取得中にエラーが発生しました: {e}"))

@claim_group.command(name="pay", description="あなた宛の請求を支払います。")
@app_commands.describe(claim_id="支払う請求のID")
async def claim_pay(interaction: discord.Interaction, claim_id: int):
    await interaction.response.defer(thinking=True)
    try:
        tx, _ = Rapid.pay_claim(claim_id, interaction.user.id)
        desc = f"請求ID `{claim_id}` の支払いが完了しました。\n**トランザクションID:** `{tx.transaction_id}`"
        await interaction.followup.send(embed=create_success_embed(desc, "支払い完了"))
    except (ValueError, PermissionError) as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))
    except exceptions.InsufficientFunds:
        await interaction.followup.send(embed=create_error_embed("残高が不足しています。"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"支払処理中にエラーが発生しました: {e}"))

@claim_group.command(name="cancel", description="未払いの請求をキャンセルします。")
@app_commands.describe(claim_id="キャンセルする請求のID")
async def claim_cancel(interaction: discord.Interaction, claim_id: int):
    await interaction.response.defer(thinking=True)
    try:
        Rapid.cancel_claim(claim_id, interaction.user.id)
        desc = f"請求ID `{claim_id}` をキャンセルしました。"
        await interaction.followup.send(embed=create_success_embed(desc, "キャンセル完了"))
    except (ValueError, PermissionError) as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"キャンセル処理中にエラーが発生しました: {e}"))

def setup(tree: app_commands.CommandTree):
    tree.add_command(balance)
    tree.add_command(transfer)
    tree.add_command(history)
    tree.add_command(currency_group)
    tree.add_command(stake_group)
    tree.add_command(contract_group)
    tree.add_command(claim_group)
