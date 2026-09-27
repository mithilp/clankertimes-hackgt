// Typographer's quotes and apostrophes: agents write straight ones.
export function smart(text: string): string {
  return text
    .replace(/(^|[\s([{—-])"/g, "$1“")
    .replace(/"/g, "”")
    .replace(/(^|[\s([{—-])'/g, "$1‘")
    .replace(/'/g, "’");
}

// Words a period can follow without ending the sentence: "U.S. Bank", "Oct. 1", "Gov. Kemp".
const ABBREVIATIONS = new Set([
  "mr", "mrs", "ms", "dr", "gov", "sen", "rep", "st", "inc", "corp", "co", "ltd", "jr", "sr", "no", "nos",
  "vs", "etc", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
  "u.s", "u.n", "a.m", "p.m", "ga", "fla", "pa", "n.j", "n.y",
]);

// The first sentence of a summary, for cards and lists. The article page keeps the whole summary.
export function firstSentence(text: string): string {
  const end = /[.!?]["”’)]*\s+(?=["“‘(]?[A-Z0-9$])/g;
  for (let m = end.exec(text); m; m = end.exec(text)) {
    const word = (text.slice(0, m.index).split(/\s/).pop() ?? "").replace(/^["“‘(]+/, "").toLowerCase();
    if (ABBREVIATIONS.has(word) || /^[a-z]$/.test(word)) continue;   // an abbreviation or an initial
    return text.slice(0, m.index + m[0].trimEnd().length);
  }
  return text;
}
