import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import discord
from decimal import Decimal
import sys

# Mock config
sys.modules['config'] = MagicMock()
sys.modules['config'].MySQL = MagicMock()
sys.modules['config'].MySQL.to_dict.return_value = {}
sys.modules['config'].RapidWireConfig = MagicMock()
sys.modules['config'].RapidWireConfig.decimal_places = 2

# Mock RapidWire
with patch('RapidWire.RapidWire') as MockRapidWire:
    import bot_commands
    # Access the callback functions directly
    # Because app_commands.command decorator wraps the function in a Command object
    approve_set_callback = bot_commands.approve_set.callback
    approve_info_callback = bot_commands.approve_info.callback

class TestApproveCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.interaction = AsyncMock(spec=discord.Interaction)
        self.interaction.user.id = 12345
        self.interaction.user.display_name = "TestUser"
        self.interaction.response.defer = AsyncMock()
        self.interaction.followup.send = AsyncMock()

        self.target_user = MagicMock(spec=discord.User)
        self.target_user.id = 67890
        self.target_user.mention = "<@67890>"

        # Reset Rapid mock
        bot_commands.Rapid.reset_mock()
        bot_commands.Rapid.Config.decimal_places = 2

        # Setup Currency mock
        self.currency = MagicMock()
        self.currency.currency_id = 1
        self.currency.symbol = "TEST"
        self.currency.name = "Test Currency"
        bot_commands.Rapid.Currencies.get_by_symbol.return_value = self.currency
        bot_commands.Rapid.Currencies.get.return_value = self.currency

    async def test_approve_set(self):
        # Arrange
        amount = 100.0
        expected_int_amount = 10000 # 100 * 10^2

        # Act
        await approve_set_callback(self.interaction, self.target_user, amount, "TEST")

        # Assert
        bot_commands.Rapid.approve.assert_called_once_with(12345, 67890, 1, expected_int_amount)
        self.interaction.followup.send.assert_called_once()
        args, kwargs = self.interaction.followup.send.call_args
        embed = kwargs['embed']
        self.assertEqual(embed.title, "承認完了")
        self.assertIn("100.00 TEST", embed.description)

    async def test_approve_set_negative(self):
        # Act
        await approve_set_callback(self.interaction, self.target_user, -10.0, "TEST")

        # Assert
        bot_commands.Rapid.approve.assert_not_called()
        self.interaction.followup.send.assert_called_once()
        args, kwargs = self.interaction.followup.send.call_args
        embed = kwargs['embed']
        self.assertEqual(embed.title, "エラー")
        self.assertIn("許可額は0以上", embed.description)

    async def test_approve_info(self):
        # Arrange
        mock_allowance = MagicMock()
        mock_allowance.amount = 5000 # 50.00
        bot_commands.Rapid.Allowances.get.return_value = mock_allowance

        # Act
        await approve_info_callback(self.interaction, self.target_user, "TEST")

        # Assert
        bot_commands.Rapid.Allowances.get.assert_called_once_with(12345, 67890, 1)
        self.interaction.followup.send.assert_called_once()
        args, kwargs = self.interaction.followup.send.call_args
        embed = kwargs['embed']
        self.assertEqual(embed.title, "許可情報")
        self.assertIn("50.00 TEST", embed.description)

    async def test_approve_info_no_allowance(self):
        # Arrange
        bot_commands.Rapid.Allowances.get.return_value = None

        # Act
        await approve_info_callback(self.interaction, self.target_user, "TEST")

        # Assert
        bot_commands.Rapid.Allowances.get.assert_called_once_with(12345, 67890, 1)
        self.interaction.followup.send.assert_called_once()
        args, kwargs = self.interaction.followup.send.call_args
        embed = kwargs['embed']
        self.assertEqual(embed.title, "許可情報")
        self.assertIn("0.00 TEST", embed.description)

if __name__ == '__main__':
    unittest.main()
