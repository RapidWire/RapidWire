import discord
from discord import app_commands, Embed, Color, User, File
from typing import Optional, Literal
import io
from decimal import Decimal
from time import time
from datetime import datetime
import hashlib

import config
from RapidWire import RapidWire, exceptions, structs
from RapidWire.constants import INTEREST_RATE_SCALE

Rapid: RapidWire = None
SYSTEM_USER_ID = 0

class EmbedField:
    def __init__(self, name: str, value: str, inline: bool = True):
        self.name: str = name
        self.value: str = value
        self.inline: bool = inline

class SwapConfirmationView(discord.ui.View):
    def __init__(self, original_author: User, from_symbol: str, to_symbol: str, amount_in: int, amount_out_est: int):
        super().__init__(timeout=30)
        self.original_author = original_author
        self.from_symbol = from_symbol
        self.to_symbol = to_symbol
        self.amount_in = amount_in
        self.amount_out_est = amount_out_est
        self.swap_executed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.original_author.id:
            await interaction.response.send_message("この操作はコマンドを実行した本人しか行えません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="確定", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Slippage check (tolerance 1%)
            route = await Rapid.find_swap_route(self.from_symbol, self.to_symbol)
            from_currency = await Rapid.Currencies.get_by_symbol(self.from_symbol)
            current_rate = Rapid.get_swap_rate(self.amount_in, route, from_currency.currency_id)

            slippage_tolerance = Decimal("0.01")
            min_amount_out = Decimal(self.amount_out_est) * (Decimal("1") - slippage_tolerance)

            if Decimal(current_rate) < min_amount_out:
                await interaction.response.edit_message(embed=create_error_embed("価格が変動したため、スワップを中断しました。再度お試しください。"), view=None)
                self.stop()
                return

            amount_out, _ = await Rapid.swap(self.from_symbol, self.to_symbol, self.amount_in, interaction.user.id)
            self.swap_executed = True
            desc = f"`{format_amount(self.amount_in)} {self.from_symbol}` を `{format_amount(amount_out)} {self.to_symbol}` にスワップしました。"
            await interaction.response.edit_message(embed=create_success_embed(desc, "スワップ完了"), view=None)
        except Exception as e:
            await interaction.response.edit_message(embed=create_error_embed(f"スワップ中にエラーが発生しました: {e}"), view=None)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=create_error_embed("スワップはキャンセルされました。"), view=None)
        self.stop()

