from playwright.sync_api import sync_playwright

URL = "https://gradescope-api-tool.streamlit.app/?automatic_ping=1"
EXPECTED_TEXT = "welcome to the gradescope api tool" 

with sync_playwright() as p: 
    browser = p.chromium.launch(headless=True)
    page = browser.new_page() 
    page.goto(URL, wait_until="domcontentloaded", timeout=30000) 
    page.wait_for_timeout(5000)
    page.screenshot(path='tmp.png', full_page=True)
    wake_button = page.get_by_role("button", name="Yes, get this app back up!")
    had_to_wake_up = False
    if wake_button.is_visible():
            wake_button.click()
            page.wait_for_timeout(30000)
            had_to_wake_up = True
    frame = page.frame_locator('iframe[title="streamlitApp"]')
    frame.get_by_text("Welcome to the Gradescope API Tool").first.wait_for(timeout=30000)
    text = frame.locator("body").inner_text().lower()
    assert EXPECTED_TEXT in text, f'Expected "{EXPECTED_TEXT}" in:\n\n{text}' 
    assert not had_to_wake_up, 'Had to wake up app – check why it went to sleep :('
    browser.close() 
    print("Success!")