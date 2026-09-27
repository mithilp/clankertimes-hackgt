import Link from "next/link";
import Sample from "@/components/Sample";
import { BEAT_NAMES, listArticles, readingMinutes } from "@/lib/articles";
import { smart } from "@/lib/text";

export const revalidate = 60;

export default async function FrontPage() {
  const articles = await listArticles();
  const [lead, ...rest] = articles;

  if (!lead) {
    return (
      <div className="empty">
        <h2>No stories yet.</h2>
        <p>The newsroom is watching its beats. Stories appear here once a reporter confirms them and the council approves.</p>
      </div>
    );
  }

  const opening = smart(lead.paragraphs[0]?.map((s) => s.text).join(" ") ?? "");
  return (
    <>
      <div className="front">
        <article className="lead story">
          <span className="kicker label">{lead.kicker || BEAT_NAMES[lead.beats[0]]}</span>
          <h2><Link href={`/article/${lead.slug}`}>{smart(lead.headline)}</Link></h2>
          {lead.dek && <p className="dek">{smart(lead.dek)}</p>}
          {lead.why_it_matters && <p className="why"><span className="label">Why it matters</span> {smart(lead.why_it_matters)}</p>}
          {opening && <p className="opening">{opening}</p>}
          <span className="meta">{readingMinutes(lead)} min read · {Object.keys(lead.sources).length} sources</span>
          {lead.sample && <Sample />}
        </article>
        <div className="side">
          {rest.slice(0, 4).map((a) => (
            <article key={a.slug} className="story">
              <span className="kicker label">{a.kicker || BEAT_NAMES[a.beats[0]]}</span>
              <h2><Link href={`/article/${a.slug}`}>{smart(a.headline)}</Link></h2>
              {a.dek && <p className="dek">{smart(a.dek)}</p>}
              {a.why_it_matters && <p className="why"><span className="label">Why it matters</span> {smart(a.why_it_matters)}</p>}
              <span className="meta">{readingMinutes(a)} min read{a.sample ? " · Sample" : ""}</span>
            </article>
          ))}
        </div>
      </div>

      <section className="band" aria-labelledby="desks">
        <h3 id="desks">How a story gets here</h3>
        <div className="desks">
          <div>
            <span className="label">1 · Watch</span>
            <h4>The beat desks</h4>
            <p>Agents watch Atlanta, Georgia Tech, technology and national politics around the clock, and log anything that is moving and has someone accountable behind it.</p>
          </div>
          <div>
            <span className="label">2 · Suspect</span>
            <h4>The hypothesis desk</h4>
            <p>Another agent reads what the desks logged and writes claims that records could prove or disprove.</p>
          </div>
          <div>
            <span className="label">3 · Report</span>
            <h4>The reporter</h4>
            <p>A reporter agent splits each claim into parts and sends scouts to find the records. A claim the records contradict is dropped.</p>
          </div>
          <div>
            <span className="label">4 · Challenge</span>
            <h4>The council</h4>
            <p>Three judges try to break the draft: one for facts and fairness, one for whether it is new, one for whether anyone will care.</p>
          </div>
        </div>
      </section>
    </>
  );
}
