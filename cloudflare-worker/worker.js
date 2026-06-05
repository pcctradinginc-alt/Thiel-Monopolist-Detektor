/**
 * Thiel Detector — Feedback Worker
 *
 * Empfängt einen signierten Link-Klick aus der Alert-Email,
 * setzt das entsprechende Label auf dem GitHub Issue und
 * leitet den Nutzer zur Issue-Seite weiter.
 *
 * URL-Format:
 *   GET /label?issue=42&action=confirmed&sig=HMAC_SHA256
 *
 * Cloudflare Secrets (wrangler secret put):
 *   GITHUB_TOKEN   — GitHub Personal Access Token (repo scope)
 *   WEBHOOK_SECRET — beliebiger geheimer String für HMAC-Signierung
 *   GITHUB_REPO    — z.B. "pcctradinginc-alt/Thiel-Monopolist-Detektor"
 */

const VALID_ACTIONS = ["confirmed", "rejected", "watchlist", "too-early"];

const ACTION_META = {
  confirmed:  { label: "confirmed",  emoji: "✓", color: "#34c759", text: "Als bestätigt markiert" },
  rejected:   { label: "rejected",   emoji: "✗", color: "#ff3b30", text: "Als abgelehnt markiert" },
  watchlist:  { label: "watchlist",  emoji: "👁", color: "#0071e3", text: "Zur Watchlist hinzugefügt" },
  "too-early":{ label: "too-early",  emoji: "⏳", color: "#f0a500", text: "Als zu früh markiert" },
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Health check
    if (url.pathname === "/health") {
      return new Response("OK", { status: 200 });
    }

    if (url.pathname !== "/label") {
      return new Response("Not found", { status: 404 });
    }

    const issue  = url.searchParams.get("issue");
    const action = url.searchParams.get("action");
    const sig    = url.searchParams.get("sig");

    // Validate params
    if (!issue || !action || !sig) {
      return errorPage("Ungültige URL — Parameter fehlen.");
    }
    if (!VALID_ACTIONS.includes(action)) {
      return errorPage(`Ungültige Aktion: ${action}`);
    }

    // Verify HMAC signature (prevents forged links)
    const payload = `${issue}:${action}`;
    const valid = await verifyHmac(payload, sig, env.WEBHOOK_SECRET);
    if (!valid) {
      return errorPage("Ungültige Signatur — Link abgelaufen oder manipuliert.");
    }

    // Set label via GitHub API
    const repo = env.GITHUB_REPO;
    const meta = ACTION_META[action];

    try {
      // 1. Get current labels (to avoid removing existing ones)
      const currentResp = await fetch(
        `https://api.github.com/repos/${repo}/issues/${issue}/labels`,
        {
          headers: {
            Authorization: `token ${env.GITHUB_TOKEN}`,
            "User-Agent": "ThielDetector-Worker",
            Accept: "application/vnd.github.v3+json",
          },
        }
      );
      const currentLabels = await currentResp.json();
      const keepLabels = (Array.isArray(currentLabels) ? currentLabels : [])
        .map(l => l.name)
        .filter(n => !VALID_ACTIONS.includes(n)); // Remove any previous feedback label

      // 2. Set new label (replace old feedback label)
      const setResp = await fetch(
        `https://api.github.com/repos/${repo}/issues/${issue}/labels`,
        {
          method: "PUT",
          headers: {
            Authorization: `token ${env.GITHUB_TOKEN}`,
            "User-Agent": "ThielDetector-Worker",
            Accept: "application/vnd.github.v3+json",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ labels: [...keepLabels, meta.label] }),
        }
      );

      if (!setResp.ok) {
        const err = await setResp.text();
        return errorPage(`GitHub API Fehler: ${setResp.status} — ${err}`);
      }
    } catch (e) {
      return errorPage(`Netzwerkfehler: ${e.message}`);
    }

    const issueUrl = `https://github.com/${repo}/issues/${issue}`;

    // Success page (auto-redirects after 2s)
    return new Response(
      `<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="2;url=${issueUrl}">
  <title>Feedback gespeichert</title>
  <style>
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif;
           background: #f5f5f7; display: flex; align-items: center; justify-content: center;
           min-height: 100vh; }
    .card { background: white; border-radius: 16px; padding: 40px 48px; text-align: center;
            box-shadow: 0 1px 3px rgba(0,0,0,.08); max-width: 400px; }
    .emoji { font-size: 48px; margin-bottom: 12px; }
    h2 { margin: 0 0 8px; font-size: 22px; font-weight: 700; color: #1d1d1f; }
    p  { margin: 0 0 20px; font-size: 14px; color: #86868b; }
    a  { color: ${meta.color}; font-size: 13px; text-decoration: none; }
  </style>
</head>
<body>
  <div class="card">
    <div class="emoji">${meta.emoji}</div>
    <h2>${meta.text}</h2>
    <p>Issue #${issue} wurde als <strong>${action}</strong> markiert.<br>
       Du wirst weitergeleitet…</p>
    <a href="${issueUrl}">Jetzt öffnen →</a>
  </div>
</body>
</html>`,
      {
        status: 200,
        headers: { "Content-Type": "text/html;charset=UTF-8" },
      }
    );
  },
};

// ─── HMAC Verification ───────────────────────────────────────────────────────

async function verifyHmac(payload, signature, secret) {
  try {
    const encoder = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw",
      encoder.encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign", "verify"]
    );
    const sigBytes = hexToBytes(signature);
    return await crypto.subtle.verify("HMAC", key, sigBytes, encoder.encode(payload));
  } catch {
    return false;
  }
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2) {
    bytes[i / 2] = parseInt(hex.slice(i, i + 2), 16);
  }
  return bytes;
}

// ─── Error Page ───────────────────────────────────────────────────────────────

function errorPage(message) {
  return new Response(
    `<!DOCTYPE html>
<html lang="de">
<head><meta charset="UTF-8"><title>Fehler</title>
<style>body{font-family:-apple-system,sans-serif;background:#f5f5f7;display:flex;
align-items:center;justify-content:center;min-height:100vh;}
.card{background:white;border-radius:16px;padding:40px;text-align:center;
box-shadow:0 1px 3px rgba(0,0,0,.08);}
h2{color:#ff3b30;}p{color:#86868b;font-size:14px;}</style>
</head>
<body><div class="card"><h2>Fehler</h2><p>${message}</p></div></body>
</html>`,
    { status: 400, headers: { "Content-Type": "text/html;charset=UTF-8" } }
  );
}
