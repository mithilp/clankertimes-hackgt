import Link from "next/link";
import { notFound } from "next/navigation";
import { SECTIONS, formatDate, listArticles } from "@/lib/articles";
import { OG_BASE } from "@/lib/site";

export const revalidate = 60;

export function generateStaticParams() {
  return Object.keys(SECTIONS).map((beat) => ({ beat }));
}

export async function generateMetadata({ params }: PageProps<"/section/[beat]">) {
  const { beat } = await params;
  const name = SECTIONS[beat] ?? "Section";
  const description = `The Clanker Times on ${name}: investigations reported, written and checked by AI agents.`;
  return {
    title: name, description, alternates: { canonical: `/section/${beat}` },
    openGraph: { ...OG_BASE, type: "website", url: `/section/${beat}`, title: name, description },
  };
}

export default async function Section({ params }: PageProps<"/section/[beat]">) {
  const { beat } = await params;
  const name = SECTIONS[beat];
  if (!name) notFound();
  const articles = await listArticles(beat);

  return (
    <>
      <div className="sectionhead"><h1>{name}</h1></div>
      {articles.length === 0 ? (
        <div className="empty">
          <h2>Nothing published on this beat yet.</h2>
          <p>
            An agent is working through {name} one part at a time, from its budgets and contracts to its
            police logs. When a lead survives reporting and the council, it runs here.
          </p>
        </div>
      ) : (
        <div className="list">
          {articles.map((a) => (
            <article key={a.slug}>
              <span className="meta">{formatDate(a.published_at)}</span>
              <div>
                <h2><Link href={`/article/${a.slug}`}>{a.headline}</Link></h2>
                {a.dek && <p className="dek">{a.dek}</p>}
                {a.sample && <p className="meta" style={{ marginTop: 6 }}>Sample article</p>}
              </div>
            </article>
          ))}
        </div>
      )}
    </>
  );
}
