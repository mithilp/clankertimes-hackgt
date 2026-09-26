// Typographer's quotes and apostrophes: agents write straight ones.
export function smart(text: string): string {
  return text
    .replace(/(^|[\s([{\u2014-])"/g, "$1\u201c")
    .replace(/"/g, "\u201d")
    .replace(/(^|[\s([{\u2014-])'/g, "$1\u2018")
    .replace(/'/g, "\u2019");
}
