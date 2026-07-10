import html
import json
# import os
import re
import secrets
import shutil
# import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import json5
import requests
from bs4 import BeautifulSoup
from google.cloud import firestore
from gradescopeapi.api.constants import BASE_URL
from gradescopeapi.classes.account import Account
# from playwright.sync_api import sync_playwright, BrowserContext

PROFILE_ROOT = Path("gradescope_profiles")
PROFILE_ROOT.mkdir(exist_ok=True)

METADATA_FILE = PROFILE_ROOT / "metadata.json"

class GSConnectionFromSession:
    def __init__(self, session: requests.Session | None, user: dict[str, str]) -> None:
        self.session = session
        self.logged_in = True
        self.start_time = datetime.now().isoformat()
        self.account = Account(session)
        self.name = user.get('name', None)
        self.email = user.get('email', None)

SAMPLE_PLACEHOLDER_GS_CONN = GSConnectionFromSession(None, {})

def create_token() -> str:
    return secrets.token_urlsafe(32)

def load_metadata() -> Any:
    if not METADATA_FILE.exists():
        return {}
    with open(METADATA_FILE) as f:
        return json.load(f)

def save_metadata(data: dict[str, Any]) -> None:
    with open(METADATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def cleanup_old_profiles(days: int = 30) -> None:
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

def register_token(token: str) -> None:
    data = load_metadata()
    data[token] = datetime.now(timezone.utc).isoformat()
    save_metadata(data)

def profile_dir_for_token(token: str) -> Path:
    return PROFILE_ROOT / token

# def build_session_from_playwright(context: BrowserContext) -> requests.Session:
#     session = requests.Session()
#     storage = context.storage_state()
#     for cookie in storage["cookies"]:
#         session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
#     return session

def build_session_from_cookies(cookies: list[list[str]] | dict[str, Any]) -> requests.Session:
    session = requests.Session()
    if isinstance(cookies, list):
        for name, value in cookies:
            session.cookies.set(name, value, domain="www.gradescope.com", path="/")
    else: 
        for cookie in cookies['cookies']: 
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    return session

def login_with_cookies(cookies: list[list[str]]) -> tuple[GSConnectionFromSession, dict[str, str]]:
    session = build_session_from_cookies(cookies)
    print([c.name for c in session.cookies])
    resp = session.get(BASE_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    scripts = soup.find_all("script")
    # print(soup)
    user = {}
    for script in scripts:
        if script.string and "bugsnagClient.user" in script.string:
            match = re.search(r"bugsnagClient\.user\s*=\s*({.*?});", script.string)
            if match:
                user = json5.loads(match.group(1))
                break
    return GSConnectionFromSession(session, user), user

# def login_with_token(token: str) -> GSConnectionFromSession:
#     profile_dir = profile_dir_for_token(token)
#     with sync_playwright() as p:
#         context = p.chromium.launch_persistent_context(str(profile_dir), headless=False, args=["--no-sandbox", "--disable-dev-shm-usage", "--single-process", "--no-zygote",],)
#         page = context.pages[0] if context.pages else context.new_page()
#         page.goto(f'{BASE_URL}/login')
#         page.wait_for_selector("text=Course Dashboard", timeout=0)
#         user = page.evaluate("bugsnagClient.user")
#         session = build_session_from_playwright(context)
#         context.close()
#     return GSConnectionFromSession(session, user)

# def login_temporary() -> tuple[GSConnectionFromSession, str]:
#     temp_profile_dir = tempfile.mkdtemp()
#     shutil.rmtree(temp_profile_dir, ignore_errors=True)
#     os.makedirs(temp_profile_dir, exist_ok=True)
#     with sync_playwright() as p:
#         context = p.chromium.launch_persistent_context(temp_profile_dir, headless=False, args=["--no-sandbox", "--disable-dev-shm-usage", "--single-process", "--no-zygote",],)
#         page = context.pages[0] if context.pages else context.new_page()
#         page.goto(f'{BASE_URL}/login')
#         page.wait_for_selector("text=Course Dashboard", timeout=0)
#         user = page.evaluate("bugsnagClient.user")
#         session = build_session_from_playwright(context)
#         context.close()
#     conn = GSConnectionFromSession(session, user)
#     return conn, temp_profile_dir

def save_profile_for_token(temp_profile_dir: Path, token: str) -> None:
    destination = profile_dir_for_token(token)
    shutil.copytree(temp_profile_dir, destination, dirs_exist_ok=True)

def create_new_user() -> str:
    token = create_token()
    profile_dir = profile_dir_for_token(token)
    profile_dir.mkdir(parents=True, exist_ok=True)
    register_token(token)
    return token

def read_session_doc_from_firestore(session_id: str, firestore_db: firestore.Client) -> str:
    doc = cast(firestore.DocumentSnapshot, firestore_db.collection("sessions").document(session_id).get())
    if doc.exists:
        doc_dict = doc.to_dict()
        assert doc_dict
        return str(doc_dict['session'])
    else:
        raise Exception("Gradescope auth session ID not found")


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