class ClaimNotificationView(discord.ui.View):
    def __init__(self, claim_id: int, rapid_instance: RapidWire):
        super().__init__(timeout=None)
        self.claim_id = claim_id
        self.rapid = rapid_instance

    @discord.ui.button(label="承認", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            tx = await self.rapid.pay_claim(self.claim_id, interaction.user.id)
            desc = f"請求ID `{self.claim_id}` の支払いが完了しました。"
            fields = [EmbedField("転送ID", f"`{tx.transfer_id}`", False)]
            await interaction.response.edit_message(embed=create_success_embed(desc, "支払い完了", fields=fields), view=None)
        except Exception as e:
            await interaction.response.edit_message(embed=create_error_embed(f"支払処理中にエラーが発生しました: {e}"), view=None)

    @discord.ui.button(label="拒否", style=discord.ButtonStyle.red)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            claim = await self.rapid.cancel_claim(self.claim_id, interaction.user.id)
            desc = f"請求ID `{self.claim_id}` を拒否しました。"
            await interaction.response.edit_message(embed=create_success_embed(desc, "請求拒否"), view=None)
        except Exception as e:
            await interaction.response.edit_message(embed=create_error_embed(f"拒否処理中にエラーが発生しました: {e}"), view=None)

    @discord.ui.button(label="このユーザーからの通知を停止", style=discord.ButtonStyle.grey)
    async def stop_notifications(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            claim = await self.rapid.Claims.get(self.claim_id)
            if claim:
                await self.rapid.NotificationPermissions.remove(interaction.user.id, claim.claimant_id)
                await interaction.response.send_message("このユーザーからの通知を停止しました。", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"通知停止中にエラーが発生しました: {e}", ephemeral=True)

def create_claim_notification_embed(claim: structs.Claim, claimant: User, currency: structs.Currency) -> Embed:
    embed = Embed(title="請求の通知", color=Color.blue())
    embed.add_field(name="請求者", value=claimant.mention, inline=False)
    embed.add_field(name="金額", value=f"`{format_amount(claim.amount)} {currency.symbol}`", inline=False)
    if claim.description:
        embed.add_field(name="説明", value=claim.description, inline=False)
    embed.set_footer(text=f"請求ID: {claim.claim_id}")
    return embed

def create_error_embed(description: str, fields: Optional[list[EmbedField]] = None) -> Embed:
    embed = Embed(title="エラー", description=description, color=Color.red())
    if fields:
        for emfi in fields:
            embed.add_field(name=emfi.name, value=emfi.value, inline=emfi.inline)
    return embed

def create_success_embed(description: str, title: str = "成功", fields: Optional[list[EmbedField]] = None) -> Embed:
    embed = Embed(title=title, description=description, color=Color.green())
    if fields:
        for emfi in fields:
            embed.add_field(name=emfi.name, value=emfi.value, inline=emfi.inline)
    return embed

def format_amount(amount: int) -> str:
    return f"{Decimal(amount) / Decimal(10**Rapid.Config.decimal_places):,.{Rapid.Config.decimal_places}f}"

async def _get_currency(interaction: discord.Interaction, symbol: Optional[str]) -> Optional[structs.Currency]:
    if symbol:
        return await Rapid.Currencies.get_by_symbol(symbol.upper())
    if interaction.guild:
        return await Rapid.Currencies.get(currency_id=interaction.guild.id)
    return None

@app_commands.command(name="balance", description="あなたの保有資産を表示します。")
@app_commands.describe(user="残高を表示するユーザー")
async def balance(interaction: discord.Interaction, user: Optional[User] = None):
    target_user = user or interaction.user
    await interaction.response.defer(thinking=True)
    try:
        user_model = Rapid.get_user(target_user.id)
        balances = await user_model.get_all_balances()

        if not balances:
            await interaction.followup.send(embed=create_success_embed(f"{target_user.display_name}は資産を保有していません。", title="残高"))
            return

        embed = Embed(title=f"{target_user.display_name}の保有資産", color=Color.green())
        for bal in balances:
            currency = await Rapid.Currencies.get(currency_id=bal.currency_id)
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
@app_commands.describe(user="送金先のユーザー", amount="送金する量", symbol="送金する通貨のシンボル (任意)")
async def transfer(interaction: discord.Interaction, user: User, amount: float, symbol: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    
    if amount <= 0:
        await interaction.followup.send(embed=create_error_embed("送金額は0より大きい必要があります。"))
        return

    try:
        currency = await _get_currency(interaction, symbol)
        if not currency:
            await interaction.followup.send(embed=create_error_embed("対象の通貨が見つかりませんでした。サーバー内で実行するか、シンボルを正しく指定してください。"))
            return

        int_amount = int(Decimal(str(amount)) * (10**Rapid.Config.decimal_places))

        tx = await Rapid.transfer(
            source_id=interaction.user.id,
            destination_id=user.id,
            currency_id=currency.currency_id,
            amount=int_amount
        )
        desc = f"{user.mention} へ `{format_amount(int_amount)} {currency.symbol}` の送金が完了しました。"
        fields = [EmbedField("転送ID", f"`{tx.transfer_id}`", False)]
        await interaction.followup.send(embed=create_success_embed(desc, title="送金完了", fields=fields))

    except exceptions.InsufficientFunds:
        await interaction.followup.send(embed=create_error_embed("残高が不足しています。"))
    except exceptions.TransactionError as e:
        await interaction.followup.send(embed=create_error_embed(f"取引の処理中にエラーが発生しました。\n`{e}`"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"予期せぬエラーが発生しました。\n`{e}`"))

@app_commands.command(name="execute_contract", description="指定したユーザーのコントラクトを実行します。")
@app_commands.describe(user="コントラクトの所有者", input_data="コントラクトに渡すデータ")
async def execute_contract(interaction: discord.Interaction, user: User, input_data: Optional[str] = None):
    await interaction.response.defer(thinking=True)

    if input_data and len(input_data) > 127:
        await interaction.followup.send(embed=create_error_embed("Input dataの長さは127文字以下である必要があります。"))
        return

    try:
        execution_id, output_data = await Rapid.execute_contract(
            caller_id=interaction.user.id,
            contract_owner_id=user.id,
            input_data=input_data,
        )
        desc = f"{user.mention} のコントラクトを実行しました。"
        fields = [EmbedField("実行ID", f"`{execution_id}`", False)]
        if output_data:
            fields.append(EmbedField("Output", f"```{output_data.replace('`', '')}```", False))
        await interaction.followup.send(embed=create_success_embed(desc, title="コントラクト実行完了", fields=fields))
    except exceptions.ContractError as e:
        await interaction.followup.send(embed=create_error_embed(f"コントラクトの処理中にエラーが発生しました。\n```{e}```"))
    except exceptions.TransactionError as e:
        await interaction.followup.send(embed=create_error_embed(f"取引の処理中にエラーが発生しました。\n`{e}`"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"予期せぬエラーが発生しました。\n`{e}`"))

@app_commands.command(name="history", description="転送履歴を表示します。")
@app_commands.describe(
    transfer_id="詳細を表示する転送ID (任意)",
    user="対象ユーザー",
    source="送金元ユーザー",
    destination="送金先ユーザー",
    currency_symbol="通貨シンボル",
    start_date="開始日 (YYYY-MM-DD)",
    end_date="終了日 (YYYY-MM-DD)",
    min_amount="最小金額",
    max_amount="最大金額",
    input_data="Input Data",
    page="ページ番号"
)
async def history(
    interaction: discord.Interaction,
    transfer_id: Optional[int] = None,
    user: Optional[User] = None,
    source: Optional[User] = None,
    destination: Optional[User] = None,
    currency_symbol: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    input_data: Optional[str] = None,
    page: int = 1
):
    await interaction.response.defer(thinking=True)
    try:
        if transfer_id:
            tx = await Rapid.Transfers.get(transfer_id)
            if not tx:
                await interaction.followup.send(embed=create_error_embed("指定された転送IDは見つかりませんでした。"))
                return

            currency = await Rapid.Currencies.get(tx.currency_id)
            source_user_mention = f"<@{tx.source_id}>" if tx.source_id != SYSTEM_USER_ID else "システム"
            dest_user_mention = f"<@{tx.dest_id}>" if tx.dest_id != SYSTEM_USER_ID else "システム"
            
            embed = Embed(title=f"転送詳細: ID {tx.transfer_id}", color=Color.blue())
            embed.add_field(name="日時", value=f"<t:{tx.timestamp}:F>", inline=False)
            embed.add_field(name="From", value=source_user_mention, inline=True)
            embed.add_field(name="To", value=dest_user_mention, inline=True)
            embed.add_field(name="金額", value=f"`{format_amount(tx.amount)} {currency.symbol if currency else '???'}`", inline=False)
            
            if tx.execution_id:
                execution = await Rapid.Executions.get(tx.execution_id)
                if execution and execution.input_data:
                    embed.add_field(name="メモ (Input Data)", value=f"```{execution.input_data.replace('`', '')}```", inline=False)

            await interaction.followup.send(embed=embed)
            return

        search_params = {
            "source_id": source.id if source else None,
            "dest_id": destination.id if destination else None,
            "input_data": input_data,
            "page": page
        }

        target_user = user or source or destination
        if not target_user:
            search_params["user_id"] = interaction.user.id
            target_user = interaction.user
        elif user:
            search_params["user_id"] = user.id

        if currency_symbol:
            currency = await Rapid.Currencies.get_by_symbol(currency_symbol.upper())
            search_params["currency_id"] = currency.currency_id if currency else -1

        def parse_date(date_str: str) -> Optional[int]:
            try:
                return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp())
            except ValueError:
                return None

        if start_date:
            search_params["start_timestamp"] = parse_date(start_date)
        if end_date:
            search_params["end_timestamp"] = parse_date(end_date)

        if min_amount is not None:
            search_params["min_amount"] = int(Decimal(str(min_amount)) * (10**Rapid.Config.decimal_places))
        if max_amount is not None:
            search_params["max_amount"] = int(Decimal(str(max_amount)) * (10**Rapid.Config.decimal_places))

        transfers = await Rapid.search_transfers(**search_params)

        target_user = user or source or destination or interaction.user

        if not transfers:
            await interaction.followup.send(embed=create_success_embed(f"指定された条件の転送履歴はありません。", "転送履歴"))
            return

        embed = Embed(title=f"転送履歴 (ページ {page})", color=Color.blue())
        for tx in transfers:
            currency = await Rapid.Currencies.get(tx.currency_id)
            if not currency: continue

            source_user_mention = f"<@{tx.source_id}>" if tx.source_id != SYSTEM_USER_ID else "システム"
            dest_user_mention = f"<@{tx.dest_id}>" if tx.dest_id != SYSTEM_USER_ID else "システム"
            
            direction_emoji = "↔️"
            direction_text = f"from {source_user_mention} to {dest_user_mention}"

            if target_user:
                if tx.source_id == target_user.id:
                    direction_emoji = "📤"
                    direction_text = f"to {dest_user_mention}"
                elif tx.dest_id == target_user.id:
                    direction_emoji = "📥"
                    direction_text = f"from {source_user_mention}"

            field_name = f"{direction_emoji} | ID: {tx.transfer_id} | <t:{tx.timestamp}:R>"
            field_value = f"`{format_amount(tx.amount)} {currency.symbol}` {direction_text}"
            embed.add_field(name=field_name, value=field_value, inline=False)
        
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"履歴の取得中にエラーが発生しました。\n`{e}`"))

