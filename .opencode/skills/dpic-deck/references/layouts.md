# DPIC Slide Layout Reference

All coordinates are for `LAYOUT_16x9` (10" × 5.625").
Copy the code patterns below and substitute your content.

---

## Shared helper: `addTitle(slide, pres, text)`

Every content slide starts with this. Add it once at the top of your build.js and call it on each slide.

```javascript
function addTitle(slide, pres, text) {
  slide.addText(text, {
    x:0.5, y:0.2, w:9.0, h:0.52,
    fontFace:FONT, fontSize:26, bold:true, color:DARKTEXT,
    align:"left", valign:"middle", margin:0
  });
  slide.addShape(pres.shapes.RECTANGLE, {
    x:0.5, y:0.75, w:9.0, h:0.04,
    fill:{color:MAROON}, line:{color:MAROON}
  });
}
// Content area starts at y=0.95
```

---

## Layout: `title`

Opening slide — white background, org name, partnership tagline, presentation title, date.
No logos unless you have the image paths.

```javascript
let s = pres.addSlide();
s.background = { color: WHITE };

// Thin top red strip
s.addShape(pres.shapes.RECTANGLE, { x:0, y:0, w:10, h:0.1, fill:{color:MAROON}, line:{color:MAROON} });
// Thin bottom red strip
s.addShape(pres.shapes.RECTANGLE, { x:0, y:5.525, w:10, h:0.1, fill:{color:MAROON}, line:{color:MAROON} });

// Org name (large, bold, spaced)
s.addText("Data, Policy and Innovation Centre", {
  x:1.0, y:1.9, w:8.0, h:0.65,
  fontFace:FONT, fontSize:30, bold:true, color:MAROON,
  align:"center", charSpacing:2, margin:0
});

// Partnership tagline (italic)
s.addText("A Government of Odisha and University of Chicago Trust Partnership", {
  x:1.0, y:2.6, w:8.0, h:0.35,
  fontFace:FONT, fontSize:14, italic:true, color:BODYTEXT,
  align:"center", margin:0
});

// Presentation title (bold, dark)
s.addText("YOUR PRESENTATION TITLE HERE", {
  x:1.0, y:3.2, w:8.0, h:0.65,
  fontFace:FONT, fontSize:22, bold:true, color:DARKTEXT,
  align:"center", margin:0
});

// Date
s.addText("Month DD, YYYY", {
  x:1.0, y:3.95, w:8.0, h:0.35,
  fontFace:FONT, fontSize:13, color:BODYTEXT,
  align:"center", margin:0
});

// Optional tag (e.g., "Ongoing Work", "Draft")
s.addText("Ongoing Work", {
  x:1.0, y:4.35, w:8.0, h:0.3,
  fontFace:FONT, fontSize:13, italic:true, color:GRAY,
  align:"center", margin:0
});
```

---

## Layout: `outline`

Table of contents. Each item = a maroon numbered box + light-gray row with description.

```javascript
let s = pres.addSlide();
s.background = { color: WHITE };
addTitle(s, pres, "Outline");

const items = [
  { num:"01", text:"First section title" },
  { num:"02", text:"Second section title" },
  { num:"03", text:"Third section title" },
  // max ~5 items comfortably
];

const rowH  = 0.72;
const startY = 0.98;
const gap   = 0.08;

items.forEach((item, i) => {
  const y = startY + i * (rowH + gap);
  // Light gray row background
  s.addShape(pres.shapes.RECTANGLE, {
    x:0.5, y, w:9.0, h:rowH, fill:{color:OFFWHITE}, line:{color:OFFWHITE}
  });
  // Maroon number box
  s.addShape(pres.shapes.RECTANGLE, {
    x:0.5, y, w:0.9, h:rowH, fill:{color:MAROON}, line:{color:MAROON}
  });
  s.addText(item.num, {
    x:0.5, y, w:0.9, h:rowH,
    fontFace:FONT, fontSize:22, bold:true, color:WHITE,
    align:"center", valign:"middle", margin:0
  });
  // Item text
  s.addText(item.text, {
    x:1.55, y, w:7.8, h:rowH,
    fontFace:FONT, fontSize:19, color:DARKTEXT,
    align:"left", valign:"middle", margin:[0,0,0,8]
  });
});
```

For an item with a sub-line (italic caption below), increase rowH to 0.88 and add:
```javascript
s.addText(subText, {
  x:1.55, y:y+0.44, w:7.8, h:0.3,
  fontFace:FONT, fontSize:11, italic:true, color:GRAY,
  align:"left", margin:[0,0,0,8]
});
```

---

## Layout: `two-col`

Two side-by-side panels. Each panel has a maroon section header, optional rule, and body text/bullets.

