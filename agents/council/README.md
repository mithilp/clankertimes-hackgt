# Council

Three judges read every finished draft independently. Any one of them can send it back to the Reporter with a specific, fixable reason.

| Judge | Asks |
|---|---|
| [skeptic](skeptic/) | Is every claim true, fairly framed, and defensible? |
| [virality](virality/) | Will people care about this and share it? |
| [novelty](novelty/) | Is this actually new? |

## How they combine

- **The skeptic can block; virality cannot overrule it.** A story that would go viral and isn't solid is the most dangerous thing this newsroom can publish.
- **Each judge returns a verdict and reasons.** `approve`, or `revise` with reasons the Reporter can act on. A bare "no" is useless.
- **Mechanical checks run before the council.** Uncited sentences, quotes not found in their sources, leaked template text, an office as the subject of a criminal verb: `newsroom/article.py` catches those without spending a council round.
- **One revision loop per story**, then the Reporter parks it if the council still objects. Endless back-and-forth usually means the hypothesis was wrong, not the prose.

## Measuring the council

The council is the easiest agent to measure honestly: plant known errors into good drafts and count what each judge catches. See [skeptic/examples/bad](skeptic/examples/bad/) for the seed set.
