import { setTimeout as sleep } from "node:timers/promises";
import { ScriptedProvider, type Provider, type ProviderRequest } from "../../tinyagents/typescript/sdk.ts";

/**
 * The offline model: a canned build that needs no key and no network.
 *
 * The first prompt of a session gets three versions of one page (structure,
 * then styling, then behaviour). Every later prompt gets one more version in a
 * different palette. What the prompt says is ignored.
 *
 * The script keeps no position of its own. Which turn to play is read from
 * the history the SDK sends with each request, so a server that was killed
 * and restarted answers a resumed run exactly where the dead one stopped.
 */
export function scriptedProvider(typingDelay = 28): Provider {
  return {
    async *stream(request) {
      const turn = turnFor(request);
      const words = (turn.text ?? "").match(/\S+\s*/g) ?? [];
      // ScriptedProvider plays a fixed list once, so each request gets its own one-turn script.
      const inner = new ScriptedProvider([{ ...turn, deltas: words }]).stream(request);
      while (true) {
        const next = await inner.next();
        if (next.done) return next.value;
        if (typingDelay > 0) await sleep(typingDelay, undefined, { signal: request.signal });
        yield next.value;
      }
    },
  };
}

/** Scripted mode's slow step: the second publish of a session. Later revisions get half of it. */
export function scriptedDelay(slow: number): (version: number) => number {
  return (version) => (version === 2 ? slow : version > 3 ? Math.round(slow / 2) : Math.min(900, slow));
}

interface Turn {
  text?: string;
  toolCalls?: { callId: string; name: string; arguments: Record<string, unknown> }[];
}

function turnFor(request: ProviderRequest): Turn {
  const prompts = request.history.filter((item) => item.role === "user").length;
  const lastPrompt = request.history.findLastIndex((item) => item.role === "user");
  const step = request.history.slice(lastPrompt + 1).filter((item) => item.role === "tool_result").length;
  const publish = (text: string, title: string, summary: string, html: string): Turn => ({
    text,
    toolCalls: [{ callId: `scripted_${prompts}_${step}`, name: "publish_design", arguments: { title, summary, html } }],
  });

  if (prompts <= 1) {
    if (step === 0) return publish(
      "I'll put the bare structure up first, so there is something in the preview straight away.",
      "Structure", "Unstyled markup: header, three cabins, a booking section.", page(1, 0));
    if (step === 1) return publish(
      "The structure is published. Next comes type, colour and layout. This is the slow step: " +
      "the publish is held open for a few seconds, which makes it a good moment to kill the server.",
      "Type and colour", "A typographic scale, a warm palette and a three-column grid.", page(2, 0));
    if (step === 2) return publish(
      "Styling is in. One more pass for the price calculator and the narrow-screen layout.",
      "Calculator and small screens", "A working nightly-price calculator and a single-column layout below 640px.", page(3, 0));
    return { text: "Done. Three versions are published: structure, styling, and the finished page with its " +
      "calculator. Use the version tabs to compare them, or send another prompt for a revision." };
  }
  const palette = PALETTES[(prompts - 1) % PALETTES.length];
  if (step === 0) return publish(
    `Here is a revision in the ${palette.name} palette. Scripted mode only knows this one change; ` +
    "with an API key the model would follow your instructions.",
    `${palette.name[0].toUpperCase()}${palette.name.slice(1)} palette`,
    `The finished page recoloured in the ${palette.name} palette.`, page(3, (prompts - 1) % PALETTES.length));
  return { text: `Published the ${palette.name} revision as a new version. The earlier versions are still there.` };
}

const PALETTES = [
  { name: "pine", paper: "#f4f1e8", ink: "#1f2a24", soft: "#5c6a61", accent: "#2f6b4f", card: "#fffdf7", line: "#d9d4c5" },
  { name: "clay", paper: "#f7efe8", ink: "#2b1d17", soft: "#75584b", accent: "#b4532a", card: "#fffaf5", line: "#e3d2c5" },
  { name: "night", paper: "#12161c", ink: "#e8ecf2", soft: "#93a0b3", accent: "#7fb4ff", card: "#1a2029", line: "#2a3340" },
  { name: "plum", paper: "#f4eef4", ink: "#261a2b", soft: "#6c5874", accent: "#7a3b8f", card: "#fdf9fd", line: "#decfe3" },
];

const CABINS = [
  ["Heron", "Sleeps two. A wood stove, a desk at the window, and the tide flats at the door.", 140],
  ["Alder", "Sleeps four. Two rooms under one roof, a covered porch, an outdoor shower.", 195],
  ["Kelp", "Sleeps six. The long table, the big kitchen, and the best view of the three.", 260],
] as const;

