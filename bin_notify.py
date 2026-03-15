import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LOOKUP_URL = "https://swict.malvernhills.gov.uk/mhdcroundlookup/HandleSearchScreen"
TIMEZONE = "Europe/London"

POSTCODE = os.environ["BIN_POSTCODE"].strip()
HOUSE_NUMBER = os.environ["BIN_HOUSE_NUMBER"].strip()

PUSHOVER_TOKEN = os.environ["PUSHOVER_TOKEN"].strip()
PUSHOVER_USER_OR_GROUP = os.environ["PUSHOVER_USERS"].strip()

# Set to "true" in workflow_dispatch if you want to force a send while testing.
FORCE_SEND = os.environ.get("FORCE_SEND", "false").lower() == "true"


def now_local() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def should_run_now() -> bool:
    if FORCE_SEND:
        return True
    n = now_local()
    # We deliberately schedule both 18:00 and 19:00 UTC on Sundays.
    # Only one of those is actually 19:00 in Europe/London depending on DST.
    return n.weekday() == 6 and n.hour == 19


def send_pushover(title: str, message: str) -> None:
    resp = requests.post(
        "https://api.pushover.net/1/messages.json",
        data={
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER_OR_GROUP,
            "title": title,
            "message": message,
            "priority": 0,
        },
        timeout=30,
    )
    resp.raise_for_status()


def normalise_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_house_option_text(options: list[str], house_number: str) -> str:
    pattern = re.compile(rf"\b{re.escape(house_number)}\b")
    for opt in options:
        if pattern.search(opt):
            return opt
    raise RuntimeError(f"Could not find an address containing house number {house_number}")


def extract_collection_message(page_text: str) -> str:
    text = normalise_space(page_text)

    patterns = [
        r"(your next collection[^.]*\.)",
        r"(next collection[^.]*\.)",
        r"((?:black|green|recycling|refuse)[^.]*collection[^.]*\.)",
        r"((?:black|green|recycling|refuse)[^.]*will be collected[^.]*\.)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return normalise_space(m.group(1))

    # fallback: find a sentence mentioning collection and a likely bin type
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        s = sentence.lower()
        if "collection" in s and any(word in s for word in ["black", "green", "recycling", "refuse"]):
            return normalise_space(sentence)

    raise RuntimeError("Could not parse the next collection details from the council page")


def lookup_collection() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto(LOOKUP_URL, wait_until="domcontentloaded", timeout=60000)

            # Fill postcode. We avoid brittle field names and use label/text-first strategies.
            postcode_filled = False
            postcode_selectors = [
                'input[name="postcode"]',
                'input[name="Postcode"]',
                'input[id*="postcode" i]',
                'input[aria-label*="postcode" i]',
                'input[type="text"]',
            ]

            for selector in postcode_selectors:
                loc = page.locator(selector).first
                if loc.count() > 0:
                    try:
                        loc.fill(POSTCODE)
                        postcode_filled = True
                        break
                    except Exception:
                        pass

            if not postcode_filled:
                raise RuntimeError("Could not find the postcode input")

            # Click the "Find address" button
            clicked = False
            button_candidates = [
                page.get_by_role("button", name=re.compile(r"find address", re.I)),
                page.get_by_text(re.compile(r"find address", re.I)),
                page.locator('input[type="submit"][value*="Find" i]').first,
                page.locator('button:has-text("Find address")').first,
            ]

            for candidate in button_candidates:
                try:
                    candidate.click(timeout=5000)
                    clicked = True
                    break
                except Exception:
                    pass

            if not clicked:
                raise RuntimeError('Could not click the "Find address" control')

            # Wait for address select/options to appear
            address_select = None
            select_candidates = [
                page.locator("select").first,
                page.locator('select[name*="address" i]').first,
                page.locator('select[id*="address" i]').first,
            ]

            for candidate in select_candidates:
                try:
                    candidate.wait_for(state="visible", timeout=15000)
                    if candidate.count() > 0:
                        address_select = candidate
                        break
                except Exception:
                    pass

            if address_select is None:
                raise RuntimeError("Address dropdown did not appear")

            options = [normalise_space(t) for t in address_select.locator("option").all_inner_texts()]
            options = [o for o in options if o]
            chosen_option = extract_house_option_text(options, HOUSE_NUMBER)

            # Select the matching address by visible label
            try:
                address_select.select_option(label=chosen_option)
            except Exception:
                # fallback: click option text if select_option by label fails
                page.select_option("select", label=chosen_option)

            # Many forms auto-submit on selection; if not, try a likely submit button.
            possible_submit_buttons = [
                page.get_by_role("button", name=re.compile(r"(view|show|submit|find|search)", re.I)),
                page.locator('input[type="submit"]').first,
                page.locator('button[type="submit"]').first,
            ]
            for btn in possible_submit_buttons:
                try:
                    btn.click(timeout=3000)
                    break
                except Exception:
                    pass

            page.wait_for_load_state("networkidle", timeout=20000)

            text = page.locator("body").inner_text(timeout=10000)
            return extract_collection_message(text)

        except PlaywrightTimeoutError as e:
            raise RuntimeError(f"Timed out while waiting for the council site: {e}") from e
        finally:
            browser.close()


def main() -> int:
    if not should_run_now():
        print(f"Skipping run at {now_local().isoformat()} - not the live 19:00 Europe/London slot.")
        return 0

    try:
        collection = lookup_collection()
        message = f"Bin reminder for {POSTCODE}, no. {HOUSE_NUMBER}: {collection}"
        send_pushover("Bin reminder", message)
        print(message)
        return 0
    except Exception as e:
        err = f"Bin reminder failed: {e}"
        print(err, file=sys.stderr)
        try:
            send_pushover("Bin reminder error", err)
        except Exception as push_err:
            print(f"Also failed to send Pushover error notification: {push_err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
