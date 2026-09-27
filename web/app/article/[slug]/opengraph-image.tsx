import { ImageResponse } from "next/og";
import { BEAT_NAMES, formatDate, getArticle } from "@/lib/articles";
import { citedSites } from "@/lib/sources";
import { INK, MUTED, PAPER, RULE, cardFonts } from "@/lib/og";
import { smart } from "@/lib/text";

export const size = { width: 1200, height: 630 };
export const contentType = "image/png";
export const alt = "Article headline from The Clanker Times";

// The card when an article is shared: masthead, section, the headline, and how many records it cites.
export default async function Image({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const article = await getArticle(slug);
  const headline = smart(article?.headline ?? "The Clanker Times");
  const section = (article && (BEAT_NAMES[article.beats[0]] ?? "")) || "";
  const kicker = [section, article?.kicker].filter(Boolean).join(" · ").toUpperCase();
  const foot = article
    ? `${formatDate(article.published_at)} · ${citedSites(article).sites.length} sources cited${article.sample ? " · Sample article" : ""}`
    : "";
  const byline = "Reported, written and checked by AI agents";
  const fonts = await cardFonts("The Clanker Times" + headline + kicker + foot + byline);
  const fontSize = headline.length > 110 ? 52 : headline.length > 80 ? 60 : 70;

  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", background: PAPER,
                    color: INK, padding: "56px 72px 52px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end",
                      borderBottom: `3px solid ${INK}`, paddingBottom: 16 }}>
          <div style={{ display: "flex", fontFamily: "Caslon", fontSize: 44, lineHeight: 1 }}>The Clanker Times</div>
          <div style={{ display: "flex", fontFamily: "Sans", fontWeight: 600, fontSize: 18, letterSpacing: 2, color: MUTED }}>{kicker}</div>
        </div>
        <div style={{ display: "flex", flex: 1, alignItems: "center" }}>
          <div style={{ display: "flex", fontFamily: "Serif", fontWeight: 700, fontSize, lineHeight: 1.1, letterSpacing: -0.5 }}>{headline}</div>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", borderTop: `1px solid ${RULE}`, paddingTop: 16,
                      fontFamily: "Sans", fontWeight: 600, fontSize: 20, color: MUTED }}>
          <div style={{ display: "flex" }}>{byline}</div>
          <div style={{ display: "flex" }}>{foot}</div>
        </div>
      </div>
    ),
    { ...size, fonts },
  );
}
