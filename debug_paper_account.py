"""Diagnose which paper account the bot is actually connecting to."""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import os
from dotenv import load_dotenv
load_dotenv()

ACCESS_TOKEN  = os.getenv("WEBULL_ACCESS_TOKEN", "")
REFRESH_TOKEN = os.getenv("WEBULL_REFRESH_TOKEN", "")
DEVICE_ID     = os.getenv("WEBULL_DEVICE_ID", "")
UUID          = os.getenv("WEBULL_UUID", "")

print(f"Access token: {ACCESS_TOKEN[:30]}...")
print(f"Device ID: {DEVICE_ID}")
print()

from webull import paper_webull, webull

# --- Test paper_webull ---
print("=== paper_webull ===")
wb = paper_webull()
if DEVICE_ID:
    wb._set_did(DEVICE_ID)
wb.api_login(
    access_token=ACCESS_TOKEN,
    refresh_token=REFRESH_TOKEN,
    token_expire="",
    uuid=UUID,
)

try:
    acct = wb.get_account()
    print(f"get_account() keys: {list(acct.keys()) if acct else 'None'}")
    print(f"netLiquidation: {acct.get('netLiquidation')}")
    print(f"accountId: {acct.get('accountId') or acct.get('id')}")
    print(f"accountType: {acct.get('accountType') or acct.get('type')}")
    print(f"Full response (truncated): {str(acct)[:500]}")
except Exception as e:
    print(f"get_account() error: {e}")

print()
try:
    acct_id = wb.get_account_id()
    print(f"get_account_id(): {acct_id}")
except Exception as e:
    print(f"get_account_id() error: {e}")

print()
try:
    positions = wb.get_positions()
    print(f"get_positions(): {positions}")
except Exception as e:
    print(f"get_positions() error: {e}")

print()
try:
    orders = wb.get_history_orders(status="All", count=5)
    print(f"get_history_orders() (last 5):")
    if isinstance(orders, list):
        for o in orders[:5]:
            print(f"  {o.get('ticker',{}).get('symbol')} {o.get('action')} {o.get('totalQuantity')} @ {o.get('avgFilledPrice')} status={o.get('statusStr')}")
    else:
        print(f"  Response: {str(orders)[:300]}")
except Exception as e:
    print(f"get_history_orders() error: {e}")