currency_group = app_commands.Group(name="currency", description="通貨に関連するコマンド")

@currency_group.command(name="create", description="このサーバーに新しい通貨を発行します。")
@app_commands.describe(name="通貨の名前", symbol="通貨のシンボル", supply="初期供給量", hourly_interest_rate="ステーキングの利率(%/hour)")
async def currency_create(interaction: discord.Interaction, name: str, symbol: str, supply: float, hourly_interest_rate: float = 0.0):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    try:
        int_supply = int(Decimal(str(supply)) * (10**Rapid.Config.decimal_places))
        # Rate is percentage, so multiply by (INTEREST_RATE_SCALE / 100)
        rate_bps = int(Decimal(str(hourly_interest_rate)) * (INTEREST_RATE_SCALE // 100))
        
        new_currency, tx = await Rapid.create_currency(interaction.guild.id, name, symbol.upper(), int_supply, interaction.user.id, rate_bps)
        
        desc = f"新しい通貨 **{new_currency.name} ({new_currency.symbol})** が発行されました。"
        fields = [
            EmbedField("総供給量", f"`{format_amount(new_currency.supply)}`", False),
            EmbedField("ステーキングの利率", f"`{hourly_interest_rate:.4f}%/h`", False)
        ]
        if tx:
            fields.append(EmbedField("初期供給の転送ID", f"`{tx.transfer_id}`", False))

        await interaction.followup.send(embed=create_success_embed(desc, title="通貨発行成功", fields=fields))
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
    embed.add_field(name="ステーキング利率", value=f"`{Decimal(currency.hourly_interest_rate) / Decimal(INTEREST_RATE_SCALE // 100):.4f}%/h`", inline=False)
    if currency.new_hourly_interest_rate and currency.rate_change_requested_at:
        embed.add_field(name="次期ステーキング利率", value=f"`{Decimal(currency.new_hourly_interest_rate) / Decimal(INTEREST_RATE_SCALE // 100):.4f}%/h`", inline=False)
        embed.add_field(name="利率変更要求日時", value=f"<t:{currency.rate_change_requested_at}:F>", inline=True)

    embed.add_field(name="Mint/利率変更 放棄状態", value="はい" if currency.minting_renounced else "いいえ", inline=True)
    if currency.delete_requested_at:
        embed.add_field(name="削除要求日時", value=f"<t:{currency.delete_requested_at}:F>", inline=True)
    
    await interaction.followup.send(embed=embed)

@currency_group.command(name="mint", description="[管理者] 通貨を追加発行します。")
@app_commands.describe(amount="追加発行する量")
async def currency_mint(interaction: discord.Interaction, amount: float):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    
    currency = await Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return
    int_amount = int(Decimal(str(amount)) * (10**Rapid.Config.decimal_places))
    try:
        await Rapid.mint_currency(currency.currency_id, int_amount, interaction.user.id)
        await interaction.followup.send(embed=create_success_embed(f"`{format_amount(int_amount)} {currency.symbol}` を追加発行しました。", "Mint成功"))
    except exceptions.RenouncedError as e:
        await interaction.followup.send(embed=create_error_embed("この通貨のMint機能は放棄されています。"))
    except PermissionError as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))

