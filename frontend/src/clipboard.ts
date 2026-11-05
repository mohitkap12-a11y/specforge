/** Copy text to the clipboard. `navigator.clipboard` exists only in a secure context (HTTPS or localhost); a SpecForge
 *  server shared on the network is usually opened over plain http://<host>:8000, where it is undefined. There, and when
 *  the browser refuses the async API, fall back to selecting the text in a hidden textarea and `document.execCommand("copy")`. */
export async function copyText(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText && (typeof window === "undefined" || window.isSecureContext !== false)) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      /* refused (permissions, focus): try the fallback */
    }
  }
  legacyCopy(text);
}

function legacyCopy(text: string): void {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  const prev = document.activeElement as HTMLElement | null;
  ta.focus();
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } finally {
    ta.remove();
    prev?.focus?.();
  }
  if (!ok) throw new Error("the browser did not allow copying; select the text and copy it by hand");
}
