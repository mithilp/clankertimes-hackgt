import type { Metadata, Viewport } from "next";
import { Libre_Caslon_Display, Libre_Franklin, Source_Serif_4 } from "next/font/google";
import InlineScript from "@/components/InlineScript";
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

// The browser chrome matches the paper color in either mode.
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fbfaf6" },
    { media: "(prefers-color-scheme: dark)", color: "#161513" },
  ],
  colorScheme: "light dark",
};

// A light or dark choice saved by the toggle (components/ThemeToggle.tsx), applied before the first paint.
const THEME = `(function(){try{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}catch(e){}})()`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${serif.variable} ${display.variable} ${sans.variable}`} suppressHydrationWarning>
      <head>
        <InlineScript html={THEME} />
      </head>
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
