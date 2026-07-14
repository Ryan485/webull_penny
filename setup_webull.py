"""
One-time Webull device verification.
Run this ONCE before setting BROKER=webull in .env.
Saves your device ID so the bot can log in without MFA each time.

Usage:
    py -3.12 setup_webull.py
"""
import os


def main():
    print("=" * 60)
    print("  Webull One-Time Device Setup")
    print("=" * 60)
    print()
    print("You can log in with your phone number OR email.")
    print("For Canadian accounts, use: +1XXXXXXXXXX format")
    print("Example: +16041234567")
    print()

    try:
        from webull import webull
    except ImportError:
        print("ERROR: webull package not installed. Run: pip install webull")
        return

    username = input("Phone number or email: ").strip()
    password = input("Password: ").strip()

    wb = webull()

    print("\nSending verification code...")
    try:
        wb.get_mfa(username)
        print("Check your SMS or email for a 6-digit code.")
    except Exception as e:
        print(f"Could not send MFA: {e}")
        print("Trying login without MFA code...")

    mfa_code = input("Enter the 6-digit code (or press Enter to skip): ").strip()

    try:
        if mfa_code:
            result = wb.login(username, password, mfa=mfa_code)
        else:
            result = wb.login(username, password)

        if "accessToken" not in str(result):
            print(f"\nLogin may have failed. Response: {result}")
            print("Try running again with the correct credentials.")
            return

        print("\nLogin successful!")
    except Exception as e:
        print(f"\nLogin error: {e}")
        return

    # Save device ID
    device_id = wb.get_did()
    print(f"Device ID: {device_id}")

    # Test trading PIN
    pin = input("\nWebull 6-digit trading PIN: ").strip()
    try:
        wb.get_trade_token(pin)
        print("Trading PIN OK.")
    except Exception as e:
        print(f"Trading PIN error: {e}")
        print("You can still set it in .env and try again.")

    # Write to .env
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()

    fields = {
        "WEBULL_DEVICE_ID": device_id,
        "WEBULL_EMAIL": username,
        "WEBULL_PASSWORD": password,
        "WEBULL_TRADING_PIN": pin,
    }

    for key, val in fields.items():
        updated = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={val}\n"
                updated = True
                break
        if not updated:
            lines.append(f"{key}={val}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)

    print("\nAll credentials saved to .env")
    print("\nNext steps:")
    print("  1. Open .env and set BROKER=webull")
    print("  2. Run: start.bat")


if __name__ == "__main__":
    main()
