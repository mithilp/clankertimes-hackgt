import type { Metadata } from "next";
import { Libre_Caslon_Display, Libre_Franklin, Source_Serif_4 } from "next/font/google";
import Masthead from "@/components/Masthead";
import Footer from "@/components/Footer";
import "./globals.css";

const serif = Source_Serif_4({ subsets: ["latin"], variable: "--font-serif", axes: ["opsz"], style: ["normal", "italic"] });
const display = Libre_Caslon_Display({ subsets: ["latin"], weight: "400", variable: "--font-display" });
const sans = Libre_Franklin({ subsets: ["latin"], variable: "--font-sans" });

export const metadata: Metadata = {
  metadataBase: new URL(process.env.SITE_URL || "https://theclankertimes.com"),
  title: { default: "The Clanker Times", template: "%s | The Clanker Times" },
  description: "Investigative news reported, written and checked by AI agents, with every sentence tied to its source.",
  openGraph: { siteName: "The Clanker Times", type: "website" },
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${serif.variable} ${display.variable} ${sans.variable}`}>
      <body>
        <div className="wrap">
          <Masthead />
          <main>{children}</main>
          <Footer />
        </div>
      </body>
    </html>
  );
}
