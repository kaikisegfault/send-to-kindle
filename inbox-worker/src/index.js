// Email inbox for send-to-kindle.
//
// Cloudflare Email Routing hands every mail for your inbox address (e.g. kindle@your-domain) to this
// Worker, which passes it on unchanged to the engine's /inbound. The engine checks the sender (its
// allowlist plus a valid DKIM signature from the sender's own domain) and does all the work; this
// Worker only turns strangers away early and never parses the mail, so it stays inside the free plan.
//
// Secrets (wrangler secret put …): ENGINE_URL, STK_TOKEN, ALLOWED_SENDERS (comma-separated addresses).

export default {
  async email(message, env) {
    const allowed = (env.ALLOWED_SENDERS || "")
      .toLowerCase()
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (!allowed.includes(message.from.toLowerCase())) {
      message.setReject("This address only takes mail from its owner.");
      return;
    }
    // Stream the raw message through with its length known; nothing is held in memory.
    const { readable, writable } = new FixedLengthStream(message.rawSize);
    const piping = message.raw.pipeTo(writable);
    const res = await fetch(`${env.ENGINE_URL.replace(/\/$/, "")}/inbound`, {
      method: "POST",
      headers: { authorization: `Bearer ${env.STK_TOKEN}`, "content-type": "message/rfc822" },
      body: readable,
    });
    await piping;
    if (!res.ok) {
      message.setReject(`send-to-kindle is not available right now (HTTP ${res.status}).`);
    }
  },
};
