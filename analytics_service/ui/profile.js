"use strict";
const $ = id => document.getElementById(id);
const sessionChannel = typeof BroadcastChannel === "function" ? new BroadcastChannel("aml-session") : null;
async function loadProfile() {
  $("profile-content").hidden = true;
  $("profile-tables").replaceChildren(); $("profile-countries").replaceChildren();
  try {
    const response = await fetch("/auth/me", {credentials:"same-origin"});
    if (response.status === 401) { location.replace("/login"); return; }
    if (!response.ok) throw new Error("Profile unavailable");
    const profile = await response.json();
    $("profile-username").textContent = profile.username;
    $("profile-role").textContent = profile.role.charAt(0).toUpperCase() + profile.role.slice(1);
    $("table-total").textContent = `${profile.tables.length} tables`;
    $("country-total").textContent = `${profile.countries.length} countries`;
    for (const name of profile.tables) { const item = document.createElement("li"); item.textContent = name; $("profile-tables").append(item); }
    for (const country of profile.countries) { const row = document.createElement("tr"); for (const value of [country.name,country.code,country.entity]) { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); } $("profile-countries").append(row); }
    $("profile-content").hidden = false; $("message").textContent = "";
  } catch (_) { $("message").textContent = "Your profile could not be loaded. Please refresh to try again."; }
}
$("sign-out").addEventListener("click", async () => {
  $("sign-out").disabled = true;
  try {
    const response = await fetch("/auth/logout", {method:"POST",credentials:"same-origin",headers:{"X-AML-Request":"1"}});
    if (!response.ok) throw new Error("Logout failed");
    if (sessionChannel) { sessionChannel.postMessage("changed"); sessionChannel.close(); }
    location.replace("/login");
  } catch (_) { $("message").textContent = "Sign-out could not be completed. Please try again."; $("sign-out").disabled = false; }
});
if (sessionChannel) sessionChannel.onmessage = loadProfile;
loadProfile();
