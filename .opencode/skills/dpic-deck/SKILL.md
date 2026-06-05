---
name: dpic-deck
description: >
  Creates PowerPoint (.pptx) presentations in the official DPIC (Data, Policy and Innovation Centre)
  style — the Government of Odisha × University of Chicago Trust partnership. Use this skill
  whenever the user asks to create, build, or make a slide deck, presentation, or pptx for DPIC,
  Janasunani, ORTPSA, grievance redressal, or any related project. Also trigger for requests like
  "make a deck in our style", "create a progress report presentation", "build slides for the committee",
  or any presentation task where the user is working in the Grievance/DPIC/ORTPSA project context.
  Always use this skill instead of generic pptx approaches when any DPIC/government-of-Odisha context is present.
---

# DPIC Deck Skill

You are building a presentation in the official DPIC house style. The design is clean,
typography-driven, and minimal — maroon as the sole accent color on white and light-gray
backgrounds. No decorative bars, no footer bands, no gold/yellow, no navy blue.

**Before writing any code**, read `references/layouts.md` for coordinate templates for each
slide type, and `scripts/build.js` for the boilerplate you should copy and adapt.

---

## Workflow

1. Understand the slide deck's content and structure (ask if unclear)
2. Map each slide to a layout type (see `references/layouts.md`)
3. Copy `scripts/build.js` to the working directory and adapt it with the actual content
4. Run `node build.js` in the working directory (pptxgenjs must be installed: `npm install pptxgenjs`)
5. Convert to PDF and render thumbnails for QA:
   ```bash
   libreoffice --headless --convert-to pdf output.pptx
   rm -f slide-*.jpg && pdftoppm -jpeg -r 150 output.pdf slide
   ```
6. If the project has a local OpenCode command or script for Office conversion, use that instead of the generic LibreOffice command
7. Visually inspect every slide; fix any overflow, clipping, or spacing issues
8. Save the final .pptx to the user's workspace

---

## Style Specification

### Slide size
Use `LAYOUT_16x9` (10" × 5.625"). All coordinates below are in this coordinate system.

### Colors
```
MAROON   = "7B1113"   // Primary accent — titles, rules, numbered circles, table headers
DKMAROON = "800000"   // Closing slide background
DARKTEXT = "222222"   // Slide titles (bold)
BODYTEXT = "444444"   // Body text, bullet points
GRAY     = "999999"   // Secondary/footnote text
WHITE    = "FFFFFF"
OFFWHITE = "F7F7F7"   // Card and panel backgrounds
LTGRAY   = "EEEEEE"   // Subtle borders, table row dividers
```

No other colors. Never use navy, gold, yellow, or bright blue.

### Typography
Font: **Calibri** throughout (bold, italic, and regular variants).

| Element                    | Size  | Style        | Color    |
|----------------------------|-------|--------------|----------|
| Slide title                | 26pt  | Bold         | DARKTEXT |
| Title underrule            | —     | 4pt MAROON line under title |
| Section header (in card)   | 15pt  | Bold         | MAROON   |
| Body / bullet text         | 13pt  | Regular      | BODYTEXT |
| Sub-bullet / caption       | 11pt  | Italic       | GRAY     |
| Numbered item (big)        | 22pt  | Bold         | WHITE (in circle) |
| Outline number box label   | 22pt  | Bold         | WHITE    |
| Footnote                   | 11pt  | Italic       | GRAY     |
| Closing "Thank You"        | 44pt  | Bold         | WHITE    |

### Layout rules
- **No left accent bar**
- **No footer band**
- **No gold, yellow, or blue of any kind**
- Title always at top-left: `x:0.5, y:0.2, w:9.0, h:0.52`
- Maroon rule immediately under title: `x:0.5, y:0.75, w:9.0, h:0.04`
- Content area starts: `y:0.95`
- Bottom margin: leave at least `0.25"` before slide edge (y ≤ 5.375)
- Side margins: `x ≥ 0.5`, `x + w ≤ 9.5`

---

## Slide Types

See `references/layouts.md` for exact coordinates and code patterns for each type.

| Type | When to use |
|------|-------------|
| `title` | Opening slide — org name, partnership line, presentation title, date |
| `outline` | Table of contents — numbered sections |
| `two-col` | Two equal content panels side by side |
| `table` | Tabular data with maroon header row |
| `numbered-list` | Action items, recommendations, decisions — numbered circles |
| `text-block` | Single topic with body text or bullet points |
| `phase-cols` | Phased timeline (2–4 columns, each a phase) |
| `closing` | End slide — dark maroon background, "Thank You" or equivalent |

---

## Code Conventions

```javascript
// Always destructure palette at top of build.js
const MAROON   = "7B1113";
const DKMAROON = "800000";
const DARKTEXT = "222222";
const BODYTEXT = "444444";
const GRAY     = "999999";
const WHITE    = "FFFFFF";
const OFFWHITE = "F7F7F7";
const LTGRAY   = "EEEEEE";
const FONT     = "Calibri";

// Standard title block — call for every content slide
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
```

Never use `#` before hex color values — pptxgenjs will corrupt the file.

---

## QA Checklist

Before presenting the file:
- [ ] All text fully visible, no overflow
- [ ] No blue, yellow, or gold anywhere
- [ ] No footer text at bottom of slides
- [ ] Title + maroon rule present on every content slide
- [ ] Numbers, circles, and highlights use `MAROON = "7B1113"` only
- [ ] Closing slide has solid `DKMAROON = "800000"` background