@currency_group.command(name="burn", description="保有する通貨を焼却します。")
@app_commands.describe(amount="焼却する量")
async def currency_burn(interaction: discord.Interaction, amount: float):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return

    currency = await Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return

    int_amount = int(Decimal(str(amount)) * (10**Rapid.Config.decimal_places))
    try:
        await Rapid.burn_currency(currency.currency_id, int_amount, interaction.user.id)
        await interaction.followup.send(embed=create_success_embed(f"`{format_amount(int_amount)} {currency.symbol}` を焼却しました。", "Burn成功"))
    except exceptions.InsufficientFunds:
        await interaction.followup.send(embed=create_error_embed("焼却するための残高が不足しています。"))

@currency_group.command(name="renounce", description="[管理者] Mintと利率変更機能を永久に放棄します。")
async def currency_renounce(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return

    currency = await Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return
    
    try:
        await Rapid.renounce_currency(currency.currency_id, interaction.user.id)
        await interaction.followup.send(embed=create_success_embed(f"**{currency.symbol}** のMint機能と利率変更機能を永久に放棄しました。この操作は取り消せません。", "機能放棄"))
    except exceptions.RenouncedError as e:
        await interaction.followup.send(embed=create_error_embed("この通貨の機能は既に放棄されています。"))
    except PermissionError as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))

@currency_group.command(name="delete", description="[管理者] このサーバーの通貨を削除します。")
async def currency_delete(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return

    currency = await Rapid.Currencies.get(interaction.guild.id)
    if not currency:
        await interaction.followup.send(embed=create_error_embed("このサーバーには通貨が存在しません。"))
        return
    
    try:
        if not currency.delete_requested_at:
            await Rapid.request_delete_currency(currency.currency_id, interaction.user.id)
            await interaction.followup.send(embed=create_success_embed(
                f"通貨 **{currency.symbol}** の削除要請を受け付けました。\n"
                f"**7日後から10日後まで**の間に再度このコマンドを実行すると、削除が確定します。",
                "削除要請完了"
            ))
        else:
            txs = await Rapid.finalize_delete_currency(currency.currency_id, interaction.user.id)
            await interaction.followup.send(embed=create_success_embed(
                f"通貨 **{currency.symbol}** を完全に削除しました。\n"
                f"{len(txs)}件の残高焼却トランザクションが作成されました。",
                "通貨削除完了"
            ))
    except exceptions.TimeLockNotExpired:
        seven_days = 7 * 24 * 60 * 60
        await interaction.followup.send(embed=create_error_embed(
            f"削除の確定には、削除要請から7日間が経過している必要があります。\n"
            f"確定可能になる日時: <t:{currency.delete_requested_at + seven_days}:F>"
        ))
    except exceptions.RequestExpired:
        await Rapid.cancel_delete_request(currency.currency_id)
        await interaction.followup.send(embed=create_error_embed(
            "削除要請から10日以上が経過したため、この削除要請は無効になりました。\n"
            "再度削除を要請してください。"
        ))
    except PermissionError as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))

@currency_group.command(name="request-interest-change", description="[管理者] ステーキングの時利変更を予約します。")
@app_commands.describe(rate="新しい利率 (%/hour)")
async def currency_request_interest_change(interaction: discord.Interaction, rate: float):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    
    try:
        # Rate is percentage, so multiply by (INTEREST_RATE_SCALE / 100)
        new_rate_bps = int(Decimal(str(rate)) * (INTEREST_RATE_SCALE // 100))
        currency = await Rapid.request_interest_rate_change(interaction.guild.id, new_rate_bps, interaction.user.id)

        timelock_seconds = Rapid.Config.Staking.rate_change_timelock
        apply_time = int(time()) + timelock_seconds

        desc = (f"ステーキングの利率を`{rate:.4f}%/hour`に変更するリクエストを受け付けました。\n"
                f"この変更は <t:{apply_time}:F> (<t:{apply_time}:R>) 以降に適用可能になります。")
        await interaction.followup.send(embed=create_success_embed(desc, "利率変更予約完了"))
    except (ValueError, PermissionError, exceptions.CurrencyNotFound) as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"予期せぬエラーが発生しました: {e}"))

@currency_group.command(name="apply-interest-change", description="[管理者] 予約されている利率変更を適用します。")
async def currency_apply_interest_change(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return

    try:
        currency = await Rapid.apply_interest_rate_change(interaction.guild.id, interaction.user.id)
        desc = f"ステーキングの利率が `{Decimal(currency.hourly_interest_rate) / Decimal(INTEREST_RATE_SCALE // 100):.4f}%/hour` に正常に更新されました。"
        await interaction.followup.send(embed=create_success_embed(desc, "利率変更適用完了"))
    except (ValueError, PermissionError, exceptions.CurrencyNotFound) as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"予期せぬエラーが発生しました: {e}"))

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

        int_amount = int(Decimal(str(amount)) * (10**Rapid.Config.decimal_places))
        stake = await Rapid.stake_deposit(interaction.user.id, currency.currency_id, int_amount)
        desc = f"`{format_amount(int_amount)} {currency.symbol}` のステーキング（または追加預け入れ）が完了しました。\n現在の合計ステーク額は `{format_amount(stake.amount)} {currency.symbol}` です。"
        await interaction.followup.send(embed=create_success_embed(desc, "ステーキング完了"))
    except exceptions.InsufficientFunds:
        await interaction.followup.send(embed=create_error_embed("ステーキングするための残高が不足しています。"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"エラーが発生しました: {e}"))

