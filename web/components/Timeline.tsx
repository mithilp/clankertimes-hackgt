import type { Step } from "@/lib/articles";
import { smart } from "@/lib/text";

type Role = "desk" | "hunch" | "research" | "reporter" | "council" | "published" | "other";

// Which kind of agent took a step: it sets the step's marker on the rail.
function roleOf(who: string): Role {
  if (who === "Published") return "published";
  if (who === "Hunch" || who === "Hypothesis desk") return "hunch";
  if (who === "Research agent" || who === "Coverage scout" || who.startsWith("Scout")) return "research";
  if (who === "Reporter") return "reporter";
  if (who === "Council" || who === "Skeptic" || who.endsWith("judge")) return "council";
  if (/desk$/i.test(who)) return "desk";
  return "other";
}

// A step's result, shown as an icon and a word: never by color alone.
const STATUS: Record<string, [icon: string, word: string, tone: "good" | "warn" | "bad"]> = {
  confirmed: ["✓", "Confirmed", "good"],
  approved: ["✓", "Approved", "good"],
  unclear: ["?", "Unclear", "warn"],
  revise: ["↻", "Changes requested", "bad"],
  corrected: ["!", "Corrected", "warn"],
};

const ZONE = "America/New_York";
const day = (iso: string) => new Date(iso).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: ZONE });
const time = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: ZONE })
    .replace("AM", "a.m.").replace("PM", "p.m.");

// "49 min", "5h 26m", "3 days": short enough to stay on one line in a narrow tile
function span(ms: number): string {
  const min = Math.round(ms / 60000);
  if (min < 60) return `${min} min`;
  if (min < 2880) return min % 60 ? `${Math.floor(min / 60)}h ${min % 60}m` : `${min / 60}h`;
  return `${Math.round(min / 1440)} days`;
}

// How the story came together: the headline numbers, then each agent's step in order, a day at a time.
export default function Timeline({ steps, sources }: { steps: Step[]; sources?: number }) {
  const sorted = [...steps].sort((a, b) => a.at.localeCompare(b.at));
  const first = sorted[0], last = sorted.at(-1);
  const elapsed = first && last ? Date.parse(last.at) - Date.parse(first.at) : 0;
  const confirmed = sorted.filter((s) => s.result === "confirmed").length;

  const days: { day: string; steps: Step[] }[] = [];
  for (const s of sorted) {
    const d = day(s.at);
    if (days.at(-1)?.day !== d) days.push({ day: d, steps: [] });
    days.at(-1)!.steps.push(s);
  }

  return (
    <section aria-labelledby="timeline">
      <h2 id="timeline">How this story came together</h2>
      <dl className="stats">
        {elapsed > 0 && (
          <div>
            <dt>First signal to {last?.result === "corrected" ? "latest correction" : "publication"}</dt>
            <dd>{span(elapsed)}</dd>
          </div>
        )}
        {confirmed > 0 && <div><dt>Findings confirmed</dt><dd>{confirmed}</dd></div>}
        {sources != null && <div><dt>Sources cited</dt><dd><a href="#sources">{sources}</a></dd></div>}
      </dl>
      <ol className="tl">
        {days.map((d) => (
          <li key={d.day}>
            <h3>{d.day}</h3>
            <ol>
              {d.steps.map((s, i) => {
                const status = s.result ? STATUS[s.result] : undefined;
                return (
                  <li key={i} className={`step r-${roleOf(s.who)}${status?.[2] === "good" ? " ok" : ""}`}>
                    <time dateTime={s.at}>{time(s.at)}</time>
                    <span className="node" aria-hidden="true" />
                    <div>
                      <p className="step-head">
                        <span className="who">{s.who}</span>
                        {status && (
                          <span className={`status ${status[2]}`}>
                            <span className="i" aria-hidden="true">{status[0]}</span>{status[1]}
                          </span>
                        )}
                      </p>
                      {s.text && <p className="step-text">{smart(s.text)}</p>}
                    </div>
                  </li>
                );
              })}
            </ol>
          </li>
        ))}
      </ol>
    </section>
  );
}
