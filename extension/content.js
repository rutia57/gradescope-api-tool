function launch() {
    chrome.runtime.sendMessage({ type: "check_login" }, (res) => {
        if (!res?.loggedIn) {
            alert(
                "This tool can only be opened from an authenticated Gradescope session.\n" +
                "Please log in to Gradescope first, then click the extension again to open the API tool."
            );
            return;
        }
        const user = window.bugsnagClient?.user || {};
        chrome.runtime.sendMessage({
            type: "launch",
            name: user.name || "",
            email: user.email || ""
        });
});}

launch();