@stake_group.command(name="withdraw", description="ステーキングした通貨の一部または全部を引き出します。")
@app_commands.describe(amount="引き出す量", symbol="引き出す通貨のシンボル (任意)")
async def stake_withdraw(interaction: discord.Interaction, amount: float, symbol: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    try:
        currency = await _get_currency(interaction, symbol)
        if not currency:
            await interaction.followup.send(embed=create_error_embed("対象の通貨が見つかりませんでした。"))
            return

        int_amount = int(Decimal(str(amount)) * (10**Rapid.Config.decimal_places))
        tx = await Rapid.stake_withdraw(interaction.user.id, currency.currency_id, int_amount)

        stake = await Rapid.Stakes.get(interaction.user.id, currency.currency_id)
        remaining_amount = stake.amount if stake else 0

        desc = f"`{format_amount(tx.amount)} {currency.symbol}` を引き出しました。\n"
        desc += f"残りのステーク額: `{format_amount(remaining_amount)} {currency.symbol}`"
        await interaction.followup.send(embed=create_success_embed(desc, "引き出し完了"))
    except (ValueError, PermissionError, exceptions.InsufficientFunds) as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"エラーが発生しました: {e}"))

@stake_group.command(name="info", description="あなたのステーキング状況を表示します。")
async def stake_info(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    
    stakes = await Rapid.Stakes.get_for_user(interaction.user.id)
    if not stakes:
        await interaction.followup.send(embed=create_success_embed("現在、アクティブなステークはありません。", "ステーク情報"))
        return
        
    embed = Embed(title=f"{interaction.user.display_name}のステーク情報", color=Color.purple())
    
    for stake in stakes:
        currency = await Rapid.Currencies.get(stake.currency_id)
        if not currency: continue

        field_name = f"通貨: **{currency.name} ({currency.symbol})**"
        field_value = (f"ステーク額: `{format_amount(stake.amount)}`\n"
                       f"現在の利率: `{Decimal(currency.hourly_interest_rate) / Decimal(INTEREST_RATE_SCALE // 100):.4f}%/h`\n"
                       f"最終更新日時: <t:{stake.last_updated_at}:F>")
        embed.add_field(name=field_name, value=field_value, inline=False)
        
    await interaction.followup.send(embed=embed)

approve_group = app_commands.Group(name="approve", description="他のユーザーにあなたの資産の使用を許可します。")

@approve_group.command(name="set", description="指定したユーザーに資産の使用を許可します。")
@app_commands.describe(user="許可を与えるユーザー", amount="許可する量", symbol="通貨のシンボル (任意)")
async def approve_set(interaction: discord.Interaction, user: User, amount: float, symbol: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    try:
        if amount < 0:
             await interaction.followup.send(embed=create_error_embed("許可額は0以上である必要があります。"))
             return

        currency = await _get_currency(interaction, symbol)
        if not currency:
            await interaction.followup.send(embed=create_error_embed("対象の通貨が見つかりませんでした。"))
            return

        int_amount = int(Decimal(str(amount)) * (10**Rapid.Config.decimal_places))
        await Rapid.approve(interaction.user.id, user.id, currency.currency_id, int_amount)

        desc = f"{user.mention} に `{format_amount(int_amount)} {currency.symbol}` の使用を許可しました。"
        await interaction.followup.send(embed=create_success_embed(desc, "承認完了"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"承認の設定中にエラーが発生しました: {e}"))

@approve_group.command(name="info", description="指定したユーザーへの許可状況を確認します。")
@app_commands.describe(user="確認するユーザー", symbol="通貨のシンボル (任意)")
async def approve_info(interaction: discord.Interaction, user: User, symbol: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    try:
        currency = await _get_currency(interaction, symbol)
        if not currency:
            await interaction.followup.send(embed=create_error_embed("対象の通貨が見つかりませんでした。"))
            return

        allowance = await Rapid.Allowances.get(interaction.user.id, user.id, currency.currency_id)
        current_amount = allowance.amount if allowance else 0

        desc = f"{user.mention} への現在の許可額: `{format_amount(current_amount)} {currency.symbol}`"
        await interaction.followup.send(embed=create_success_embed(desc, "許可情報"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"情報の取得中にエラーが発生しました: {e}"))

contract_group = app_commands.Group(name="contract", description="あなたのアカウントのコントラクトを管理します。")

@contract_group.command(name="set", description="あなたのアカウントにコントラクトを設定します。")
@app_commands.describe(
    script="コントラクトとして実行するPythonコードが書かれたファイル",
    max_cost="このコントラクトの実行を許可する最大コスト (0で無制限)",
    lock_hours="コントラクトの更新を禁止する期間（時間単位）。0でロックなし。"
)
async def contract_set(interaction: discord.Interaction, script: discord.Attachment, max_cost: Optional[int] = 0, lock_hours: Optional[int] = 0):
    await interaction.response.defer(thinking=True)
    try:
        script_content = (await script.read()).decode('utf-8')
        contract = await Rapid.set_contract(interaction.user.id, script_content, max_cost, lock_hours)

        fields = [
            EmbedField("計算されたコスト", f"`{contract.cost}`", False),
            EmbedField("設定された最大コスト", f"`{contract.max_cost}`" if contract.max_cost > 0 else "無制限", False)
        ]

        if contract.locked_until > time():
            fields.append(EmbedField("ロック期限", f"<t:{contract.locked_until}:F>", False))

        await interaction.followup.send(embed=create_success_embed("コントラクトを正常に設定しました。", fields=fields))
    except PermissionError as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"コントラクトの設定中にエラーが発生しました。\n`{e}`"))

@contract_group.command(name="get", description="現在設定されているコントラクトを取得します。")
@app_commands.describe(user="コントラクトを取得するユーザー (任意)")
async def contract_get(interaction: discord.Interaction, user: Optional[User] = None):
    await interaction.response.defer(thinking=True)
    target_user = user or interaction.user
    contract = await Rapid.Contracts.get(target_user.id)
    if contract and contract.script:
        script_hash = hashlib.sha256(contract.script.encode('utf-8')).hexdigest()
        file = File(io.BytesIO(contract.script.encode('utf-8')), filename=f"contract-{target_user.id}-{script_hash[:6]}.py")

        fields = [
            EmbedField("ユーザー", target_user.mention, False),
            EmbedField("計算されたコスト", f"`{contract.cost}`", False),
            EmbedField("設定された最大コスト", f"`{contract.max_cost}`" if contract.max_cost > 0 else "無制限", False)
        ]

        if contract.locked_until > time():
            fields.append(EmbedField("ロック期限", f"<t:{contract.locked_until}:F>", False))
        else:
             fields.append(EmbedField("ロック期限", "ロックされていません", False))

        await interaction.followup.send(file=file, embed=create_success_embed("", title="コントラクト詳細", fields=fields))
    else:
        msg = "現在、コントラクトは設定されていません。" if target_user.id == interaction.user.id else f"{target_user.mention} はコントラクトを設定していません。"
        await interaction.followup.send(embed=create_success_embed(msg, title="コントラクト情報"))

claim_group = app_commands.Group(name="claim", description="請求に関連するコマンド")

@claim_group.command(name="create", description="ユーザーに通貨を請求します。")
@app_commands.describe(user="請求先のユーザー", amount="請求額", description="請求の説明")
async def claim_create(interaction: discord.Interaction, user: User, amount: float, description: Optional[str] = None):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    try:
        currency = await Rapid.Currencies.get(currency_id=interaction.guild.id)
        if not currency:
            raise ValueError("このサーバーには通貨が存在しません。")
        
        int_amount = int(Decimal(str(amount)) * (10**Rapid.Config.decimal_places))
        claim = await Rapid.Claims.create(interaction.user.id, user.id, currency.currency_id, int_amount, description)
        
        desc = f"{user.mention} への請求を作成しました。"
        fields = [EmbedField("請求ID", f"`{claim.claim_id}`", False)]
        await interaction.followup.send(embed=create_success_embed(desc, "請求作成完了", fields=fields))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"請求の作成中にエラーが発生しました: {e}"))

