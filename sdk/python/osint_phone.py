"""Phone Number OSINT — Extract everything possible from a phone number.

Sources: phonenumbers lib, Truecaller, WhatsApp, Telegram, social media checks.
For authorized reconnaissance only.
"""
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.request

import phonenumbers
from phonenumbers import carrier, geocoder, timezone

logging.basicConfig(level="INFO", format="%(asctime)s [osint] %(message)s")
logger = logging.getLogger("osint")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def phone_basic_info(number: str) -> dict:
    """Extract basic info from phone number using phonenumbers lib."""
    try:
        parsed = phonenumbers.parse(number, "IN")
        if not phonenumbers.is_valid_number(parsed):
            return {"error": "Invalid phone number"}

        country_code = parsed.country_code
        national = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
        international = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

        return {
            "number": number,
            "valid": True,
            "country_code": f"+{country_code}",
            "national_format": national,
            "international_format": international,
            "e164": e164,
            "country": geocoder.description_for_number(parsed, "en"),
            "carrier": carrier.name_for_number(parsed, "en"),
            "timezones": list(timezone.time_zones_for_number(parsed)),
            "number_type": _number_type(phonenumbers.number_type(parsed)),
            "is_possible": phonenumbers.is_possible_number(parsed),
        }
    except Exception as e:
        return {"error": str(e)}


def _number_type(t) -> str:
    types = {
        0: "FIXED_LINE", 1: "MOBILE", 2: "FIXED_LINE_OR_MOBILE",
        3: "TOLL_FREE", 4: "PREMIUM_RATE", 5: "SHARED_COST",
        6: "VOIP", 7: "PERSONAL_NUMBER", 8: "PAGER",
        9: "UAN", 10: "VOICEMAIL", -1: "UNKNOWN",
    }
    return types.get(t, "UNKNOWN")


def check_whatsapp(number: str) -> dict:
    """Check if number is on WhatsApp using wa.me redirect."""
    e164 = number.replace("+", "").replace(" ", "").replace("-", "")
    if not e164.startswith("91"):
        e164 = "91" + e164
    try:
        url = f"https://wa.me/{e164}"
        req = urllib.request.Request(url, headers=HEADERS, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {
                "platform": "whatsapp",
                "likely_registered": True,
                "link": f"https://wa.me/{e164}",
                "note": "wa.me link accessible (doesn't confirm registration, but suggests it)",
            }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"platform": "whatsapp", "likely_registered": False}
        return {"platform": "whatsapp", "likely_registered": "unknown", "http_code": e.code}
    except Exception as e:
        return {"platform": "whatsapp", "error": str(e)}


def check_telegram(number: str) -> dict:
    """Check Telegram presence via t.me/+number."""
    e164 = number.replace("+", "").replace(" ", "").replace("-", "")
    if not e164.startswith("91"):
        e164 = "91" + e164
    return {
        "platform": "telegram",
        "note": "Telegram doesn't expose number→username publicly. Use Telethon contacts.ImportContacts to check.",
        "manual_check": f"Add +{e164} to contacts in Telegram app to see profile",
    }


def check_truecaller(number: str) -> dict:
    """Truecaller search hint — can't scrape directly without auth."""
    e164 = number.replace("+", "").replace(" ", "").replace("-", "")
    if not e164.startswith("91"):
        e164 = "91" + e164
    return {
        "platform": "truecaller",
        "search_url": f"https://www.truecaller.com/search/in/{e164[-10:]}",
        "note": "Open this URL in browser (requires Truecaller login) to see caller ID, spam reports, address",
    }


def check_social_media(number: str) -> dict:
    """Check social media presence via various methods."""
    e164 = number.replace("+", "").replace(" ", "").replace("-", "")
    if not e164.startswith("91"):
        e164 = "91" + e164
    national = e164[-10:]

    results = {}

    # Google dork
    results["google_dorks"] = [
        f'"{national}" site:facebook.com',
        f'"{national}" site:instagram.com',
        f'"{national}" site:linkedin.com',
        f'"+91{national}" OR "{national}"',
        f'intext:"{national}" filetype:pdf',
    ]
    results["google_search_url"] = f"https://www.google.com/search?q=%22{national}%22"

    # Facebook recovery check
    results["facebook"] = {
        "recovery_url": "https://www.facebook.com/login/identify/",
        "note": "Enter the phone number in FB's account recovery to see if an account exists (shows partial name/photo)",
    }

    # Instagram
    results["instagram"] = {
        "note": "Instagram password reset with phone number can reveal if account exists",
        "recovery_url": "https://www.instagram.com/accounts/password/reset/",
    }

    # LinkedIn
    results["linkedin"] = {
        "note": "LinkedIn password reset with phone can reveal account existence",
    }

    # UPI/payment apps (India specific)
    results["upi"] = {
        "note": f"Try sending Re.1 to {national}@paytm, {national}@ybl, {national}@upi to find UPI IDs and real names",
        "common_vpas": [
            f"{national}@paytm",
            f"{national}@ybl",
            f"{national}@okaxis",
            f"{national}@oksbi",
            f"{national}@okhdfcbank",
            f"{national}@okicici",
        ],
    }

    return results


