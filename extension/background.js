chrome.action.onClicked.addListener(async (tab) => {
    const url = tab?.url || "";
    const isGradescope = url.startsWith("https://www.gradescope.com/");
    if (!isGradescope) {
        chrome.windows.getCurrent((win) => {
            const width = 420;
            const height = 260;
            chrome.windows.create({
                url: chrome.runtime.getURL("popup.html"),
                type: "popup",
                width,
                height,
                left: Math.round(win.left + (win.width - width) / 2),
                top: Math.round(win.top + (win.height - height) / 2)
            });
        });
        return;
    }
    chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["content.js"]
    });
});


chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    // -------------------------
    // LOGIN CHECK
    // -------------------------
    if (msg.type === "check_login") {
        chrome.cookies.getAll({
            domain: "www.gradescope.com"
        }, (cookies) => {
            const loggedIn = cookies.some(c => {
                const sessionCookies = [
                    "signed_token",
                ];
                return sessionCookies.includes(c.name) && !!c.value;
            });
            sendResponse({ loggedIn, cookies });
        });
        return true;
    }

    // -------------------------
    // LAUNCH
    // -------------------------
    if (msg.type === "launch") {
        chrome.cookies.getAll({
            domain: "www.gradescope.com"
        }, (cookies) => {
            const data = {
                name: msg.name,
                email: msg.email,
                cookies
            };
            const bytes = new TextEncoder().encode(JSON.stringify(data));
            let binary = "";
            for (const b of bytes) binary += String.fromCharCode(b);
            const session = btoa(binary);
            chrome.tabs.create({
                url: "https://gradescope-api-tool.streamlit.app/?session_from_ext=" +
                     encodeURIComponent(session)
            });
        });
    }
});