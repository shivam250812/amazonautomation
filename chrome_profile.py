"""
chrome_profile.py — Shared Chrome / Playwright profile setup.

Both script.py (Amazon scraper) and sellercentral.py import from here
so Chrome configuration lives in exactly one place.
"""

import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ─── Paths & Defaults ──────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
_CHROME_DATA_DEFAULT = str(_SCRIPT_DIR / "chrome-data")


def _resolve_user_data_dir(raw: str) -> str:
    """
    Make CHROME_USER_DATA_DIR stable even if you run from another cwd.
    - Absolute paths are used as-is
    - Relative paths are resolved relative to this script's folder
    """
    p = Path(raw)
    if p.is_absolute():
        return str(p)
    return str((_SCRIPT_DIR / p).resolve())


def get_default_chrome_path() -> str:
    system = platform.system()
    if system == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    elif system == "Windows":
        return r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    else:
        return "/usr/bin/google-chrome"


CHROME_USER_DATA_DIR = _resolve_user_data_dir(
    os.getenv("CHROME_USER_DATA_DIR", _CHROME_DATA_DEFAULT)
)
CHROME_EXECUTABLE = os.getenv("CHROME_EXECUTABLE", get_default_chrome_path())
PROFILE_DIR = os.getenv("CHROME_PROFILE_DIR", "Default")
HELIUM10_EXTENSION_ID = os.getenv(
    "HELIUM10_EXTENSION_ID", "njmehopjdpcckochcggncklnlmikcbnb"
)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def google_chrome_is_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-x", "Google Chrome"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def is_system_default_chrome_user_data(candidate: Path) -> bool:
    """True if this is the real macOS Chrome profile root (not ./chrome-data copy)."""
    try:
        default_root = (
            Path.home() / "Library/Application Support/Google/Chrome"
        ).resolve()
        return candidate.resolve() == default_root
    except Exception:
        return False


def repair_chrome_crash_recovery(
    user_data_dir: Path, profile_dir: str
) -> None:
    """
    Clear Chrome's 'didn't shut down correctly' state so the Restore
    banner is less likely.  Safe for copied profiles; run only when
    Chrome is not using this user-data dir.
    """
    profile_root = user_data_dir / profile_dir
    if not profile_root.is_dir():
        return

    prefs_path = profile_root / "Preferences"
    if prefs_path.exists():
        try:
            data = json.loads(prefs_path.read_text(encoding="utf-8"))
            prof = data.get("profile")
            if not isinstance(prof, dict):
                prof = {}
            prof["exit_type"] = "Normal"
            prof["exited_cleanly"] = True
            data["profile"] = prof
            prefs_path.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
            print(" Cleared crash / restore flags in Chrome Preferences")
        except Exception as e:
            print(f" Could not patch Chrome Preferences: {e}")

    for fname in (
        "Current Session",
        "Current Tabs",
        "Last Session",
        "Last Tabs",
    ):
        p = profile_root / fname
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    # Stale singleton locks block launch; safe if no Chrome is running
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lp = user_data_dir / lock
        if lp.exists():
            try:
                lp.unlink()
            except OSError:
                pass


def find_extension_path(
    user_data_dir: Path, profile_dir: str, extension_id: str
) -> Optional[Path]:
    ext_root = user_data_dir / profile_dir / "Extensions" / extension_id
    if not ext_root.exists():
        return None
    versions = [p for p in ext_root.iterdir() if p.is_dir()]
    if not versions:
        return None
    return sorted(versions, key=lambda p: p.name)[-1]


def purge_helium10_storage(
    user_data_dir: Path, profile_dir: str
) -> None:
    """
    Best-effort cleanup for Helium 10 auth/storage in a copied Chrome profile.
    """
    profile = user_data_dir / profile_dir

    cookies_db = profile / "Cookies"
    if cookies_db.exists():
        try:
            conn = sqlite3.connect(str(cookies_db))
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM cookies WHERE host_key LIKE '%helium10.com%'"
            )
            conn.commit()
            conn.close()
            print(" Cleared Helium 10 cookies")
        except Exception as e:
            print(f" Could not clear cookies DB: {e}")

    for rel in [
        "Local Storage",
        "Session Storage",
        "IndexedDB",
        "Service Worker",
        "Local Extension Settings",
        "Extension State",
    ]:
        base = profile / rel
        if not base.exists():
            continue
        try:
            for child in base.iterdir():
                name = child.name.lower()
                if "helium" in name or "helium10" in name:
                    if child.is_dir():
                        for root, dirs, files in os.walk(
                            child, topdown=False
                        ):
                            for fn in files:
                                try:
                                    os.remove(os.path.join(root, fn))
                                except Exception:
                                    pass
                            for dn in dirs:
                                try:
                                    os.rmdir(os.path.join(root, dn))
                                except Exception:
                                    pass
                        try:
                            os.rmdir(child)
                        except Exception:
                            pass
                    else:
                        try:
                            child.unlink()
                        except Exception:
                            pass
            print(f" Cleaned Helium 10 artifacts in {rel}")
        except Exception as e:
            print(f" Could not clean {rel}: {e}")


