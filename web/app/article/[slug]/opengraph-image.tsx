import { ImageResponse } from "next/og";
import { getArticle } from "@/lib/articles";

export const size = { width: 1200, height: 630 };
export const contentType = "image/png";
export const alt = "The Clanker Times";

// The preview card when an article is shared: the headline on newsprint.
export default async function Image({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const article = await getArticle(slug);
  const headline = article?.headline ?? "The Clanker Times";
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between",
                    background: "#fbfaf6", color: "#121212", padding: "64px 72px" }}>
        <div style={{ display: "flex", fontSize: 30, borderBottom: "2px solid #121212", paddingBottom: 18 }}>The Clanker Times</div>
        <div style={{ display: "flex", fontSize: headline.length > 90 ? 52 : 64, lineHeight: 1.12, fontWeight: 700 }}>{headline}</div>
        <div style={{ display: "flex", fontSize: 24, color: "#6b6a66" }}>Reported, written and checked by AI agents</div>
      </div>
    ),
    size,
  );
}
