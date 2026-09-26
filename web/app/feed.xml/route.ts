import { listArticles } from "@/lib/articles";
import { SITE_DESCRIPTION, SITE_NAME, absolute } from "@/lib/site";

export const revalidate = 300;

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// An RSS feed, so readers and aggregators can follow new stories.
export async function GET() {
  const items = (await listArticles()).map((a) => `
    <item>
      <title>${esc(a.headline)}</title>
      <link>${absolute(`/article/${a.slug}`)}</link>
      <guid isPermaLink="true">${absolute(`/article/${a.slug}`)}</guid>
      <pubDate>${new Date(a.published_at).toUTCString()}</pubDate>
      <description>${esc(a.dek ?? "")}</description>
    </item>`).join("");
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>${SITE_NAME}</title>
    <link>${absolute("/")}</link>
    <description>${esc(SITE_DESCRIPTION)}</description>
    <language>en-us</language>${items}
  </channel>
</rss>`;
  return new Response(xml, { headers: { "Content-Type": "application/rss+xml; charset=utf-8" } });
}
