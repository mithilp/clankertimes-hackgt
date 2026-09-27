import type { Step } from "@/lib/articles";
import { smart } from "@/lib/text";

const LABEL: Record<string, [string, string]> = {
  confirmed: ["Confirmed", "good"],
  unclear: ["Unclear", "warn"],
  revise: ["Changes requested", "bad"],
  approved: ["Approved", "good"],
};

const ZONE = "America/New_York";
const day = (iso: string) => new Date(iso).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: ZONE });
const time = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: ZONE })
    .replace("AM", "a.m.").replace("PM", "p.m.");

// How the story came together: each agent's step, in order, with the result where there was one.
export default function Timeline({ steps }: { steps: Step[] }) {
  const sorted = [...steps].sort((a, b) => a.at.localeCompare(b.at));
  const first = sorted[0], last = sorted.at(-1);
  const hours = first && last ? Math.round((Date.parse(last.at) - Date.parse(first.at)) / 36e5) : 0;
  let shownDay = "";
  return (
    <section aria-labelledby="timeline">
      <h2 id="timeline">How this story came together</h2>
      {hours > 0 && (
        <p className="fine">
          From the first signal to {last?.result === "corrected" ? "the latest correction" : "publication"}: {hours < 48 ? `${hours} hours` : `${Math.round(hours / 24)} days`}, {sorted.filter((s) => s.who === "Research agent" || s.who.startsWith("Scout")).length} findings confirmed by research agents, and signed off by the council before publication.
        </p>
      )}
      <ol className="tl">
        {sorted.map((s, i) => {
          const d = day(s.at);
          const header = d !== shownDay ? (shownDay = d) : null;
          const chip = s.result ? LABEL[s.result] : undefined;
          const key = s.result === "published" || s.result === "confirmed" || s.result === "approved";
          return (
            <li key={i} className={`${key ? "key " : ""}${header ? "newday" : ""}`}>
              {header && <span className="tlday">{header}</span>}
              <time dateTime={s.at}>{time(s.at)}</time>
              <span className="dot" aria-hidden="true" />
              <div className="tlbody">
                <span className="who">{s.who}</span>
                {s.text && <span>{smart(s.text)}{chip && <> <span className={`chip ${chip[1]}`}>{chip[0]}</span></>}</span>}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