```javascript
let s = pres.addSlide();
s.background = { color: WHITE };
addTitle(s, pres, "Slide Title");

const panelY = 0.98;
const panelH = 4.4;   // adjust if footnote needed
const panelW = 4.45;
const gap    = 0.1;

// LEFT panel
s.addShape(pres.shapes.RECTANGLE, {
  x:0.5, y:panelY, w:panelW, h:panelH, fill:{color:OFFWHITE}, line:{color:OFFWHITE}
});
s.addText("Left Panel Header", {
  x:0.65, y:panelY+0.12, w:panelW-0.3, h:0.35,
  fontFace:FONT, fontSize:15, bold:true, color:MAROON, align:"left", margin:0
});
s.addText("Left panel body text here.", {
  x:0.65, y:panelY+0.55, w:panelW-0.3, h:panelH-0.8,
  fontFace:FONT, fontSize:13, color:BODYTEXT, align:"left", valign:"top", margin:0
});

// RIGHT panel
const rx = 0.5 + panelW + gap;
s.addShape(pres.shapes.RECTANGLE, {
  x:rx, y:panelY, w:panelW, h:panelH, fill:{color:OFFWHITE}, line:{color:OFFWHITE}
});
s.addText("Right Panel Header", {
  x:rx+0.15, y:panelY+0.12, w:panelW-0.3, h:0.35,
  fontFace:FONT, fontSize:15, bold:true, color:MAROON, align:"left", margin:0
});
// Bullets in right panel:
s.addText([
  { text:"First bullet point",   options:{ bullet:true, breakLine:true } },
  { text:"Second bullet point",  options:{ bullet:true, breakLine:true } },
  { text:"Third bullet point",   options:{ bullet:true } },
], {
  x:rx+0.15, y:panelY+0.55, w:panelW-0.3, h:panelH-0.8,
  fontFace:FONT, fontSize:13, color:BODYTEXT, align:"left", valign:"top", margin:0
});
```

---

## Layout: `table`

Clean data table — maroon header row, alternating white/OFFWHITE rows, optional footnote.

```javascript
let s = pres.addSlide();
s.background = { color: WHITE };
addTitle(s, pres, "Slide Title");

// colW must sum to 9.0
const tableData = [
  // Header row
  [
    { text:"Column 1", options:{ fill:{color:MAROON}, color:WHITE, bold:true, fontFace:FONT, fontSize:13, align:"left", margin:[4,8,4,8] } },
    { text:"Column 2", options:{ fill:{color:MAROON}, color:WHITE, bold:true, fontFace:FONT, fontSize:13, align:"left", margin:[4,8,4,8] } },
    { text:"Column 3", options:{ fill:{color:MAROON}, color:WHITE, bold:true, fontFace:FONT, fontSize:13, align:"left", margin:[4,8,4,8] } },
  ],
  // Data rows — alternate OFFWHITE and WHITE
  [ cellVal("Row 1 Col 1", WHITE), cellVal("Row 1 Col 2", WHITE), cellVal("Row 1 Col 3", WHITE) ],
  [ cellVal("Row 2 Col 1", OFFWHITE), cellVal("Row 2 Col 2", OFFWHITE), cellVal("Row 2 Col 3", OFFWHITE) ],
];

// Helper for data cells:
function cellVal(text, bg) {
  return { text, options:{ fill:{color:bg}, color:BODYTEXT, fontFace:FONT, fontSize:13, align:"left", valign:"middle", margin:[4,8,4,8] } };
}
// For a bold value in a cell (e.g., a count): override bold:true and color:MAROON in options

s.addTable(tableData, {
  x:0.5, y:1.0, w:9.0,
  colW:[3.5, 1.5, 4.0],   // adjust to content
  border:{ pt:0.5, color:LTGRAY },
  autoPage:false,
});

// Optional footnote
s.addText("† Footnote text here.", {
  x:0.5, y:5.1, w:9.0, h:0.4,
  fontFace:FONT, fontSize:11, italic:true, color:GRAY,
  align:"left", margin:0
});
```

---

## Layout: `numbered-list`

For action items, recommendations, or "for the committee's consideration" slides.
Each item: large maroon circle with number + bold header + body text, on a light-gray row.

```javascript
let s = pres.addSlide();
s.background = { color: WHITE };
addTitle(s, pres, "Slide Title");

const items = [
  { num:"1", header:"First action item header", body:"Description of what this means and what is being asked." },
  { num:"2", header:"Second action item",        body:"Description here." },
  { num:"3", header:"Third action item",         body:"Description here." },
  { num:"4", header:"Fourth action item",        body:"Description here." },
];

const rowH  = 1.02;
const startY = 0.98;
const gap   = 0.05;

items.forEach((item, i) => {
  const y = startY + i * (rowH + gap);

  // Row background
  s.addShape(pres.shapes.RECTANGLE, {
    x:0.5, y, w:9.0, h:rowH, fill:{color:OFFWHITE}, line:{color:OFFWHITE}
  });

  // Maroon circle
  s.addShape(pres.shapes.OVAL, {
    x:0.62, y:y+0.22, w:0.58, h:0.58,
    fill:{color:MAROON}, line:{color:MAROON}
  });
  s.addText(item.num, {
    x:0.62, y:y+0.22, w:0.58, h:0.58,
    fontFace:FONT, fontSize:22, bold:true, color:WHITE,
    align:"center", valign:"middle", margin:0
  });

  // Header (bold)
  s.addText(item.header, {
    x:1.4, y:y+0.1, w:7.95, h:0.36,
    fontFace:FONT, fontSize:13, bold:true, color:DARKTEXT,
    align:"left", valign:"middle", margin:0
  });

  // Body text
  s.addText(item.body, {
    x:1.4, y:y+0.5, w:7.95, h:0.44,
    fontFace:FONT, fontSize:12, color:BODYTEXT,
    align:"left", valign:"top", margin:0
  });
});
```

