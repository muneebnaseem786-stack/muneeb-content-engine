// Cloudflare Worker — Telegram webhook → GitHub repository_dispatch relay.
//
// Telegram POSTs every update (button tap, message) to this Worker.
// The Worker fires a `repository_dispatch` event at GitHub, which kicks off
// the telegram_webhook.yml workflow. End-to-end latency: ~5-15s vs the old
// 5-min cron.
//
// Required Worker secrets (set in Cloudflare dashboard → Settings → Variables):
//   GITHUB_PAT     — fine-grained PAT with "Contents: Read & Write" on the repo
//   GITHUB_REPO    — e.g. "muneebnaseem786/muneeb-content-engine"
//   WEBHOOK_SECRET — random string; must match the ?secret=... query on setWebhook
//
// Deploy: paste this into a new Worker on dash.cloudflare.com → Workers & Pages.

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    // Verify Telegram is the caller (we set this secret in setWebhook URL)
    const url = new URL(request.url);
    if (url.searchParams.get("secret") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    // Fire-and-forget the GitHub dispatch — respond to Telegram immediately
    // (Telegram retries if we take >60s or return non-2xx).
    const payload = await request.json().catch(() => ({}));

    const ghResp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GITHUB_PAT}`,
          "Accept":        "application/vnd.github+json",
          "User-Agent":    "telegram-webhook-relay",
          "Content-Type":  "application/json",
        },
        body: JSON.stringify({
          event_type:     "telegram_update",
          client_payload: { update_id: payload.update_id ?? null },
        }),
      }
    );

    if (!ghResp.ok) {
      console.log("github dispatch failed", ghResp.status, await ghResp.text());
    }

    return new Response("OK", { status: 200 });
  },
};
