for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const input = document.getElementById(button.dataset.copy);
    const status = button.parentElement.nextElementSibling;
    let value = input.value;
    if (button.hasAttribute("data-private-link")) {
      const link = new URL(location.pathname, location.origin);
      link.hash = value;
      value = link.href;
    }
    try {
      await navigator.clipboard.writeText(value);
      status.textContent = "Link copied.";
    } catch {
      input.select();
      status.textContent = button.hasAttribute("data-private-link")
        ? "Copy the selected private code."
        : "Copy the selected link.";
    }
  });
}

const fragment = location.hash.slice(1);
if (/^[A-Za-z0-9_-]{43}$/.test(fragment)) {
  history.replaceState(null, "", location.pathname + location.search);
  const restore = document.getElementById("restore");
  if (restore) {
    restore.elements.token.value = fragment;
    restore.requestSubmit();
  }
}
