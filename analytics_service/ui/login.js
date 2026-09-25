"use strict";
const form = document.getElementById("login-form");
const message = document.getElementById("message");
fetch("/auth/me", {credentials: "same-origin"}).then(response => { if (response.ok) location.replace("/ui/"); }).catch(() => {});
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
