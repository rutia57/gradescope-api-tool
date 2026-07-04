from playwright.sync_api import sync_playwright

URL = "https://gradescope-api-tool.streamlit.app/" 
EXPECTED_TEXT = "welcome to the gradescope api tool" 

with sync_playwright() as p: 
    browser = p.chromium.launch(headless=True) 
    page = browser.new_page() 
    page.goto(URL, wait_until="domcontentloaded", timeout=30000) 
    frame = page.frame_locator('iframe[title="streamlitApp"]')
    frame.get_by_text("Welcome to the Gradescope API Tool").first.wait_for(timeout=30000)
    text = frame.locator("body").inner_text().lower()
    assert EXPECTED_TEXT in text, f'Expected "{EXPECTED_TEXT}" in:\n\n{text}' 
    browser.close() 
    print("Success!")