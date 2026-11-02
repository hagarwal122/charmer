# Charmer brand (C19)

Made with the `taste-skill:brandkit` method (strategy, then symbol, then system), delivered as code: this session had
no image generator, so the board is `board.html`, rendered from the same `tokens.css` the dashboard uses.

## Strategy

| | |
| --- | --- |
| Category | Developer infrastructure: a self-hosted LLM gateway |
| Audience | Developers who run their own stack |
| Promise | Many small free tiers, coaxed into one calm endpoint, never a bill |
| Personality | Precise, frugal, calm, a little clever |
| Core metaphor | The charmer draws many voices into one tune |
| Avoid | Neon AI glow, robot imagery, money imagery (it is free), OpenRouter's identity |

## Mark

An open **C** built from four separate arc segments, one per provider, opening to the right where requests enter;
a single **charm rose** dot at the centre is the request being served. Construction: circle r = 11 on a 32 grid,
opening ±40° around 0°, four 61° arcs with 12° gaps, stroke 3.2, round caps; dot r = 2.8. `mark.svg` uses
`currentColor` for the arcs, so it follows the text colour; the dot is always the accent.

Wordmark: "charmer" in the sans stack, semibold, lowercase, tracking −0.01em, set beside the mark at the cap height.

Tagline: **Every free tier, in tune.**

## System

- **Colour.** Near-black canvas, three surface steps, thin borders. One accent, charm rose (`--c-accent`), for the
  active page, focus and the one number that matters. Status colours are used only for state, always with a word.
  The light theme is designed, not inverted: darker accent and status tones for AA contrast on white.
- **Type.** System sans for UI, system mono for ids, models and numbers in tables. Nothing is fetched from a CDN, so
  a self-hosted install works offline. Numbers use tabular figures.
- **Space, radii, elevation, motion.** 4 px steps; radii 6/10/14; one inset hairline plus a soft shadow; 120 ms and
  200 ms with one ease; motion off under `prefers-reduced-motion`.
- **Status vocabulary.** serving, near limit, filtered, cooling, pulled, no key.

The tokens live in `dashboard/static/tokens.css`; no other file may hold a literal colour, font, size or shadow
(DSH-13).
