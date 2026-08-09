# Hover interaction — window cells, month strip, value columns

One shared behaviour on every cell that carries a datapoint. Piano-key feel: the cell
**travels** on hover (no easing, two hard steps) and a panel **snaps** in above it.

## 1. Keyframes — add once

```css
@keyframes cckey{0%{transform:translateY(0)}100%{transform:translateY(-3px)}}
@keyframes cctip{0%{opacity:0;transform:translateY(4px)}40%{opacity:1;transform:translateY(0)}100%{opacity:1;transform:translateY(0)}}
```

Non-negotiable:
- `steps(2,end)` / `steps(3,end)`, never `ease` or `linear`. The motion must arrive in
  discrete frames — that is what makes it feel mechanical rather than web-smooth.
- `cctip` animates **opacity and translateY only**. It must never contain a horizontal
  translate, or it will fight the edge clamping in §4.

## 2. State — one key for the whole page

```js
state = { key: null };            // e.g. 'Thu2', 'm17', 'v29' — null when nothing is hovered
```

A single hover key across all three charts, so only one panel is ever open. Each cell gets
`onMouseEnter={() => setKey(id)}` and `onMouseLeave={() => setKey(null)}`.

## 3. Structure — three nested elements per cell

```
<div>                            position:relative — the hit area, owns the mouse handlers
  <div>                          the visible cell — animation: cckey when hovered
  {hovered && (
    <div>                        positioning wrapper — owns left/right, pointer-events:none
      <div>                      the panel — animation: cctip
```

Do not collapse the two tooltip divs into one. The outer one positions, the inner one
animates. Merging them is what caused the panels to fly off-screen.

## 4. Edge clamping — derive from the index, do not hardcode `left:50%`

A month cell is ~13px wide and its panel is 156px, so a centred panel on cell 0 sits 32px
past the left edge of the window. Compute alignment per cell:

```js
const edge =
  i < N        ? 'left:0;right:auto'                    // first N cells: flush left
: i > len-N-1  ? 'left:auto;right:0'                    // last N cells: flush right
:                'left:50%;transform:translateX(-50%)'; // everything else: centred
```

N = 7 for the month strip (60 cells), 4 for value absorbed (30 cells), 1 for the week grid
(4 wide cells per row). Apply `edge` to the **positioning wrapper**, not the panel.

## 5. Cell state while hovered

```js
face:  on ? '#2b2620' : 'transparent'                   // key face darkens
press: on ? 'cckey 0.09s steps(2,end) forwards' : 'none'
```

`animation-fill-mode:forwards` holds the lifted position. The week grid also reserves
`padding-bottom:4px` on the hit area so the 3px lift does not clip.

## 6. Panel contents — same shape everywhere

Line 1: when + the headline number, coloured by the severity scale.
Line 2: a 10-block meter of the same value.
Line 3: absolute tokens against the cap.
Line 4: the leading model, with its identity stripe.
Line 5: the consequence — `RAN DRY` / `TIGHT` / `4.2M UNUSED`.

Value absorbed drops lines 2 and 4 (dollars have no cap and no model).

## 7. Reference implementation — week grid

Markup:

```html
          <sc-for list="{{ row.cells }}" as="c" hint-placeholder-count="4">
            <div onMouseEnter="{{ c.enter }}" onMouseLeave="{{ c.leave }}" style="flex:1;position:relative;padding-bottom:4px">
              <div style="display:flex;gap:2px;align-items:center;height:20px;border:2px solid {{ c.edge }};padding:2px;background:{{ c.face }};animation:{{ c.press }};animation-fill-mode:forwards;cursor:default">
                <div style="width:6px;height:12px;background:{{ c.led }}"></div>
                <sc-for list="{{ c.blocks }}" as="b" hint-placeholder-count="7">
                  <div style="flex:1;height:12px;background:{{ b.bg }}"></div>
                </sc-for>
```

Per-cell values:

```js
    const grid = raw.map(([day, arr]) => {
      const cells = [0, 1, 2, 3].map(i => {
        const p = arr[i];
        if (p === undefined) return { edge: 'var(--l0)', led: 'transparent', blocks: meter(0, 7, 'var(--l0)'), on: false, face: 'transparent', press: 'none', tipEdge: 'left:50%;transform:translateX(-50%)' };
        const who = p >= 95 ? 'fable' : (i === 0 && p < 40) ? 'sonnet' : 'opus';
        ledCount[who]++;
        const led = 'var(--' + who + ')';
        const id = day + i, on = hv === id;
        const edge = i === 0 ? 'left:0;right:auto' : i === 3 ? 'left:auto;right:0' : 'left:50%;transform:translateX(-50%)';
        const tok = (CAP * p / 100).toFixed(1);
        return {
          edge: p >= 95 ? 'var(--l4)' : 'transparent', tipEdge: edge, led, blocks: meter(p, 7, lvl(p)),
          face: on ? '#2b2620' : 'transparent',
          press: on ? 'cckey 0.09s steps(2,end) forwards' : 'none',
          on, enter: () => this.setState({ key: id }), leave: () => this.setState({ key: null }),
          tipWhen: day.toUpperCase() + ' ' + SLOT[i], tipPct: p + '%', tipColor: lvl(p),
          tipCells: meter(p, 10, lvl(p)),
          tipTokens: tok + 'M OF ' + CAP + 'M',
          tipModel: 'LED BY ' + who.toUpperCase(),
          tipNote: p >= 95 ? 'RAN DRY — WORK QUEUED' : p >= 75 ? 'TIGHT — LITTLE ROOM LEFT' : (CAP - tok).toFixed(1) + 'M UNUSED'
        };
      });

```

The month strip and value columns follow the same pattern with `m`/`v` id prefixes and the
N values from §4.
