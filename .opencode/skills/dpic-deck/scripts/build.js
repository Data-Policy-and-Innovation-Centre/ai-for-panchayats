/**
 * DPIC Deck Builder — Boilerplate
 * Copy this file, adapt SLIDES array with your content, then run:
 *   npm install pptxgenjs
 *   node build.js
 *
 * Output: output.pptx in the current directory.
 */

const pptxgen = require("pptxgenjs");

// ── Palette ──────────────────────────────────────────────────────────────────
const MAROON   = "7B1113";
const DKMAROON = "800000";
const DARKTEXT = "222222";
const BODYTEXT = "444444";
const GRAY     = "999999";
const WHITE    = "FFFFFF";
const OFFWHITE = "F7F7F7";
const LTGRAY   = "EEEEEE";
const FONT     = "Calibri";

// ── Presentation setup ───────────────────────────────────────────────────────
let pres = new pptxgen();
pres.layout = "LAYOUT_16x9";  // 10" × 5.625"
pres.title  = "DPIC Presentation";
pres.author = "DPIC";

// ── Shared helpers ───────────────────────────────────────────────────────────

/** Add bold title + maroon underrule to a content slide. */
function addTitle(slide, text) {
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

/** Helper to build a plain table cell. */
function cell(text, bg, opts = {}) {
  return {
    text,
    options: {
      fill:    { color: bg },
      color:   opts.color   || BODYTEXT,
      bold:    opts.bold    || false,
      italic:  opts.italic  || false,
      fontFace: FONT,
      fontSize: opts.fontSize || 13,
      align:   opts.align   || "left",
      valign:  "middle",
      margin:  [4, 8, 4, 8],
    }
  };
}

/** Header cell (maroon background, white text). */
function hdrCell(text, opts = {}) {
  return cell(text, MAROON, { color:WHITE, bold:true, ...opts });
}

// ── Slide builders ───────────────────────────────────────────────────────────

function titleSlide({ title, date, tag }) {
  let s = pres.addSlide();
  s.background = { color: WHITE };

  s.addShape(pres.shapes.RECTANGLE, { x:0, y:0, w:10, h:0.1, fill:{color:MAROON}, line:{color:MAROON} });
  s.addShape(pres.shapes.RECTANGLE, { x:0, y:5.525, w:10, h:0.1, fill:{color:MAROON}, line:{color:MAROON} });

  s.addText("Data, Policy and Innovation Centre", {
    x:1.0, y:1.9, w:8.0, h:0.65,
    fontFace:FONT, fontSize:30, bold:true, color:MAROON,
    align:"center", charSpacing:2, margin:0
  });
  s.addText("A Government of Odisha and University of Chicago Trust Partnership", {
    x:1.0, y:2.6, w:8.0, h:0.35,
    fontFace:FONT, fontSize:14, italic:true, color:BODYTEXT, align:"center", margin:0
  });
  s.addText(title, {
    x:1.0, y:3.15, w:8.0, h:0.65,
    fontFace:FONT, fontSize:22, bold:true, color:DARKTEXT, align:"center", margin:0
  });
  s.addText(date, {
    x:1.0, y:3.9, w:8.0, h:0.32,
    fontFace:FONT, fontSize:13, color:BODYTEXT, align:"center", margin:0
  });
  if (tag) {
    s.addText(tag, {
      x:1.0, y:4.28, w:8.0, h:0.3,
      fontFace:FONT, fontSize:13, italic:true, color:GRAY, align:"center", margin:0
    });
  }
}

function outlineSlide({ title, items }) {
  // items: [{ num, text, subtitle? }]
  let s = pres.addSlide();
  s.background = { color: WHITE };
  addTitle(s, title || "Outline");

  const rowH  = items.some(i => i.subtitle) ? 0.88 : 0.72;
  const startY = 0.98;
  const gap   = 0.06;

  items.forEach((item, i) => {
    const y = startY + i * (rowH + gap);
    s.addShape(pres.shapes.RECTANGLE, { x:0.5, y, w:9.0, h:rowH, fill:{color:OFFWHITE}, line:{color:OFFWHITE} });
    s.addShape(pres.shapes.RECTANGLE, { x:0.5, y, w:0.9, h:rowH, fill:{color:MAROON}, line:{color:MAROON} });
    s.addText(item.num, { x:0.5, y, w:0.9, h:rowH, fontFace:FONT, fontSize:22, bold:true, color:WHITE, align:"center", valign:"middle", margin:0 });
    s.addText(item.text, { x:1.55, y:y+(item.subtitle ? 0.06 : 0), w:7.8, h:item.subtitle ? 0.44 : rowH, fontFace:FONT, fontSize:19, color:DARKTEXT, align:"left", valign:"middle", margin:[0,0,0,8] });
    if (item.subtitle) {
      s.addText(item.subtitle, { x:1.55, y:y+0.52, w:7.8, h:0.28, fontFace:FONT, fontSize:11, italic:true, color:GRAY, align:"left", margin:[0,0,0,8] });
    }
  });
}

function numberedListSlide({ title, items }) {
  // items: [{ num, header, body }]
  let s = pres.addSlide();
  s.background = { color: WHITE };
  addTitle(s, title);

  const N     = items.length;
  const rowH  = N <= 3 ? 1.3 : (N === 4 ? 1.02 : 0.85);
  const startY = 0.98;
  const gap   = 0.05;

  items.forEach((item, i) => {
    const y = startY + i * (rowH + gap);
    s.addShape(pres.shapes.RECTANGLE, { x:0.5, y, w:9.0, h:rowH, fill:{color:OFFWHITE}, line:{color:OFFWHITE} });
    s.addShape(pres.shapes.OVAL, { x:0.62, y:y+(rowH-0.58)/2, w:0.58, h:0.58, fill:{color:MAROON}, line:{color:MAROON} });
    s.addText(item.num, { x:0.62, y:y+(rowH-0.58)/2, w:0.58, h:0.58, fontFace:FONT, fontSize:22, bold:true, color:WHITE, align:"center", valign:"middle", margin:0 });
    s.addText(item.header, { x:1.4, y:y+0.1, w:7.95, h:0.36, fontFace:FONT, fontSize:13, bold:true, color:DARKTEXT, align:"left", valign:"middle", margin:0 });
    s.addText(item.body,   { x:1.4, y:y+0.5, w:7.95, h:rowH-0.58, fontFace:FONT, fontSize:12, color:BODYTEXT, align:"left", valign:"top", margin:0 });
  });
}

function twoColSlide({ title, left, right }) {
  // left/right: { header, body (string or string[]), secondary? }
  let s = pres.addSlide();
  s.background = { color: WHITE };
  addTitle(s, title);

  const panelY = 0.98;
  const panelH = 4.4;
  const panelW = 4.45;

  function addPanel(x, panel) {
    s.addShape(pres.shapes.RECTANGLE, { x, y:panelY, w:panelW, h:panelH, fill:{color:OFFWHITE}, line:{color:OFFWHITE} });
    s.addText(panel.header, { x:x+0.15, y:panelY+0.12, w:panelW-0.3, h:0.35, fontFace:FONT, fontSize:15, bold:true, color:MAROON, align:"left", margin:0 });

    const bodyIsArray = Array.isArray(panel.body);
    if (bodyIsArray) {
      const bulletItems = panel.body.map((b, bi) => ({ text:b, options:{ bullet:true, breakLine: bi < panel.body.length-1 } }));
      s.addText(bulletItems, { x:x+0.15, y:panelY+0.55, w:panelW-0.3, h:panelH-0.8, fontFace:FONT, fontSize:13, color:BODYTEXT, align:"left", valign:"top", margin:0 });
    } else {
      s.addText(panel.body, { x:x+0.15, y:panelY+0.55, w:panelW-0.3, h:panel.secondary ? panelH-1.5 : panelH-0.8, fontFace:FONT, fontSize:16, color:DARKTEXT, align:"left", valign:"middle", margin:0 });
    }

    if (panel.secondary) {
      s.addShape(pres.shapes.RECTANGLE, { x:x+0.15, y:panelY+panelH-0.9, w:panelW-0.3, h:0.02, fill:{color:LTGRAY}, line:{color:LTGRAY} });
      s.addText(panel.secondary, { x:x+0.15, y:panelY+panelH-0.84, w:panelW-0.3, h:0.7, fontFace:FONT, fontSize:11, italic:true, color:GRAY, align:"left", valign:"top", margin:0 });
    }
  }

  addPanel(0.5, left);
  addPanel(0.5 + panelW + 0.1, right);
}

function closingSlide({ text }) {
  let s = pres.addSlide();
  s.background = { color: DKMAROON };
  s.addText(text || "Thank You", {
    x:1.0, y:2.0, w:8.0, h:1.5,
    fontFace:FONT, fontSize:44, bold:true, color:WHITE,
    align:"center", valign:"middle", margin:0
  });
}

// ── EXAMPLE DECK (replace with your content) ─────────────────────────────────

titleSlide({
  title: "Your Presentation Title Here",
  date:  "Month DD, YYYY",
  tag:   "Ongoing Work",  // remove if not needed
});

outlineSlide({
  items: [
    { num:"01", text:"First section" },
    { num:"02", text:"Second section" },
    { num:"03", text:"Third section" },
  ]
});

twoColSlide({
  title: "Background",
  left: {
    header: "Left Panel Header",
    body:   "Main text for the left panel goes here.",
    secondary: "Supporting context or status note here."
  },
  right: {
    header: "Right Panel Header",
    body:   ["First bullet point", "Second bullet point", "Third bullet point"]
  }
});

numberedListSlide({
  title: "For the Committee's Consideration",
  items: [
    { num:"1", header:"First action item", body:"Description of what is being asked and why." },
    { num:"2", header:"Second action item", body:"Description here." },
    { num:"3", header:"Third action item", body:"Description here." },
  ]
});

closingSlide({ text: "Thank You" });

// ── Write file ────────────────────────────────────────────────────────────────
pres.writeFile({ fileName: "output.pptx" })
  .then(() => console.log("✓ output.pptx written"))
  .catch(e => { console.error(e); process.exit(1); });
