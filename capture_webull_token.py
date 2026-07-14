"""
Webull token capture -- opens a browser, you log in, token saved automatically.

Usage:
    py -3.12 capture_webull_token.py
"""
import asyncio
import os
import sys

# Fix encoding for Korean/non-UTF8 Windows terminals
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CAPTURED = {}


async def run():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        print("Opening browser...")

        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ],
            ignore_default_args=["--enable-automation"],
        )

        context = await browser.new_context(
            viewport=None,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        # Watch ALL responses from webull domains and print what we see
        async def on_response(response):
            if CAPTURED.get("done"):
                return
            url = response.url
            if "webull" not in url:
                return
            try:
                body = await response.json()
            except Exception:
                return
            if not isinstance(body, dict):
                return

            # Show what we're receiving so we can debug
            keys = list(body.keys())[:8]
            print(f"  [API] {url.split('webull')[-1][:50]}  keys={keys}")

            # Check everywhere a token might live
            access = (
                body.get("accessToken")
                or body.get("access_token")
                or body.get("token")
                or (body.get("data") or {}).get("accessToken")
                or (body.get("data") or {}).get("access_token")
            )
            refresh = (
                body.get("refreshToken")
                or body.get("refresh_token")
                or (body.get("data") or {}).get("refreshToken")
                or ""
            )

            if access and len(str(access)) > 20:
                did = response.request.headers.get("did", "")
                CAPTURED.update({
                    "access_token": str(access),
                    "refresh_token": str(refresh) if refresh else "",
                    "did": did,
                    "done": True,
                })
                print("\n" + "=" * 55)
                print("  Token captured!")
                print(f"  {str(access)[:40]}...")
                print("=" * 55 + "\n")

        # Also watch request headers
        def on_request(request):
            if CAPTURED.get("done"):
                return
            token = request.headers.get("access_token", "")
            did   = request.headers.get("did", "")
            if token and len(token) > 20:
                CAPTURED.update({
                    "access_token": token,
                    "did": did,
                    "done": True,
                })
                print(f"\nToken captured from request header: {token[:30]}...")

        page.on("response", on_response)
        page.on("request", on_request)

        await page.goto("about:blank")
        await page.evaluate("""() => {
            document.body.style.cssText = 'background:#111;margin:0;font-family:sans-serif';
            document.body.innerHTML =
                '<div style="color:#fff;text-align:center;margin-top:180px">'
                + '<h2 style="color:#f0a500">Webull Token Capture</h2>'
                + '<p>Go to <a href="https://app.webull.com" target="_self" '
                + 'style="color:#00bc8c;font-size:20px">app.webull.com</a> and log in.</p>'
                + '<p style="color:#888;font-size:14px">'
                + 'This window closes automatically once your token is captured.</p>'
                + '</div>';
        }""")

        print("Go to app.webull.com in the browser and log in.")
        print("Watching Webull API responses...\n")

        for _ in range(300):
            if CAPTURED.get("done"):
                break
            await asyncio.sleep(1)

        if not CAPTURED.get("done"):
            print("Timed out -- no token captured.")

        try:
            await browser.close()
        except Exception:
            pass


def save_to_env(data: dict, pin: str = "") -> None:
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()

    updates = {
        "WEBULL_ACCESS_TOKEN":  data.get("access_token", ""),
        "WEBULL_REFRESH_TOKEN": data.get("refresh_token", ""),
        "WEBULL_UUID":          data.get("did", ""),
        "WEBULL_DEVICE_ID":     data.get("did", ""),
    }
    if pin:
        updates["WEBULL_TRADING_PIN"] = pin

    for key, val in updates.items():
        if not val:
            continue
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={val}\n"
                found = True
                break
        if not found:
            lines.append(f"{key}={val}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def main():
    print("=" * 55)
    print("  Webull Token Capture")
    print("=" * 55)
    print()

    asyncio.run(run())

    if not CAPTURED.get("access_token"):
        print("No token captured.")
        print("Paste the lines above and share them -- we can figure out the right key.")
        return

    print("Verifying with Webull API...")
    try:
        from webull import webull
        wb = webull()
        if CAPTURED.get("did"):
            wb._set_did(CAPTURED["did"])
        wb.api_login(
            access_token=CAPTURED["access_token"],
            refresh_token=CAPTURED.get("refresh_token", ""),
            uuid=CAPTURED.get("did", ""),
        )
        acct = wb.get_account()
        if acct and isinstance(acct, dict) and acct.get("netLiquidation"):
            print(f"Account value: ${acct['netLiquidation']}")
        else:
            print("Connected to Webull.")
    except Exception as e:
        print(f"Note: {e}")

    # PIN not needed for paper trading
    pin = input("\nTrading PIN (press Enter to skip -- not needed for paper trading): ").strip()
    save_to_env(CAPTURED, pin)

    # Switch .env to webull_paper
    env_path = ".env"
    with open(env_path, encoding="utf-8") as f:
        content = f.read()
    content = content.replace("BROKER=alpaca", "BROKER=webull_paper")
    content = content.replace("BROKER=paper", "BROKER=webull_paper")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)

    print("\nDone! BROKER set to webull_paper in .env")
    print("Run start.bat to launch the bot.")


if __name__ == "__main__":
    main()
