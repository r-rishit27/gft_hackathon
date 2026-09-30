"use strict";
const form = document.getElementById("login-form");
const message = document.getElementById("message");
fetch("/auth/me", {credentials: "same-origin"}).then(response => { if (response.ok) location.replace("/ui/"); }).catch(() => {});

// Public demo credentials (also published in the README). Kept in memory, not in the DOM:
// the page shows a mask, and both the Copy button and a manual copy of the mask yield the real value.
const DEMO_LOGINS = [["monitoring", "c7MjHxLKHeDxA3UCeIaPP0XA"], ["investigation", "PeSg4lVR9Ga4zkrEo2ku_XmP"], ["admin", "wiAwcn62tsc8dtExhZmtih9v"]];
const secretOf = new WeakMap();

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch {
    const area = document.createElement("textarea");
    area.value = text; area.setAttribute("readonly", ""); area.className = "sr-only";
    document.body.append(area); area.select();
    const ok = document.execCommand("copy"); area.remove(); return ok;
  }
}

function smallButton(label, ariaLabel, onClick) {
  const button = document.createElement("button");
  button.type = "button"; button.textContent = label; button.setAttribute("aria-label", ariaLabel);
  button.addEventListener("click", onClick);
  return button;
}

const demoBody = document.getElementById("demo-logins-body");
for (const [username, password] of DEMO_LOGINS) {
  const row = document.createElement("tr");
  const userCell = document.createElement("td"); const code = document.createElement("code"); code.textContent = username; userCell.append(code);
  const secretCell = document.createElement("td"); const secret = document.createElement("span");
  secret.className = "demo-secret"; secret.textContent = "************"; secret.setAttribute("aria-label", `Hidden password for ${username}`);
  secretOf.set(secret, password); secretCell.append(secret);
  const actions = document.createElement("td"); const wrap = document.createElement("div"); wrap.className = "demo-actions";
  const copy = smallButton("Copy", `Copy password for ${username}`, async () => {
    const ok = await copyText(password);
    copy.textContent = ok ? "Copied" : "Copy failed"; message.textContent = ok ? `Password for ${username} copied.` : "Could not copy; use the Fill button instead.";
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  });
  const fill = smallButton("Fill", `Fill the form as ${username}`, () => {
    document.getElementById("username").value = username; document.getElementById("password").value = password;
    message.textContent = `Filled in as ${username}. Select Sign in to continue.`; document.getElementById("sign-in").focus();
  });
  wrap.append(copy, fill); actions.append(wrap);
  row.append(userCell, secretCell, actions); demoBody.append(row);
}

// Selecting the masked "************" and copying it (Ctrl+C / context menu) copies the real password.
document.addEventListener("copy", event => {
  const selection = document.getSelection();
  const secretFor = node => (node && (node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement)?.closest(".demo-secret")) || null;
  const secret = selection && (secretFor(selection.anchorNode) || secretFor(selection.focusNode));
  if (!secret || !secretOf.has(secret)) return;
  event.clipboardData.setData("text/plain", secretOf.get(secret));
  event.preventDefault();
});
form.addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.getElementById("sign-in");
  const password = document.getElementById("password");
  button.disabled = true; button.textContent = "Signing in..."; message.textContent = "";
  try {
    const response = await fetch("/auth/login", {method: "POST", credentials: "same-origin", headers: {"Content-Type":"application/json", "X-AML-Request":"1"}, body:JSON.stringify({username:document.getElementById("username").value.trim(),password:password.value})});
    password.value = "";
    if (!response.ok) throw new Error(response.status === 401 ? "Username or password is incorrect." : response.status === 429 ? "Too many sign-in attempts. Please wait a minute and try again." : "Sign-in is temporarily unavailable. Please try again.");
    if (typeof BroadcastChannel === "function") { const channel = new BroadcastChannel("aml-session"); channel.postMessage("changed"); channel.close(); }
    location.replace("/ui/");
  } catch (error) { message.textContent = error.message === "Failed to fetch" ? "The service could not be reached. Please try again." : error.message; }
  finally { password.value = ""; button.disabled = false; button.textContent = "Sign in"; }
});
