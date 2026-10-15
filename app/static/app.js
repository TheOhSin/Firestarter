document.addEventListener("DOMContentLoaded", () => {
    document
        .querySelectorAll("[data-copy-select]")
        .forEach((element) => {
            element.addEventListener("click", () => {
                element.select();
            });
        });

    document
        .querySelectorAll("form[data-confirm]")
        .forEach((form) => {
            form.addEventListener("submit", (event) => {
                const message = form.dataset.confirm;

                if (
                    message &&
                    !window.confirm(message)
                ) {
                    event.preventDefault();
                }
            });
        });

    document
        .querySelectorAll("[data-relative-time]")
        .forEach((element) => {
            const raw = element.dataset.relativeTime;

            if (!raw) {
                return;
            }

            const date = new Date(raw);

            if (Number.isNaN(date.getTime())) {
                return;
            }

            const now = new Date();
            const diffMs = now - date;
            const diffSeconds = Math.floor(diffMs / 1000);
            const diffMinutes = Math.floor(diffSeconds / 60);
            const diffHours = Math.floor(diffMinutes / 60);
            const diffDays = Math.floor(diffHours / 24);

            let label;

            if (diffSeconds < 30) {
                label = "Just now";

            } else if (diffMinutes < 1) {
                label = `${diffSeconds} seconds ago`;

            } else if (diffMinutes < 60) {
                label = `${diffMinutes} minute${diffMinutes === 1 ? "" : "s"} ago`;

            } else if (diffHours < 24) {
                label = `${diffHours} hour${diffHours === 1 ? "" : "s"} ago`;

            } else if (diffDays === 1) {
                label = `Yesterday at ${date.toLocaleTimeString([], {
                    hour: "numeric",
                    minute: "2-digit"
                })}`;

            } else if (diffDays < 7) {
                label = date.toLocaleDateString([], {
                    weekday: "long",
                    hour: "numeric",
                    minute: "2-digit"
                });

            } else {
                label = date.toLocaleDateString([], {
                    month: "short",
                    day: "numeric",
                    hour: "numeric",
                    minute: "2-digit"
                });
            }

            element.textContent = label;
            element.title = date.toLocaleString();
        });
});