def check_data_breaches(number: str) -> dict:
    """Check if number appears in known data breach databases."""
    e164 = number.replace("+", "").replace(" ", "").replace("-", "")
    national = e164[-10:]

    return {
        "haveibeenpwned": {
            "url": f"https://haveibeenpwned.com/",
            "note": "Search with email associated with this number",
        },
        "intelligence_x": {
            "url": "https://intelx.io/",
            "note": f"Search for +91{national} — shows breached databases, paste sites, dark web mentions",
        },
        "dehashed": {
            "url": "https://dehashed.com/",
            "note": f"Search phone:{national} — shows breached records with emails, passwords, addresses",
        },
        "leak_lookup": {
            "url": "https://leak-lookup.com/",
            "note": f"Search +91{national}",
        },
    }


def check_telecom_osint(number: str) -> dict:
    """Telecom-specific OSINT for Indian numbers."""
    e164 = number.replace("+", "").replace(" ", "").replace("-", "")
    national = e164[-10:]

    # Indian number series identification
    prefix = national[:4]

    return {
        "number_series": prefix,
        "dnd_check": {
            "url": "https://www.nccptrai.gov.in/nccpregistry/search.misc",
            "note": "Check if number is on Do Not Disturb registry",
        },
        "mnp_check": {
            "note": "Number may have been ported — carrier from phonenumbers lib shows original, not current",
        },
        "ceir_check": {
            "url": "https://www.ceir.gov.in/Home/index.jsp",
            "note": "Check if any IMEI is linked to this number (lost/stolen device registry)",
        },
    }


def telegram_deep_lookup(number: str) -> dict:
    """Use Telethon to look up Telegram account from phone number."""
    e164 = number.replace("+", "").replace(" ", "").replace("-", "")
    if not e164.startswith("91"):
        e164 = "91" + e164

    try:
        import asyncio
        from telethon import TelegramClient
        from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
        from telethon.tl.types import InputPhoneContact

        SESSION = "/opt/swarmesh/swarmesh_telegram"
        API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
        API_HASH = os.getenv("TELEGRAM_API_HASH", "")

        async def lookup():
            client = TelegramClient(SESSION, API_ID, API_HASH)
            await client.start()

            # Import as contact
            contact = InputPhoneContact(
                client_id=0,
                phone=f"+{e164}",
                first_name="Lookup",
                last_name="Target",
            )
            result = await client(ImportContactsRequest([contact]))

            info = {"platform": "telegram", "phone": f"+{e164}"}

            if result.users:
                user = result.users[0]
                info.update({
                    "found": True,
                    "user_id": user.id,
                    "first_name": user.first_name or "",
                    "last_name": user.last_name or "",
                    "username": user.username or "",
                    "phone": user.phone or "",
                    "is_bot": user.bot,
                    "is_premium": getattr(user, "premium", False),
                    "profile_photo": user.photo is not None,
                })

                # Clean up — delete the imported contact
                await client(DeleteContactsRequest(id=[user]))
            else:
                info["found"] = False

            await client.disconnect()
            return info

        return asyncio.run(lookup())

    except ImportError:
        return {"platform": "telegram", "error": "Telethon not installed (run on VPS)"}
    except Exception as e:
        return {"platform": "telegram", "error": str(e)}


def full_osint(number: str, deep: bool = False) -> dict:
    """Run full OSINT on a phone number."""
    print(f"\n{'='*60}")
    print(f"  OSINT REPORT: {number}")
    print(f"{'='*60}\n")

    results = {}

    # Basic info
    print("[*] Basic phone info...")
    results["basic"] = phone_basic_info(number)
    _print_section("BASIC INFO", results["basic"])

    # WhatsApp
    print("[*] Checking WhatsApp...")
    results["whatsapp"] = check_whatsapp(number)
    _print_section("WHATSAPP", results["whatsapp"])

    # Telegram
    print("[*] Checking Telegram...")
    if deep:
        results["telegram"] = telegram_deep_lookup(number)
    else:
        results["telegram"] = check_telegram(number)
    _print_section("TELEGRAM", results["telegram"])

    # Truecaller
    print("[*] Truecaller...")
    results["truecaller"] = check_truecaller(number)
    _print_section("TRUECALLER", results["truecaller"])

    # Social media
    print("[*] Social media checks...")
    results["social_media"] = check_social_media(number)
    _print_section("SOCIAL MEDIA", results["social_media"])

    # Data breaches
    print("[*] Data breach databases...")
    results["data_breaches"] = check_data_breaches(number)
    _print_section("DATA BREACHES", results["data_breaches"])

    # Telecom OSINT
    print("[*] Telecom OSINT...")
    results["telecom"] = check_telecom_osint(number)
    _print_section("TELECOM", results["telecom"])

    print(f"\n{'='*60}")
    print(f"  OSINT COMPLETE")
    print(f"{'='*60}\n")

    return results


def _print_section(title, data, indent=2):
    prefix = " " * indent
    print(f"\n  [{title}]")
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                print(f"{prefix}{k}:")
                for k2, v2 in v.items():
                    print(f"{prefix}  {k2}: {v2}")
            elif isinstance(v, list):
                print(f"{prefix}{k}:")
                for item in v[:10]:
                    print(f"{prefix}  - {item}")
            else:
                print(f"{prefix}{k}: {v}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python osint_phone.py <phone_number> [--deep]")
        print("  --deep: Use Telethon for Telegram lookup (requires session on VPS)")
        print("\nExamples:")
        print("  python osint_phone.py 9876543210")
        print("  python osint_phone.py +919876543210 --deep")
        sys.exit(0)

    number = sys.argv[1]
    deep = "--deep" in sys.argv

    results = full_osint(number, deep=deep)

    # Save to file
    outfile = f"/tmp/osint_{number.replace('+','').replace(' ','')}.json"
    with open(outfile, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Full results saved to: {outfile}")
