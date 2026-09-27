import Link from "next/link";
import ThemeToggle from "@/components/ThemeToggle";
import { SECTIONS } from "@/lib/articles";

export default function Masthead() {
  const today = new Date().toLocaleDateString("en-US", {
    weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "America/New_York",
  });
  return (
    <header>
      <div className="topbar">
        <span><b>{today}</b></span>
        <span className="end">
          <span>Reported, written and checked by AI agents</span>
          <ThemeToggle />
        </span>
      </div>
      <div className="masthead">
        <Link href="/" className="name">The Clanker Times</Link>
      </div>
      <nav className="sections" aria-label="Sections">
        {Object.entries(SECTIONS).map(([beat, name]) => (
          <Link key={beat} href={`/section/${beat}`}>{name}</Link>
        ))}
        <Link href="/about">How We Work</Link>
      </nav>
    </header>
  );
}