/** The canned page at one of three stages: 1 markup only, 2 styled, 3 styled with behaviour. */
function page(stage: 1 | 2 | 3, paletteIndex: number): string {
  const p = PALETTES[paletteIndex];
  const css = stage === 1 ? "" : `<style>
  :root { --paper: ${p.paper}; --ink: ${p.ink}; --soft: ${p.soft}; --accent: ${p.accent}; --card: ${p.card}; --line: ${p.line}; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--paper); color: var(--ink); font: 17px/1.55 Georgia, "Iowan Old Style", serif; }
  header, main, footer { max-width: 62rem; margin: 0 auto; padding: 0 1.5rem; }
  header { padding-top: 4.5rem; padding-bottom: 3rem; }
  .eyebrow { font: 600 0.72rem/1 ui-sans-serif, system-ui, sans-serif; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent); }
  h1 { font-size: clamp(2.4rem, 6vw, 4.2rem); line-height: 1.02; letter-spacing: -0.02em; margin: 1rem 0 1.2rem; font-weight: 500; max-width: 14ch; }
  header p { max-width: 38rem; color: var(--soft); font-size: 1.1rem; margin: 0; }
  h2 { font-size: 1.5rem; font-weight: 500; margin: 0 0 1.2rem; }
  .cabins { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; padding: 0; margin: 0 0 3.5rem; list-style: none; }
  .cabins li { background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 1.3rem 1.3rem 1.1rem; }
  .cabins h3 { margin: 0 0 0.4rem; font-size: 1.25rem; font-weight: 500; }
  .cabins p { margin: 0 0 1rem; color: var(--soft); font-size: 0.97rem; }
  .cabins strong { font: 600 0.85rem ui-sans-serif, system-ui, sans-serif; color: var(--accent); }
  .book { border-top: 1px solid var(--line); padding: 2.5rem 0 3rem; }
  .book p { color: var(--soft); max-width: 36rem; }
  footer { border-top: 1px solid var(--line); padding-top: 1.2rem; padding-bottom: 2.5rem; color: var(--soft); font: 0.8rem ui-sans-serif, system-ui, sans-serif; }
${stage === 3 ? `  .calc { display: grid; grid-template-columns: 1fr 1fr auto; gap: 1rem; align-items: end; margin-top: 1.5rem; max-width: 40rem; }
  label { display: grid; gap: 0.35rem; font: 600 0.72rem ui-sans-serif, system-ui, sans-serif; letter-spacing: 0.08em; text-transform: uppercase; color: var(--soft); }
  select, input { font: 1rem ui-sans-serif, system-ui, sans-serif; padding: 0.6rem 0.7rem; border: 1px solid var(--line); border-radius: 5px; background: var(--card); color: var(--ink); width: 100%; }
  select:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  output { font-size: 2rem; font-variant-numeric: tabular-nums; color: var(--accent); min-width: 6ch; text-align: right; line-height: 1.2; }
  @media (max-width: 640px) {
    header { padding-top: 2.5rem; padding-bottom: 2rem; }
    .cabins, .calc { grid-template-columns: 1fr; }
    output { text-align: left; }
  }
` : ""}</style>`;
  const booking = stage === 3 ? `<div class="calc">
      <label>Cabin
        <select id="cabin">${CABINS.map(([name, , price]) => `<option value="${price}">${name}</option>`).join("")}</select>
      </label>
      <label>Nights
        <input id="nights" type="number" min="2" max="21" value="3">
      </label>
      <output id="total" aria-live="polite"></output>
    </div>` : `<p>Prices are per night. Two nights minimum.</p>`;
  const script = stage === 3 ? `<script>
  const cabin = document.getElementById("cabin"), nights = document.getElementById("nights"), total = document.getElementById("total");
  const update = () => {
    const count = Math.min(21, Math.max(2, Number(nights.value) || 2));
    total.textContent = "$" + (Number(cabin.value) * count).toLocaleString("en-US");
  };
  cabin.addEventListener("input", update);
  nights.addEventListener("input", update);
  update();
</script>` : "";
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tide &amp; Timber</title>
${css}
</head>
<body>
  <header>
    <div class="eyebrow">Tide &amp; Timber &middot; three cabins on the inlet</div>
    <h1>Stay where the road runs out.</h1>
    <p>Three small cabins at the edge of a tidal inlet, forty minutes from the nearest traffic light. No wifi in the cabins. Good coffee in all of them.</p>
  </header>
  <main>
    <h2>The cabins</h2>
    <ul class="cabins">
${CABINS.map(([name, text, price]) => `      <li><h3>${name}</h3><p>${text}</p><strong>$${price} a night</strong></li>`).join("\n")}
    </ul>
    <section class="book">
      <h2>Work out a stay</h2>
      <p>Pick a cabin and a number of nights. Booking opens on the first of each month for the month after.</p>
      ${booking}
    </section>
  </main>
  <footer>Tide &amp; Timber is a made-up place. This page was built by TinyAgent's scripted demo.</footer>
${script}
</body>
</html>
`;
}
