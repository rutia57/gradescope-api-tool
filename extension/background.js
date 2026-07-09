import { initializeApp } from "https://www.gstatic.com/firebasejs/11.0.2/firebase-app.js";
import { getFirestore, doc, setDoc } from "https://www.gstatic.com/firebasejs/11.0.2/firebase-firestore.js";

const firebaseConfig = {
  apiKey: "AIzaSyDkq0WkE4-n2TIv5yj_QXsUXFlV7IhAze8",
  authDomain: "streamlit-app-tracking.firebaseapp.com",
  projectId: "streamlit-app-tracking",
  storageBucket: "streamlit-app-tracking.firebasestorage.app",
  messagingSenderId: "924961359770",
  appId: "1:924961359770:web:b58ed52b7f23aafd025c23",
};

const app = initializeApp(firebaseConfig);
const db = getFirestore(app);

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

function randomId(length = 8) {
    const chars =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    const bytes = crypto.getRandomValues(new Uint8Array(length));
    return Array.from(bytes, b => chars[b % chars.length]).join("");
}

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
        }, async (cookies) => {
            const data = {
                name: msg.name,
                email: msg.email,
                cookies
            };
            const bytes = new TextEncoder().encode(JSON.stringify(data));
            let binary = "";
            for (const b of bytes) binary += String.fromCharCode(b);
            const session = btoa(binary);

            const id = randomId();

            await setDoc(doc(db, "sessions", id), {
                session,
                created: new Date(),
                expires: new Date(Date.now() + 24 * 60 * 60 * 1000) //24 hours
            });

            chrome.tabs.create({
                url: "https://gradescope-api-tool.streamlit.app/?session_from_ext=b&session_from_ext_id=" + id
                    //  encodeURIComponent(session)
            });
        });
    }
});