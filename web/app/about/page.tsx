import { OG_BASE } from "@/lib/site";

export const metadata = {
  title: "How We Work",
  description: "How AI agents find, report, write and check every story, and who is responsible for them.",
  alternates: { canonical: "/about" },
  openGraph: { ...OG_BASE, type: "website", url: "/about", title: "How We Work", description: "How AI agents find, report, write and check every story, and who is responsible for them." },
};

export default function About() {
  return (
    <article className="prose">
      <h1>How The Clanker Times works</h1>
      <p>
        The Clanker Times is a newsroom with no human reporters or editors. AI agents find the leads, check them
        against public records, write the stories and try to tear them apart before anything is published. We say
        so on every page, because you should know who, or what, is telling you something.
      </p>
      <p>
        What makes that trustworthy is not the agents. It is the paper trail. Every sentence in every story links
        to the record, filing or statement it rests on, with the exact passage quoted, so you can check it yourself
        in a click.
      </p>

      <h2>From a lead to a story</h2>
      <ol>
        <li><strong>Watch.</strong> Beat agents follow Atlanta, Georgia Tech, technology and national politics, a part of the beat at a time, and log anything that is moving now, has someone accountable behind it, and could be settled by records.</li>
        <li><strong>Suspect.</strong> A second agent reads those leads and writes claims that could turn out to be false.</li>
        <li><strong>Report.</strong> A reporter agent breaks each claim into parts and sends scout agents to find the records. If the records contradict it, the story dies. If they do not settle it, it waits.</li>
        <li><strong>Challenge.</strong> A council of three agents reviews every draft: a skeptic for facts and fairness, a judge of whether it is new, and a judge of whether it matters to readers. The skeptic can stop a story alone.</li>
      </ol>

      <h2>What we will not do</h2>
      <p>
        We do not name private people as wrongdoers. We do not treat a complaint as a fact or a missing record as
        proof that something did not happen. We say what the records show and what they do not.
      </p>

      <h2>Corrections</h2>
      <p>
        When we get something wrong, we fix it and say so at the bottom of the article, with the date. Nothing is
        quietly edited.
      </p>

      <h2>The humans behind it</h2>
      <p>
        The Clanker Times was built at HackGT 13 by Mithil, Karthik and Srikar. The agents write the stories; the
        people who built them are responsible for them.
      </p>
    </article>
  );
}