@claim_group.command(name="list", description="あなたが関与する請求を一覧表示します。")
async def claim_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        claims = await Rapid.Claims.get_for_user(interaction.user.id)
        if not claims:
            await interaction.followup.send(embed=create_success_embed("関連する請求はありません。", "請求一覧"))
            return
        
        embed = Embed(title="請求一覧", color=Color.blue())
        for claim in claims:
            currency = await Rapid.Currencies.get(currency_id=claim.currency_id)
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
        tx = await Rapid.pay_claim(claim_id, interaction.user.id)
        desc = f"請求ID `{claim_id}` の支払いが完了しました。"
        fields = [EmbedField("転送ID", f"`{tx.transfer_id}`", False)]
        await interaction.followup.send(embed=create_success_embed(desc, "支払い完了", fields=fields))
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
        await Rapid.cancel_claim(claim_id, interaction.user.id)
        desc = f"請求ID `{claim_id}` をキャンセルしました。"
        await interaction.followup.send(embed=create_success_embed(desc, "キャンセル完了"))
    except (ValueError, PermissionError) as e:
        await interaction.followup.send(embed=create_error_embed(str(e)))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"キャンセル処理中にエラーが発生しました: {e}"))

async def _get_pool_info_embed(pool: structs.LiquidityPool) -> Embed:
    currency_a = await Rapid.Currencies.get(pool.currency_a_id)
    currency_b = await Rapid.Currencies.get(pool.currency_b_id)
    embed = Embed(title=f"流動性プール情報: {currency_a.symbol}-{currency_b.symbol}", color=Color.purple())
    embed.add_field(name="プールID", value=f"`{pool.pool_id}`", inline=False)
    embed.add_field(name=f"{currency_a.symbol} リザーブ", value=f"`{format_amount(pool.reserve_a)}`", inline=True)
    embed.add_field(name=f"{currency_b.symbol} リザーブ", value=f"`{format_amount(pool.reserve_b)}`", inline=True)
    embed.add_field(name="合計シェア", value=f"`{format_amount(pool.total_shares)}`", inline=False)
    return embed

lp_group = app_commands.Group(name="lp", description="流動性プールに関連するコマンド")

