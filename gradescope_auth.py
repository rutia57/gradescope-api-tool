import secrets
from pathlib import Path
import tempfile
import shutil
import json
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

from gradescopeapi.classes.account import Account
from gradescopeapi.api.constants import BASE_URL

PROFILE_ROOT = Path("gradescope_profiles")
PROFILE_ROOT.mkdir(exist_ok=True)

METADATA_FILE = PROFILE_ROOT / "metadata.json"

class GSConnectionFromSession:
    def __init__(self, session, user):
        self.session = session
        self.logged_in = True
        self.start_time = datetime.now().isoformat()
        self.account = Account(session)
        self.name = user.get('name', None)
        self.email = user.get('email', None)

def create_token():
    return secrets.token_urlsafe(32)

def load_metadata():
    if not METADATA_FILE.exists():
        return {}
    with open(METADATA_FILE) as f:
        return json.load(f)

def save_metadata(data):
    with open(METADATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def cleanup_old_profiles(days=30):
    data = load_metadata()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    updated = {}
    for token, created in data.items():
        created_dt = datetime.fromisoformat(created)
        if created_dt >= cutoff:
            updated[token] = created
            continue
        profile_dir = profile_dir_for_token(token)
        if profile_dir.exists():
            shutil.rmtree(profile_dir)
    save_metadata(updated)

def register_token(token):
    data = load_metadata()
    data[token] = datetime.now(timezone.utc).isoformat()
    save_metadata(data)

def profile_dir_for_token(token):
    return PROFILE_ROOT / token

def build_session_from_playwright(context):
    session = requests.Session()
    storage = context.storage_state()
    for cookie in storage["cookies"]:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    return session

def login_with_token(token):
    profile_dir = profile_dir_for_token(token)
    with sync_playwright() as p:
        print("Launching browser...")
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],)
        print("Browser launched!")
        page = context.pages[0] if context.pages else context.new_page()
        print("Page created!")
        page.goto(f'{BASE_URL}/login')
        print("Went to login!")
        page.wait_for_selector("text=Course Dashboard", timeout=0)
        user = page.evaluate("bugsnagClient.user")
        session = build_session_from_playwright(context)
        context.close()
    return GSConnectionFromSession(session, user)

def login_temporary():
    temp_profile_dir = tempfile.mkdtemp()
    with sync_playwright() as p:
        print("Launching browser...")
        context = p.chromium.launch_persistent_context(temp_profile_dir, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"],)
        print("Browser launched!")
        page = context.pages[0] if context.pages else context.new_page()
        print("Page created!")
        page.goto(f'{BASE_URL}/login')
        print("Went to login!")
        page.wait_for_selector("text=Course Dashboard", timeout=0)
        user = page.evaluate("bugsnagClient.user")
        session = build_session_from_playwright(context)
        context.close()
    conn = GSConnectionFromSession(session, user)
    return conn, temp_profile_dir

def save_profile_for_token(temp_profile_dir, token):
    destination = profile_dir_for_token(token)
    shutil.copytree(temp_profile_dir, destination, dirs_exist_ok=True)

def create_new_user():
    token = create_token()
    profile_dir = profile_dir_for_token(token)
    profile_dir.mkdir(parents=True, exist_ok=True)
    register_token(token)
    return token