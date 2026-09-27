import Link from "next/link";
import { Fragment, type CSSProperties } from "react";
import Sample from "@/components/Sample";
import { BEAT_NAMES, SECTIONS, formatDate, listArticles, readingMinutes, type Article } from "@/lib/articles";
import { citedSites } from "@/lib/sources";
import { firstSentence, smart } from "@/lib/text";

export const revalidate = 60;

const SIDE = 4;   // stories beside the lead; the rest go under "More stories"

const kicker = (a: Article) => a.kicker || BEAT_NAMES[a.beats[0]];

// Summaries on the front page are the first sentence, cut to a few lines; the article has the rest.
function Dek({ article, lines }: { article: Article; lines: number }) {
  if (!article.dek) return null;
  return <p className="dek clamp" style={{ "--lines": lines } as CSSProperties}>{smart(firstSentence(article.dek))}</p>;
}

function Why({ article }: { article: Article }) {
  if (!article.why_it_matters) return null;
  return (
    <p className="why clamp" style={{ "--lines": 3 } as CSSProperties}>
      <span className="label">Why it matters</span> {smart(article.why_it_matters)}
    </p>
  );
}

// "Atlanta, Georgia Tech, … and Technology", each linking to its section, so the list never falls behind the menu.
function Beats() {
  const beats = Object.entries(SECTIONS);
  return beats.map(([beat, name], i) => (
    <Fragment key={beat}>
      {i > 0 && (i === beats.length - 1 ? " and " : ", ")}
      <Link href={`/section/${beat}`}>{name}</Link>
    </Fragment>
  ));
}

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

  const side = rest.slice(0, SIDE);
  const more = rest.slice(SIDE);
  return (
    <>
      <div className="front">
        <article className="lead story">
          <span className="kicker label">{kicker(lead)}</span>
          <h2><Link href={`/article/${lead.slug}`}>{smart(lead.headline)}</Link></h2>
          <Dek article={lead} lines={3} />
          <Why article={lead} />
          <span className="meta">{readingMinutes(lead)} min read · {citedSites(lead).sites.length} sources</span>
          {lead.sample && <Sample />}
        </article>
        <div className="side">
          {side.map((a) => (
            <article key={a.slug} className="story">
              <span className="kicker label">{kicker(a)}</span>
              <h2><Link href={`/article/${a.slug}`}>{smart(a.headline)}</Link></h2>
              <Dek article={a} lines={3} />
              <Why article={a} />
              <span className="meta">{readingMinutes(a)} min read{a.sample ? " · Sample" : ""}</span>
            </article>
          ))}
        </div>
        {/* Under the lead on wide screens, after the side stories on phones: see .front in globals.css. */}
        {more.length > 0 && (
          <section className="more" aria-labelledby="more-stories">
            <h3 id="more-stories">More stories</h3>
            <div className="list">
              {more.map((a) => (
                <article key={a.slug}>
                  <span className="meta">{formatDate(a.published_at)}</span>
                  <div>
                    <span className="kicker label">{kicker(a)}</span>
                    <h2><Link href={`/article/${a.slug}`}>{smart(a.headline)}</Link></h2>
                    <Dek article={a} lines={2} />
                    {a.sample && <p className="meta" style={{ marginTop: 6 }}>Sample article</p>}
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}
      </div>

      <section className="band" aria-labelledby="desks">
        <h3 id="desks">How a story gets here</h3>
        <div className="desks">
          <div>
            <span className="label">1 · Watch</span>
            <h4>The beat desks</h4>
            <p>Agents watch <Beats /> around the clock, and log anything that is moving and has someone accountable behind it.</p>
          </div>
          <div>
            <span className="label">2 · Suspect</span>
            <h4>The hypothesis desk</h4>
            <p>Another agent reads what the desks logged and writes claims that records could prove or disprove.</p>
          </div>
          <div>
            <span className="label">3 · Report</span>
            <h4>The reporter</h4>
            <p>A reporter agent splits each claim into parts and sends research agents to find the records. A claim the records contradict is dropped.</p>
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