@lp_group.command(name="create", description="新しい流動性プールを作成します。")
@app_commands.describe(symbol_a="通貨Aのシンボル", amount_a="通貨Aの量", symbol_b="通貨Bのシンボル", amount_b="通貨Bの量")
async def lp_create(interaction: discord.Interaction, symbol_a: str, amount_a: float, symbol_b: str, amount_b: float):
    await interaction.response.defer(thinking=True)
    try:
        currency_a = await Rapid.Currencies.get_by_symbol(symbol_a.upper())
        currency_b = await Rapid.Currencies.get_by_symbol(symbol_b.upper())
        if not currency_a or not currency_b:
            await interaction.followup.send(embed=create_error_embed("指定された通貨が見つかりませんでした。"))
            return

        int_amount_a = int(Decimal(str(amount_a)) * (10**Rapid.Config.decimal_places))
        int_amount_b = int(Decimal(str(amount_b)) * (10**Rapid.Config.decimal_places))

        pool = await Rapid.create_liquidity_pool(currency_a.currency_id, currency_b.currency_id, int_amount_a, int_amount_b, interaction.user.id)
        embed = await _get_pool_info_embed(pool)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"プールの作成中にエラーが発生しました: {e}"))

@lp_group.command(name="add", description="流動性プールに流動性を追加します。")
@app_commands.describe(symbol_a="通貨Aのシンボル", amount_a="通貨Aの量", symbol_b="通貨Bのシンボル", amount_b="通貨Bの量")
async def lp_add(interaction: discord.Interaction, symbol_a: str, amount_a: float, symbol_b: str, amount_b: float):
    await interaction.response.defer(thinking=True)
    try:
        int_amount_a = int(Decimal(str(amount_a)) * (10**Rapid.Config.decimal_places))
        int_amount_b = int(Decimal(str(amount_b)) * (10**Rapid.Config.decimal_places))

        shares = await Rapid.add_liquidity(symbol_a.upper(), symbol_b.upper(), int_amount_a, int_amount_b, interaction.user.id)
        desc = f"`{format_amount(shares)}` シェアを受け取りました。"
        await interaction.followup.send(embed=create_success_embed(desc, "流動性追加完了"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"流動性の追加中にエラーが発生しました: {e}"))

@lp_group.command(name="remove", description="流動性プールから流動性を削除します。")
@app_commands.describe(symbol_a="通貨Aのシンボル", symbol_b="通貨Bのシンボル", shares="削除するシェアの量")
async def lp_remove(interaction: discord.Interaction, symbol_a: str, symbol_b: str, shares: float):
    await interaction.response.defer(thinking=True)
    try:
        int_shares = int(Decimal(str(shares)) * (10**Rapid.Config.decimal_places))
        amount_a, amount_b = await Rapid.remove_liquidity(symbol_a.upper(), symbol_b.upper(), int_shares, interaction.user.id)

        currency_a = await Rapid.Currencies.get_by_symbol(symbol_a.upper())
        currency_b = await Rapid.Currencies.get_by_symbol(symbol_b.upper())

        desc = f"`{format_amount(amount_a)} {currency_a.symbol}` と `{format_amount(amount_b)} {currency_b.symbol}` を受け取りました。"
        await interaction.followup.send(embed=create_success_embed(desc, "流動性削除完了"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"流動性の削除中にエラーが発生しました: {e}"))

@lp_group.command(name="info", description="流動性プールの情報を表示します。")
@app_commands.describe(symbol_a="通貨Aのシンボル", symbol_b="通貨Bのシンボル")
async def lp_info(interaction: discord.Interaction, symbol_a: str, symbol_b: str):
    await interaction.response.defer(thinking=True)
    pool = await Rapid.LiquidityPools.get_by_symbols(symbol_a.upper(), symbol_b.upper())
    if not pool:
        await interaction.followup.send(embed=create_error_embed("指定された通貨ペアのプールは見つかりませんでした。"))
        return
    embed = await _get_pool_info_embed(pool)
    await interaction.followup.send(embed=embed)

@app_commands.command(name="swap", description="通貨をスワップします。")
@app_commands.describe(from_symbol="スワップ元の通貨シンボル", to_symbol="スワップ先の通貨シンボル", amount="スワップする量")
async def swap(interaction: discord.Interaction, from_symbol: str, to_symbol: str, amount: float):
    try:
        from_symbol_upper = from_symbol.upper()
        to_symbol_upper = to_symbol.upper()

        from_currency = await Rapid.Currencies.get_by_symbol(from_symbol_upper)
        to_currency = await Rapid.Currencies.get_by_symbol(to_symbol_upper)
        if not from_currency or not to_currency:
            await interaction.response.send_message(embed=create_error_embed("指定された通貨が見つかりませんでした。"), ephemeral=True)
            return

        int_amount = int(Decimal(str(amount)) * (10**Rapid.Config.decimal_places))

        try:
            route = await Rapid.find_swap_route(from_symbol_upper, to_symbol_upper)
        except ValueError:
            await interaction.response.send_message(embed=create_error_embed("スワップルートが見つかりませんでした。"), ephemeral=True)
            return

        amount_out_est = Rapid.get_swap_rate(int_amount, route, from_currency.currency_id)
        if amount_out_est <= 0:
            await interaction.response.send_message(embed=create_error_embed("スワップで得られる通貨量が0以下です。"), ephemeral=True)
            return

        route_symbols = [from_symbol_upper]
        current_currency_id = from_currency.currency_id
        for pool in route:
            if pool.currency_a_id == current_currency_id:
                next_currency_id = pool.currency_b_id
            else:
                next_currency_id = pool.currency_a_id

            next_currency = await Rapid.Currencies.get(next_currency_id)
            route_symbols.append(next_currency.symbol)
            current_currency_id = next_currency_id

        route_str = " -> ".join(route_symbols)

        embed = Embed(title="スワップ確認", color=Color.blue())
        embed.add_field(name="スワップ元", value=f"`{format_amount(int_amount)} {from_currency.symbol}`", inline=False)
        embed.add_field(name="スワップ先 (推定)", value=f"`{format_amount(amount_out_est)} {to_currency.symbol}`", inline=False)
        embed.add_field(name="ルート", value=f"`{route_str}`", inline=False)
        embed.set_footer(text="このレートは30秒間有効です。")

        view = SwapConfirmationView(interaction.user, from_symbol_upper, to_symbol_upper, int_amount, amount_out_est)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        await view.wait()

        if not view.swap_executed:
            if not interaction.is_expired():
                await interaction.edit_original_response(embed=create_error_embed("スワップがタイムアウトしました。"), view=None)

    except Exception as e:
        if not interaction.is_expired():
            await interaction.followup.send(embed=create_error_embed(f"スワップ中にエラーが発生しました: {e}"))

