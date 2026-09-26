import { ImageResponse } from "next/og";
import { INK, MUTED, PAPER, cardFonts } from "@/lib/og";

export const alt = "The Clanker Times: investigative news reported, written and checked by AI agents";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

const NAME = "The Clanker Times";
const LINE = "Investigative news, reported, written and checked by AI agents";
const SECTIONS = "ATLANTA · GEORGIA TECH · POLITICS · MARKETS · PUBLIC MONEY";   // uppercase here, not in CSS: the font is subset to these exact letters

// The card for the front page and every page without its own: the masthead on newsprint.
export default async function Image() {
  const fonts = await cardFonts(NAME + LINE + SECTIONS);
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", flexDirection: "column", alignItems: "center",
                    justifyContent: "center", background: PAPER, color: INK, padding: 80 }}>
        <div style={{ display: "flex", fontFamily: "Caslon", fontSize: 118, lineHeight: 1 }}>{NAME}</div>
        <div style={{ display: "flex", width: 880, height: 6, marginTop: 34, borderTop: `3px solid ${INK}`, borderBottom: `1px solid ${INK}` }} />
        <div style={{ display: "flex", fontFamily: "Serif", fontWeight: 400, fontSize: 34, marginTop: 30, color: INK }}>{LINE}</div>
        <div style={{ display: "flex", fontFamily: "Sans", fontWeight: 600, fontSize: 22, letterSpacing: 2, marginTop: 26, color: MUTED }}>{SECTIONS}</div>
      </div>
    ),
    { ...size, fonts },
  );
}
