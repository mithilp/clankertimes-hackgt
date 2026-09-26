import { ImageResponse } from "next/og";
import { INK, PAPER, googleFont } from "@/lib/og";

export const size = { width: 180, height: 180 };
export const contentType = "image/png";

// The home-screen icon on iPhones: the same Caslon C, larger.
export default async function AppleIcon() {
  const caslon = await googleFont("Libre Caslon Display", 400, "C");
  return new ImageResponse(
    (
      <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center",
                    background: PAPER, color: INK, fontFamily: "Caslon", fontSize: 150, lineHeight: 1, paddingTop: 12 }}>C</div>
    ),
    { ...size, fonts: caslon ? [{ name: "Caslon", data: caslon, weight: 400, style: "normal" }] : [] },
  );
}
