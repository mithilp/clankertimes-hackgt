import { ImageResponse } from "next/og";
import { INK, PAPER, googleFont } from "@/lib/og";

export const size = { width: 64, height: 64 };
export const contentType = "image/png";

// The browser-tab icon: a Caslon C on newsprint.
export default async function Icon() {
  const caslon = await googleFont("Libre Caslon Display", 400, "C");
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center",
                    background: PAPER, color: INK, fontFamily: "Caslon", fontSize: 58, lineHeight: 1, paddingTop: 4,
                    border: `2px solid ${INK}`, borderRadius: 10 }}>C</div>
    ),
    { ...size, fonts: caslon ? [{ name: "Caslon", data: caslon, weight: 400, style: "normal" }] : [] },
  );
}
