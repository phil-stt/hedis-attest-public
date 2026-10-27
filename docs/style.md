# Attest style note

One palette for the app and for slides, so screenshots look native on both. Generic and clinical on purpose; no plan branding anywhere in the app.

## Type

- One family: Inter (system fallback: -apple-system, Segoe UI, Helvetica, Arial).
- Tabular numerals everywhere numbers appear (`font-variant-numeric: tabular-nums`).
- Sizes: body 15px, table 14px, labels 12px uppercase with 0.04em tracking, page title 22px semibold. Nothing bigger in the app.

## Neutrals

| Role | Hex |
|---|---|
| Ink (text) | `#1A1D21` |
| Muted text | `#5B6470` |
| Border | `#D9DEE3` |
| Surface (cards, panels) | `#FFFFFF` |
| Background | `#F4F6F8` |

## Semantic colors (same meaning on every screen and every slide)

| Meaning | Hex | Where it shows |
|---|---|---|
| AI finding (proposed, not yet decided) | `#2F6FDB` blue | Highlighted source span, finding badge, confidence bar |
| Human decision (accepted, record created) | `#1F8A5B` green | Accept button, "Record created" panel, accepted counts |
| Abstained (AI declined to decide) | `#C47A0A` amber | Abstain badge and reason, abstain counts in the eval panel |
| Rejected or no evidence | `#8A939E` gray | Rejected rows, "no evidence found" status |
| Error or wrong value (eval only) | `#C2362B` red | The wrong-value count in the eval panel, even when it's zero |

Rules: blue is always the machine, green is always the person, amber is always the machine saying "I don't know." Never use green for an AI finding before a human accepts it.

## Layout

- Review view: source document on the left (60%), finding on the right (40%). The highlighted span uses the blue at 15% opacity as background with a 2px solid blue left border.
- Cards: 1px border, 6px radius, no shadows.
- One accent per screen beyond the semantic colors: none. Keep it quiet.
- Density: a nurse works this 200 times a day. Rows 40px, generous horizontal padding, no decorative elements.

