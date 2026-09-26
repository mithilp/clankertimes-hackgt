// Fonts and pieces shared by the generated share cards and icons. Fonts come from Google Fonts,
// subset to just the characters the card uses, so each card stays small. If the fetch fails the
// card still renders in the default font.
export async function googleFont(family: string, weight: number, text: string): Promise<ArrayBuffer | null> {
  try {
    const css = await (await fetch(
      `https://fonts.googleapis.com/css2?family=${encodeURIComponent(family)}:wght@${weight}&text=${encodeURIComponent(text)}`,
    )).text();
    const url = css.match(/src: url\((.+?)\) format\('(opentype|truetype)'\)/)?.[1];
    return url ? await (await fetch(url)).arrayBuffer() : null;
  } catch {
    return null;
  }
}

type Font = { name: string; data: ArrayBuffer; weight: 400 | 600 | 700; style: "normal" };

export async function cardFonts(text: string): Promise<Font[]> {
  const wanted: [string, string, 400 | 600 | 700][] = [
    ["Caslon", "Libre Caslon Display", 400],
    ["Serif", "Source Serif 4", 700],
    ["Serif", "Source Serif 4", 400],
    ["Sans", "Libre Franklin", 600],
  ];
  const loaded = await Promise.all(wanted.map(async ([name, family, weight]) => {
    const data = await googleFont(family, weight, text);
    return data ? { name, data, weight, style: "normal" as const } : null;
  }));
  return loaded.filter((f): f is Font => f !== null);
}

export const PAPER = "#fbfaf6";
export const INK = "#121212";
export const MUTED = "#6b6a66";
export const RULE = "#dedbd2";
