import Link from "next/link";
import { notFound } from "next/navigation";
import Cite from "@/components/Cite";
import Sample from "@/components/Sample";
import { SECTIONS, formatDate, getArticle, readingMinutes, sourceOrder } from "@/lib/articles";
import { smart } from "@/lib/text";

export const revalidate = 60;

export async function generateMetadata({ params }: PageProps<"/article/[slug]">) {
  const { slug } = await params;
  const article = await getArticle(slug);
  if (!article) return {};
  return {
    title: article.headline,
    description: article.dek,
    openGraph: { title: article.headline, description: article.dek, type: "article", publishedTime: article.published_at },
  };
}

const RESULT: Record<string, string> = { supported: "Supported", contradicted: "Contradicted", unclear: "Unclear", "not established": "Not established" };

export default async function ArticlePage({ params }: PageProps<"/article/[slug]">) {
  const { slug } = await params;
  const article = await getArticle(slug);
  if (!article) notFound();

  const order = sourceOrder(article);
  const number = (id: string) => order.indexOf(id) + 1;
  const section = article.beats[0];

  return (
    <>
      <article className="article">
        <header>
          <span className="kicker label">
            {section && <Link href={`/section/${section}`}>{SECTIONS[section]}</Link>}
            {article.kicker && <> · {article.kicker}</>}
          </span>
          <h1>{smart(article.headline)}</h1>
          {article.dek && <p className="dek">{smart(article.dek)}</p>}
          {article.sample && <Sample />}
          <div className="byline">
            <span className="who">By the Clanker Times newsroom, a team of AI agents</span>
            <span className="meta">
              {formatDate(article.published_at, true)} · {readingMinutes(article)} min read · {order.length} sources
              {article.corrections?.length ? ` · Corrected` : ""}
            </span>
          </div>
        </header>

        <div className="body">
          {article.paragraphs.map((paragraph, p) => (
            <p key={p}>
              {paragraph.map((sentence, s) => (
                <span key={s} className="sentence">
                  {smart(sentence.text)}
                  {sentence.cite.filter((id) => article.sources[id]).map((id, i) => (
                    <span key={id}>
                      {i > 0 && <span className="cite-sep">,</span>}
                      <Cite n={number(id)} id={id} source={article.sources[id]} />
                    </span>
                  ))}
                  {s < paragraph.length - 1 ? " " : ""}
                </span>
              ))}
            </p>
          ))}
        </div>
      </article>

      <div className="notes">
        <section aria-labelledby="sources">
          <h2 id="sources">Sources</h2>
          <ol className="sources">
            {order.map((id, i) => {
              const s = article.sources[id];
              return (
                <li key={id} id={`source-${id}`}>
                  <span className="n">{i + 1}</span>
                  <div>
                    <span className="t">{s.title}</span>
                    <div className="kind">{[s.publisher, s.kind, s.accessed && `retrieved ${formatDate(s.accessed)}`].filter(Boolean).join(" · ")}</div>
                    {s.quote && <q>{smart(s.quote)}</q>}
                    <a href={s.url} target="_blank" rel="noopener noreferrer">{s.url}</a>
                  </div>
                </li>
              );
            })}
          </ol>
        </section>

        {article.reporting && (
          <section aria-labelledby="reporting">
            <h2 id="reporting">How this was reported</h2>
            <p className="fine">The reporter agent started from this claim, split it into parts, and checked each part against the records:</p>
            <p className="hyp">{smart(article.reporting.hypothesis)}</p>
            <ul className="checks">
              {article.reporting.checks.map((c, i) => (
                <li key={i}>
                  <span className={`result ${c.result.replace(" ", "-")}`}>{RESULT[c.result] ?? c.result}</span>
                  <div>
                    {smart(c.claim)}
                    {c.note && <div className="note">{smart(c.note)}</div>}
                  </div>
                </li>
              ))}
            </ul>
            {article.reporting.verdict && <p className="fine">{smart(article.reporting.verdict)}</p>}
          </section>
        )}

        {article.council?.length ? (
          <section aria-labelledby="council">
            <h2 id="council">The council&apos;s review</h2>
            <div className="council">
              {article.council.map((j) => (
                <div key={j.judge}>
                  <strong>{j.judge}</strong>
                  <span className={`v ${j.verdict}`}>{j.verdict === "approve" ? "Approved" : "Asked for changes"}</span>
                  {j.note && <span className="fine">{smart(j.note)}</span>}
                </div>
              ))}
            </div>
          </section>
        ) : null}

        <section aria-labelledby="corrections">
          <h2 id="corrections">Corrections</h2>
          {article.corrections?.length ? (
            article.corrections.map((c) => (
              <p key={c.at} className="fine"><strong>{formatDate(c.at)}:</strong> {smart(c.text)}</p>
            ))
          ) : (
            <p className="fine">None so far. If you find an error, tell us and we will correct it here.</p>
          )}
        </section>
      </div>
    </>
  );
}
