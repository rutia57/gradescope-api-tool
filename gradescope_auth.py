import secrets
from pathlib import Path
import tempfile
import shutil
import os
import json
import json5
import html
import re
from bs4 import BeautifulSoup
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

SAMPLE_PLACEHOLDER_GS_CONN = GSConnectionFromSession(None, {})

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

def build_session_from_cookies(cookies):
    session = requests.Session()
    for cookie in cookies['cookies']:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    return session

def login_with_cookies(cookies): 
    session = build_session_from_cookies(cookies)
    resp = session.get(BASE_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    scripts = soup.find_all("script")
    user = {}
    for script in scripts:
        if script.string and "bugsnagClient.user" in script.string:
            match = re.search(r"bugsnagClient\.user\s*=\s*({.*?});", script.string)
            if match:
                user = json5.loads(match.group(1))
                break
    return GSConnectionFromSession(session, user), user

def login_with_token(token):
    profile_dir = profile_dir_for_token(token)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile_dir), headless=False, args=["--no-sandbox", "--disable-dev-shm-usage", "--single-process", "--no-zygote",],)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f'{BASE_URL}/login')
        page.wait_for_selector("text=Course Dashboard", timeout=0)
        user = page.evaluate("bugsnagClient.user")
        session = build_session_from_playwright(context)
        context.close()
    return GSConnectionFromSession(session, user)

def login_temporary():
    temp_profile_dir = tempfile.mkdtemp()
    shutil.rmtree(temp_profile_dir, ignore_errors=True)
    os.makedirs(temp_profile_dir, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(temp_profile_dir, headless=False, args=["--no-sandbox", "--disable-dev-shm-usage", "--single-process", "--no-zygote",],)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f'{BASE_URL}/login')
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

bookmarklet_code = r"""
    javascript:(function(){
        if(location.hostname!=="www.gradescope.com"){
            alert("This tool can only be opened from an authenticated Gradescope session.\nPlease go to https://www.gradescope.com to log in, and once you're logged in, click the bookmark again to open the API tool.");
            if (confirm("Open the Gradescope login page?")) {
                window.open("https://www.gradescope.com/login", "_blank");
            }
            return;
        }
        const el = document.querySelector('input[name="authenticity_token"]');
        if(!el){
            alert("This tool can only be opened from an authenticated Gradescope session.\nPlease log in to Gradescope first, then click the bookmark to open the API tool.");
            return;
        }
        const authToken = el.value;
        const user = window.bugsnagClient?.user || {};

        const cookieString = document.cookie
            .split('; ')
            .filter(Boolean)
            .map(c => {
                const i = c.indexOf('=');
                const key = c.slice(0, i);
                const value = c.slice(i + 1);
                return `&${encodeURIComponent(key)}=${encodeURIComponent(value)}`;
            })
            .join('');

        const originalFetch = window.fetch;
        window.fetch = async (...args) => {
            console.log("FETCH REQUEST:", args);
            const response = await originalFetch(...args);
            return response;
        };

        window.location.href = "http://localhost:8501/?auth_token=" + encodeURIComponent(authToken) 
                        + "&name=" + encodeURIComponent(user.name || "")
                        + "&email=" + encodeURIComponent(user.email || "") + cookieString;;
    })();
    """

bookmarklet_oneliner = html.escape(''.join([l.strip() for l in bookmarklet_code.split('\n')]), quote=True)