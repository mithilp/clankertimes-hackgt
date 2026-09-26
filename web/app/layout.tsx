import type { Metadata } from "next";
import { Libre_Caslon_Display, Libre_Franklin, Source_Serif_4 } from "next/font/google";
import Masthead from "@/components/Masthead";
import Footer from "@/components/Footer";
import { OG_BASE, SITE_DESCRIPTION, SITE_NAME, SITE_URL } from "@/lib/site";
import "./globals.css";

const serif = Source_Serif_4({ subsets: ["latin"], variable: "--font-serif", axes: ["opsz"], style: ["normal", "italic"] });
const display = Libre_Caslon_Display({ subsets: ["latin"], weight: "400", variable: "--font-display" });
const sans = Libre_Franklin({ subsets: ["latin"], variable: "--font-sans" });

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: { default: SITE_NAME, template: `%s | ${SITE_NAME}` },
  description: SITE_DESCRIPTION,
  applicationName: SITE_NAME,
  alternates: { types: { "application/rss+xml": [{ url: "/feed.xml", title: SITE_NAME }] } },
  openGraph: { ...OG_BASE, type: "website", url: "/", title: SITE_NAME, description: SITE_DESCRIPTION },
  twitter: { card: "summary_large_image", title: SITE_NAME, description: SITE_DESCRIPTION },
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
