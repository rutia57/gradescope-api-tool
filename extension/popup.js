document.getElementById("open").addEventListener("click", async () => {
    await chrome.tabs.create({
        url: "https://www.gradescope.com/login"
    });
    window.close(); // closes the popup window
});

document.getElementById("ok").addEventListener("click", () => {
    window.close();
});