For 3 items, increase rowH to 1.3 to give more breathing room.
For 5+ items, reduce fontSize to 11 and rowH to 0.85.

---

## Layout: `text-block`

Single large text area — for "request to committee", quotes, or extended narrative.

```javascript
let s = pres.addSlide();
s.background = { color: WHITE };
addTitle(s, pres, "Slide Title");

// Main text block (large, centered vertically in content area)
s.addText("Your main text here — can be a direct ask, a key finding, or a short narrative paragraph. Keep it short enough to be readable at a glance.", {
  x:0.8, y:1.1, w:8.4, h:3.0,
  fontFace:FONT, fontSize:21, color:DARKTEXT,
  align:"left", valign:"middle", margin:0
});

// Optional note or context line at bottom
s.addText("Supporting note or caveat here.", {
  x:0.5, y:4.5, w:9.0, h:0.5,
  fontFace:FONT, fontSize:13, color:GRAY,
  align:"left", valign:"middle", margin:0
});
```

---

## Layout: `phase-cols`

Phased timeline — 2 to 4 columns, each a phase with a number, timeline label, header, bullets, and output.

```javascript
let s = pres.addSlide();
s.background = { color: WHITE };
addTitle(s, pres, "Slide Title");

// Optional subtitle line
s.addText("Timeline is contingent on data receipt", {
  x:0.5, y:0.82, w:9.0, h:0.28,
  fontFace:FONT, fontSize:12, color:GRAY, italic:true, align:"left", margin:0
});

const phases = [
  {
    num:"01", period:"Month 1", header:"Phase Name",
    bullets:["Bullet one","Bullet two","Bullet three"],
    output:"Output: Description of deliverable"
  },
  {
    num:"02", period:"Month 2", header:"Phase Name",
    bullets:["Bullet one","Bullet two","Bullet three"],
    output:"Output: Description"
  },
  {
    num:"03", period:"Month 2–3", header:"Phase Name",
    bullets:["Bullet one","Bullet two","Bullet three"],
    output:"Output: Description"
  },
];

const N      = phases.length;
const colW   = (9.0 - (N-1)*0.1) / N;
const startX = 0.5;
const colY   = 1.18;
const colH   = 4.15;

phases.forEach((ph, i) => {
  const cx = startX + i * (colW + 0.1);

  // Column background (very subtle)
  s.addShape(pres.shapes.RECTANGLE, {
    x:cx, y:colY, w:colW, h:colH, fill:{color:OFFWHITE}, line:{color:OFFWHITE}
  });

  // Number + period header row
  s.addShape(pres.shapes.RECTANGLE, {
    x:cx, y:colY, w:colW, h:0.58, fill:{color:MAROON}, line:{color:MAROON}
  });
  s.addText(ph.num, {
    x:cx+0.08, y:colY, w:0.5, h:0.58,
    fontFace:FONT, fontSize:20, bold:true, color:WHITE, align:"left", valign:"middle", margin:0
  });
  s.addText(ph.period, {
    x:cx+0.62, y:colY, w:colW-0.72, h:0.58,
    fontFace:FONT, fontSize:12, color:"FADADD", align:"left", valign:"middle", margin:0
  });

  // Phase header
  s.addText(ph.header, {
    x:cx+0.1, y:colY+0.66, w:colW-0.2, h:0.38,
    fontFace:FONT, fontSize:15, bold:true, color:MAROON, align:"left", margin:0
  });

  // Bullets
  const bulletItems = ph.bullets.map((b, bi) => ({
    text: b,
    options: { bullet:true, breakLine: bi < ph.bullets.length-1 }
  }));
  s.addText(bulletItems, {
    x:cx+0.1, y:colY+1.1, w:colW-0.2, h:2.3,
    fontFace:FONT, fontSize:12, color:BODYTEXT, align:"left", valign:"top", margin:0
  });

  // Output line
  s.addShape(pres.shapes.RECTANGLE, {
    x:cx+0.1, y:colY+3.48, w:colW-0.2, h:0.02, fill:{color:LTGRAY}, line:{color:LTGRAY}
  });
  s.addText(ph.output, {
    x:cx+0.1, y:colY+3.55, w:colW-0.2, h:0.52,
    fontFace:FONT, fontSize:11, italic:true, color:GRAY, align:"left", valign:"top", margin:0
  });
});
```

---

## Layout: `closing`

Dark maroon full-bleed background, centered white text. Use for the final slide.

```javascript
let s = pres.addSlide();
s.background = { color: DKMAROON };

s.addText("Thank You", {
  x:1.0, y:2.0, w:8.0, h:1.5,
  fontFace:FONT, fontSize:44, bold:true, color:WHITE,
  align:"center", valign:"middle", margin:0
});
```

Optionally add a contact email or website below in a smaller, non-bold white font.
