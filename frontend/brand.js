// Shared brand substitution: replaces the {{AVATAR_NAME}} placeholder in static
// pages with the avatar's display name from /api/config (env AVATAR_DISPLAY_NAME).
// Keeps the avatar name out of the markup so it's never hardcoded — change the env
// var in one place and every page follows. Falls back to "Avatar" when unset.
(function () {
    const TOKEN = "{{AVATAR_NAME}}";

    function applyName(name) {
        const value = (name && String(name).trim()) || "Avatar";

        if (document.title.includes(TOKEN)) {
            document.title = document.title.split(TOKEN).join(value);
        }

        // Replace inside text nodes.
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_TEXT,
            null,
        );
        const textNodes = [];
        while (walker.nextNode()) {
            if (walker.currentNode.nodeValue.includes(TOKEN)) {
                textNodes.push(walker.currentNode);
            }
        }
        for (const node of textNodes) {
            node.nodeValue = node.nodeValue.split(TOKEN).join(value);
        }

        // Replace inside user-facing attributes.
        const attrs = ["title", "placeholder", "aria-label", "alt", "value"];
        for (const el of document.querySelectorAll("*")) {
            for (const attr of attrs) {
                const v = el.getAttribute && el.getAttribute(attr);
                if (v && v.includes(TOKEN)) {
                    el.setAttribute(attr, v.split(TOKEN).join(value));
                }
            }
        }
    }

    async function init() {
        let name = "";
        try {
            const res = await fetch("/api/config");
            if (res.ok) {
                const cfg = await res.json();
                name = (cfg.defaults && cfg.defaults.avatarDisplayName) || "";
            }
        } catch (_) {
            // Network/parse failure -> fall back to "Avatar".
        }
        applyName(name);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