# ─── Browser Launcher ─────────────────────────────────────────────────────────

async def create_browser(p, *, require_helium: bool = True, is_setup_mode: bool = False):
    """
    Launch a persistent Chrome context with full profile support.

    Parameters
    ----------
    p : playwright async Playwright instance
    require_helium : bool
        If True, verify Helium 10 extension is installed.
    is_setup_mode : bool
        If True, skips extension checks and just opens a browser for the user to configure.
    """
    print("\n Launching browser with FULL Chrome data...\n")
    print(f"   - executable:    {CHROME_EXECUTABLE}")
    print(f"   - user data dir: {CHROME_USER_DATA_DIR}")
    print(f"   - profile dir:   {PROFILE_DIR}\n")

    profile_path = Path(CHROME_USER_DATA_DIR)

    if not profile_path.exists():
        if not is_setup_mode:
            print(" Chrome user data dir not found!")
            print(f"Looked for: {profile_path}")
            print("\n Tip: Run `python run_pipeline.py --setup` to create and configure a new profile first.")
            sys.exit(1)
        else:
            print(" Creating new Chrome profile directory...")
            profile_path.mkdir(parents=True, exist_ok=True)

    if is_system_default_chrome_user_data(profile_path) and os.getenv(
        "ALLOW_LIVE_CHROME_PROFILE", "0"
    ) != "1":
        print(
            " Do not point automation at your main Chrome folder "
            "(~/Library/Application Support/Google/Chrome).\n"
            "   Use a copied folder instead.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    if is_system_default_chrome_user_data(profile_path) and google_chrome_is_running():
        print(
            " Google Chrome is already running. Quit it completely, then rerun.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    if "chrome-data" in str(profile_path.resolve()).replace("\\", "/").lower():
        print(
            "  Helium 10 session lives only in ./chrome-data .\n"
            "   • Log in to Helium inside THIS automation window.\n"
        )

    if os.getenv("SKIP_PROFILE_REPAIR", "0") != "1":
        repair_chrome_crash_recovery(profile_path, PROFILE_DIR)
    else:
        print("  SKIP_PROFILE_REPAIR=1 — skipped crash/session file cleanup.\n")

    args_extra = []

    if require_helium and not is_setup_mode:
        helium_ext = find_extension_path(
            profile_path, PROFILE_DIR, HELIUM10_EXTENSION_ID
        )
        if not helium_ext:
            print(" Helium 10 extension not found inside this profile.")
            print(
                f"   Expected: {profile_path}/{PROFILE_DIR}/Extensions/"
                f"{HELIUM10_EXTENSION_ID}/<version>"
            )
            print("\n Tip: Run `python run_pipeline.py --setup` to install it and log in.")
            sys.exit(1)

        if os.getenv("HELIUM_UNPACKED_ONLY", "0") == "1":
            args_extra = [
                f"--disable-extensions-except={str(helium_ext)}",
                f"--load-extension={str(helium_ext)}",
            ]

    args = [
        "--start-maximized",
        "--disable-blink-features=AutomationControlled",
        f"--profile-directory={PROFILE_DIR}",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-gpu",
        "--js-flags=--max-old-space-size=512",
        "--disable-background-networking",
        "--disable-extensions-except=" + str(helium_ext) if require_helium and not is_setup_mode else "",
    ] + args_extra

    print(" Starting Chrome (max ~2 min)…\n")
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(profile_path),
        executable_path=CHROME_EXECUTABLE,
        headless=False,
        ignore_default_args=[
            "--disable-extensions",
            "--use-mock-keychain",
            "--password-store=basic",
        ],
        args=args,
        timeout=120_000,
    )
    print(" Chrome connected.\n")

    return context