notification_group = app_commands.Group(name="notification", description="請求通知に関わるコマンド")

@notification_group.command(name="allow", description="指定したユーザーからの請求通知を許可します。")
@app_commands.describe(user="許可するユーザー")
async def notification_allow(interaction: discord.Interaction, user: User):
    await interaction.response.defer(thinking=True)
    try:
        await Rapid.NotificationPermissions.add(interaction.user.id, user.id)
        await interaction.followup.send(embed=create_success_embed(f"{user.mention} からに請求が来た際にDMで通知を送ります。"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"エラーが発生しました: {e}"))

@notification_group.command(name="deny", description="指定したユーザーからの請求通知を拒否します。")
@app_commands.describe(user="拒否するユーザー")
async def notification_deny(interaction: discord.Interaction, user: User):
    await interaction.response.defer(thinking=True)
    try:
        await Rapid.NotificationPermissions.remove(interaction.user.id, user.id)
        await interaction.followup.send(embed=create_success_embed(f"{user.mention} からの請求通知をオフにしました。"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"エラーが発生しました: {e}"))

@notification_group.command(name="list", description="請求通知を許可しているユーザーの一覧を表示します。")
async def notification_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        permissions = await Rapid.NotificationPermissions.get_for_user(interaction.user.id)
        if not permissions:
            await interaction.followup.send(embed=create_success_embed("現在、請求通知を許可しているユーザーはいません。"))
            return

        embed = Embed(title="請求通知許可ユーザー一覧", color=Color.blue())
        user_mentions = []
        for p in permissions:
            try:
                user = await interaction.client.fetch_user(p.allowed_user_id)
                user_mentions.append(user.mention)
            except discord.NotFound:
                user_mentions.append(f"`{p.allowed_user_id}` (不明なユーザー)")

        embed.description = "\n".join(user_mentions)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"一覧の取得中にエラーが発生しました: {e}"))

# --- Discord Permission Commands ---

discord_permission_group = app_commands.Group(name="discord-permission", description="[管理者] コントラクトによるDiscord操作権限を管理します。")

@discord_permission_group.command(name="allow", description="指定したユーザーのコントラクトにDiscord操作を許可します。")
@app_commands.describe(user="許可するユーザー")
@app_commands.checks.has_permissions(administrator=True)
async def discord_permission_allow(interaction: discord.Interaction, user: User):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    try:
        await Rapid.DiscordPermissions.add(interaction.guild.id, user.id)
        await interaction.followup.send(embed=create_success_embed(f"{user.mention} のコントラクトによるDiscord操作を許可しました。", "権限付与成功"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"エラーが発生しました: {e}"))

@discord_permission_group.command(name="deny", description="指定したユーザーのコントラクトによるDiscord操作を禁止します。")
@app_commands.describe(user="禁止するユーザー")
@app_commands.checks.has_permissions(administrator=True)
async def discord_permission_deny(interaction: discord.Interaction, user: User):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    try:
        await Rapid.DiscordPermissions.remove(interaction.guild.id, user.id)
        await interaction.followup.send(embed=create_success_embed(f"{user.mention} のコントラクトによるDiscord操作を禁止しました。", "権限剥奪成功"))
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"エラーが発生しました: {e}"))

@discord_permission_group.command(name="list", description="Discord操作を許可されているユーザーの一覧を表示します。")
@app_commands.checks.has_permissions(administrator=True)
async def discord_permission_list(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    if not interaction.guild: return
    try:
        permissions = await Rapid.DiscordPermissions.get_all(interaction.guild.id)
        if not permissions:
            await interaction.followup.send(embed=create_success_embed("現在、Discord操作を許可されているユーザーはいません。", "権限一覧"))
            return

        embed = Embed(title="Discord操作許可ユーザー一覧", color=Color.blue())
        user_mentions = []
        for p in permissions:
            try:
                user = await interaction.client.fetch_user(p.user_id)
                user_mentions.append(user.mention)
            except discord.NotFound:
                user_mentions.append(f"`{p.user_id}` (不明なユーザー)")

        embed.description = "\n".join(user_mentions)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(embed=create_error_embed(f"一覧の取得中にエラーが発生しました: {e}"))

def setup(tree: app_commands.CommandTree, rapid: RapidWire):
    global Rapid
    Rapid = rapid

    tree.add_command(balance)
    tree.add_command(transfer)
    tree.add_command(execute_contract)
    tree.add_command(history)
    tree.add_command(currency_group)
    tree.add_command(stake_group)
    tree.add_command(approve_group)
    tree.add_command(contract_group)
    tree.add_command(claim_group)
    tree.add_command(lp_group)
    tree.add_command(swap)
    tree.add_command(notification_group)
    tree.add_command(discord_permission_group)
