import asyncio
import sys
import logging
import random
from client import RapidWireClient, Currency, Balance, RapidWireAPIError, Contract

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Config
BASE_URL = "http://localhost:8000"
API_KEY = "test_api_key"

USER_ID = 123456789
DEST_USER_ID = 987654321
SPENDER_ID = 111222333

CURRENCY_1 = 1
CURRENCY_2 = 2

def main():
    logger.info("Starting Comprehensive RapidWire Client investigation...")

    with RapidWireClient(base_url=BASE_URL, api_key=API_KEY) as client:
        try:
            # 1. Info & Config
            logger.info("--- Info & Config ---")
            version = client.get_version()
            logger.info(f"Version: {version.message}")
            config = client.get_config()
            logger.info(f"Config: {config.model_dump_json()}")

            # 2. User Info
            logger.info("--- User Info ---")
            try:
                username = client.get_user_name(USER_ID)
                logger.info(f"Username: {username.username}")
            except RapidWireAPIError as e:
                logger.warning(f"Get username failed (expected): {e}")

            stats = client.get_user_stats(USER_ID)
            logger.info(f"User stats: {stats.model_dump_json()}")

            # 3. Currency
            logger.info("--- Currency ---")
            c1 = client.get_currency(CURRENCY_1)
            logger.info(f"Currency 1: {c1.name}")
            c2 = client.get_currency(CURRENCY_2)
            logger.info(f"Currency 2: {c2.name}")

            c_sym = client.get_currency_by_symbol("TEST")
            logger.info(f"Currency by symbol TEST: {c_sym.currency_id}")

            # 4. Balances
            logger.info("--- Balances ---")
            balances = client.get_balance(USER_ID)
            for b in balances:
                logger.info(f"Balance {b.currency.symbol}: {b.amount}")

            # 5. Transfers
            logger.info("--- Transfers ---")
            # Transfer Currency 1 to DEST_USER_ID
            logger.info(f"Transferring 100 {c1.symbol} to {DEST_USER_ID}...")
            tx_resp = client.transfer_currency(DEST_USER_ID, CURRENCY_1, 100)
            logger.info(f"Transfer ID: {tx_resp.transfer.transfer_id if tx_resp.transfer else 'None'}")

            # Verify balance decrease
            balances = client.get_balance(USER_ID)
            # Find currency 1 balance
            for b in balances:
                if b.currency.currency_id == CURRENCY_1:
                    logger.info(f"New Balance {b.currency.symbol}: {b.amount}")

            # Search Transfers
            transfers = client.search_transfers(user_id=USER_ID, limit=5)
            logger.info(f"Recent transfers: {len(transfers)}")
            if transfers:
                tx_details = client.get_transfer(transfers[0].transfer_id)
                logger.info(f"Transfer details for {tx_details.transfer_id}: Amount {tx_details.amount}")

            # 6. Allowances & Transfer From
            logger.info("--- Allowances & Transfer From ---")
            # Approve allowance for SPENDER_ID on Currency 1
            logger.info(f"Approving 50 {c1.symbol} for spender {SPENDER_ID}...")
            client.approve_allowance(SPENDER_ID, CURRENCY_1, 50)

            allowance = client.get_allowance(USER_ID, SPENDER_ID, CURRENCY_1)
            logger.info(f"Allowance: {allowance.amount}")

            # To test transfer_from, we need to act as SPENDER_ID.
            # But our API key is tied to USER_ID (123456789).
            # So we can't easily test transfer_from with this API key unless we create another API key for SPENDER_ID.
            # We will skip execution of transfer_from but we called approve/get_allowance.

            # 7. Claims
            logger.info("--- Claims ---")
            # Create claim
            logger.info("Creating claim...")
            claim = client.create_claim(payer_id=DEST_USER_ID, currency_id=CURRENCY_1, amount=10, description="Test Claim")
            logger.info(f"Claim Created: ID {claim.claim_id}, Status {claim.status}")

            claims = client.get_claims()
            logger.info(f"My Claims: {len(claims)}")

            # Get claim details
            claim_details = client.get_claim(claim.claim_id)
            logger.info(f"Claim Details: {claim_details.description}")

            # Cancel claim
            logger.info("Cancelling claim...")
            cancelled_claim = client.cancel_claim(claim.claim_id)
            logger.info(f"Claim Status: {cancelled_claim.status}")

            # 8. DEX / Pools
            logger.info("--- DEX / Pools ---")
            # Add Liquidity (C1 and C2)
            # 1000 C1 + 1000 C2
            logger.info(f"Adding Liquidity: 1000 {c1.symbol} + 1000 {c2.symbol}...")
            lp_resp = client.add_liquidity(CURRENCY_1, CURRENCY_2, 1000, 1000)
            logger.info(f"LP Shares Minted: {lp_resp.shares_minted}")

            pools = client.get_pools()
            logger.info(f"Pools count: {len(pools)}")

            pool = client.get_pool(CURRENCY_1, CURRENCY_2)
            logger.info(f"Pool Reserves: {pool.reserve_a} / {pool.reserve_b}, Total Shares: {pool.total_shares}")

            provider_info = client.get_provider_info(USER_ID)
            logger.info(f"My Provider Info: {len(provider_info)} entries")

            # Swap Rate
            # Swap 100 C1 -> C2
            logger.info("Checking swap rate for 100 C1 -> C2...")
            rate_resp = client.get_swap_rate(CURRENCY_1, CURRENCY_2, 100)
            logger.info(f"Estimated Output: {rate_resp.amount_out}")

            # Swap Route
            route_resp = client.get_swap_route(CURRENCY_1, CURRENCY_2)
            logger.info(f"Route hops: {len(route_resp.route)}")

            # Execute Swap
            logger.info("Executing Swap 100 C1 -> C2...")
            swap_resp = client.swap(CURRENCY_1, CURRENCY_2, 100)
            logger.info(f"Swap Executed. Out: {swap_resp.amount_out}, Exec ID: {swap_resp.execution_id}")

            # Remove Liquidity
            # Remove 10 shares
            logger.info("Removing Liquidity (10 shares)...")
            remove_resp = client.remove_liquidity(CURRENCY_1, CURRENCY_2, 10)
            logger.info(f"Received: {remove_resp.amount_a_received} {c1.symbol}, {remove_resp.amount_b_received} {c2.symbol}")

            # 9. Contracts
            logger.info("--- Contracts ---")
            # Update Contract
            # Simple script: output "Hello"
            script = '[{"op": "output", "args": ["Hello"]}]'
            logger.info("Setting contract...")
            contract_resp = client.update_contract(script=script)
            logger.info(f"Contract set. Cost: {contract_resp.contract.cost}")

            script_resp = client.get_contract_script(USER_ID)
            logger.info(f"Retrieved script: {script_resp.script[:20]}...")

            # Execute Contract
            logger.info("Executing contract...")
            exec_resp = client.execute_contract(contract_owner_id=USER_ID, input_data="test")
            logger.info(f"Execution Output: {exec_resp.output_data}, Exec ID: {exec_resp.execution_id}")

            # Contract History
            history = client.get_contract_history(USER_ID)
            logger.info(f"Contract History: {len(history)} entries")

            # Contract Variables (none set by script, but we can check empty)
            variables = client.get_contract_variables(USER_ID)
            logger.info(f"Contract Variables: {len(variables)}")

            # 10. Stakes
            logger.info("--- Stakes ---")
            stakes = client.get_stakes(USER_ID)
            logger.info(f"Stakes: {len(stakes)}")

        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}", exc_info=True)

if __name__ == "__main__":
    main()
