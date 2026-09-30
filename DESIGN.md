---
name: rás
description: A cinema foyer control room for a shared household's media pipeline.
colors:
  petrol-wall: "#102328"
  petrol-panel: "#193036"
  petrol-recess: "#0c1c21"
  petrol-hover: "#284148"
  petrol-surface: "#213940"
  ivory-text: "#f5f0e5"
  mist-text: "#c2d1cc"
  quiet-text: "#a8bdb8"
  amber-action: "#e0ad60"
  amber-ink: "#18272a"
  sea-glass: "#a6cbc4"
  sea-glass-success: "#a3d4b6"
  amber-warning: "#f0c377"
  coral-error: "#ffa79d"
  lightbox-paper: "#f1ebdc"
  lightbox-ink: "#173036"
  lightbox-muted: "#5b6d69"
  lightbox-rule: "#c7c9b8"
  date-amber: "#9b622c"
  daylight-wall: "#e9e7dd"
  daylight-panel: "#fffcf5"
  daylight-lightbox-paper: "#fffaf0"
  daylight-recess: "#dedfd3"
  daylight-text: "#183237"
  daylight-muted: "#3f5959"
  daylight-amber: "#80501a"
  daylight-sea-glass: "#365f5b"
  daylight-success: "#235442"
  daylight-error: "#a23e38"
typography:
  display:
    fontFamily: '"Barlow Condensed", "Arial Narrow", sans-serif'
    fontSize: "clamp(32px, 3vw, 46px)"
    fontWeight: 500
    lineHeight: 0.95
  headline:
    fontFamily: '"Barlow Condensed", "Arial Narrow", sans-serif'
    fontSize: "34px"
    fontWeight: 500
    lineHeight: 1
  title:
    fontFamily: '"Barlow Condensed", "Arial Narrow", sans-serif'
    fontSize: "22px"
    fontWeight: 500
    lineHeight: 1
  body:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif'
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "-0.006em"
  label:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif'
    fontSize: "14px"
    fontWeight: 600
  mono:
    fontFamily: 'ui-monospace, "Cascadia Code", "JetBrains Mono", Consolas, monospace'
    fontSize: "13px"
    fontWeight: 400
rounded:
  compact: "4px"
  panel: "10px"
  dialog: "14px"
  pill: "999px"
components:
  button-primary:
    backgroundColor: "{colors.amber-action}"
    textColor: "{colors.amber-ink}"
    rounded: "{rounded.compact}"
    padding: "9px 22px"
    height: "40px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ivory-text}"
    rounded: "{rounded.compact}"
    padding: "9px 16px"
    height: "40px"
  button-danger:
    backgroundColor: "transparent"
    textColor: "{colors.coral-error}"
    rounded: "{rounded.compact}"
    padding: "9px 16px"
    height: "40px"
  card:
    backgroundColor: "{colors.petrol-panel}"
    textColor: "{colors.ivory-text}"
    rounded: "{rounded.panel}"
  schedule-panel:
    backgroundColor: "{colors.lightbox-paper}"
    textColor: "{colors.lightbox-ink}"
    rounded: "{rounded.panel}"
  chip-selected:
    backgroundColor: "{colors.amber-action}"
    textColor: "{colors.amber-ink}"
    rounded: "{rounded.pill}"
    padding: "6px 14px"
  input:
    backgroundColor: "{colors.petrol-panel}"
    textColor: "{colors.ivory-text}"
    rounded: "{rounded.compact}"
    padding: "9px 12px"
---

# Design System: rás

## Overview

**Creative North Star: "The Living Showtime Board"**

rás gives a shared household an at-a-glance view of upcoming media and live operations. The interface borrows the materials of a cinema foyer: a deep petrol wall behind luminous ivory listings, deliberate amber date and action accents, and quiet sea glass status. The result is a practical control room with an editorial showtime rhythm.

The design repeats that rhythm across navigation, schedule listings, calendar cells, queue rows, and forms. Poster artwork retains its own color. Information stays dense and legible, with rules and tonal surfaces doing most of the grouping. The established three-stream mark and `rás` wordmark anchor the rail.

**Key Characteristics:**

- The schedule is the brightest material; operational panels sit on petrol tones.
- Condensed display type gives dates and titles an immediate reading order; plain sans-serif carries controls and explanations.
- Amber identifies dates and selected actions, while status colors retain their own meanings.
- The month calendar, agenda, waiting shelf, and explicit operational controls share one visual language.

## Colors

The dark theme is the base composition. Light mode changes the surrounding wall and controls, while the ivory schedule material remains recognizable.

### Primary

- **Petrol Wall** (`petrol-wall`): the default app canvas; the deeper recess and raised panel tones separate the rail and work areas.
- **Amber Action** (`amber-action`): selected navigation, primary actions, and active emphasis. Date numerals inside the ivory schedule use the deeper `date-amber` for contrast.

### Secondary

- **Sea Glass** (`sea-glass`): the second stroke in the three-stream mark and a cool companion to amber. `sea-glass-success` is reserved for successful or healthy states.

### Tertiary

- **Coral Error** (`coral-error`): faults and destructive action cues. Amber warning has a separate token and meaning.

### Neutral

- **Ivory Text** (`ivory-text`): foreground on dark petrol surfaces, with mist and quiet tones for progressively less prominent information.
- **Lightbox Paper** (`lightbox-paper`): the showtime listing surface, paired with its own ink, muted copy, and fine rule tokens.
- **Daylight Wall** (`daylight-wall`): light-theme canvas, with matching panel, brighter lightbox paper, recess, text, amber, sea glass, success, and error values in the frontmatter.

**The Material Boundary Rule.** Use the ivory lightbox for schedule reading; use petrol surfaces and their light-theme counterparts for operational controls.

**The Status Meaning Rule.** Amber can draw attention to a date or decision, but success, warning, and failure keep distinct colors and labels.

## Typography

**Display Font:** Barlow Condensed, served locally, with Arial Narrow and sans-serif fallbacks.

**Body Font:** Segoe UI Variable Text, Segoe UI, system UI, and sans-serif.

**Mono Font:** The system monospace stack, led by `ui-monospace`, for timestamps, technical details, and logs.

Condensed type creates the showtime character without making dense data hard to read. Body copy and controls keep familiar UI proportions and sentence case.

### Hierarchy

- **Display:** the schedule heading and large date numerals; sizes respond to viewport width, with the date much larger than surrounding copy.
- **Headline:** page titles in the sticky top bar.
- **Title:** card headings, section headings, and item titles.
- **Body:** descriptions, metadata, settings, and state explanations.
- **Label:** buttons and short actionable controls, with clearly readable weight.
- **Mono:** machine details, time, countdowns, logs, and candidate names.

**The Numbers Lead Rule.** Release dates and status counts use condensed type and tabular numerals; supporting facts stay smaller and quieter.

## Layout

The shell uses a compact sticky rail (`216px`) and a flexible main column. A sticky top bar names the current route; content has generous horizontal gutters (`30px`) and vertical separation (`30px`). The dashboard showtime feature and dated run form a wide two-column schedule, followed by a narrow status board and operational sections. Other views use ruled rows, cards, tables, or the calendar grid as their information requires.

At `1160px`, the schedule becomes one column; at `1080px`, split panels stack. At `820px`, the rail becomes an off-canvas menu, gutters shrink to `18px`, the status board becomes two columns, and controls gain a `44px` touch height. The calendar cells and poster thumbnails compress at `900px` and again at `560px`. The ultrawide treatment at `1800px` enlarges the schedule rather than leaving a small island of content. Tables and log panels can scroll within their own region.

## Elevation & Depth

Cards rest flat (`--shadow: none`). Depth primarily comes from petrol tonal layers, ivory schedule panels, fine rules, and a darker rail recess. The showtime panels use a restrained ambient shadow, while overlays, toasts, and the mobile rail use the stronger overlay shadow. The feature listing lifts by `2px` on hover; this is a response to interaction, not a default floating-card language.

**The Flat Operations Rule.** Keep ordinary monitoring surfaces flat and ruled; reserve lift for the schedule feature and temporary overlays.

## Shapes

Most controls use compact corners (`4px`), cards and schedule panels use gently curved corners (`10px`), and the modal uses a slightly larger corner (`14px`). Pills and chips are fully rounded. Poster frames stay almost square-cornered (`3px`); dense table and calendar cells use precise one-pixel rules rather than separate floating tiles.

## Components

### Buttons

Buttons are plainly labeled and tactile. The primary button is amber with dark ink; secondary buttons use a fine border over the current surface; danger buttons use the error color. All use compact corners, a `40px` default minimum height, and a `44px` touch height at narrow widths. Hover changes surface or brightness; keyboard focus gets a visible `2px` outline with offset. Disabled controls visibly fade.

### Chips and Status Pills

Filter chips use a fine border when unselected and fill amber when pressed. Status pills use quiet tinted backgrounds and separate success, warning, error, information, or accent foregrounds. A small live dot may pulse, while its text remains explicit.

### Cards and Rows

Operational cards have a petrol-raised surface, fine border, and no resting shadow. Their headings and body are separated by rules. Item rows and approval cards keep poster, title, facts, and actions grouped; hover changes the row's surface. Warning cards tint their header instead of changing the whole layout.

### Inputs and Fields

Text controls share the raised surface, strong border, compact corner, and body type. Focus sharpens the border and preserves the global focus indication. Settings switches use amber for the selected track and an explicit visible focus ring.

### Navigation

The rail contains the three-stream mark, wordmark, six route links, theme control, and version. Links are quiet until hover, while the current page fills amber and sets `aria-current`. On narrow screens, the rail opens over a scrim and can be closed with a labeled control.

### Showtime and Calendar

The schedule pairs a large ivory lead listing with a ruled run of following dates; date links open the corresponding Calendar day. The month calendar retains poster thumbnails and a selected day state, while the agenda remains available. Opening day detail uses one short lightbox reveal (`.36s`) and stays still under reduced-motion preferences.

## Do's and Don'ts

### Do:

- **Do** lead with the next useful release or household decision and show its context before an action.
- **Do** keep the ivory schedule, petrol operational surfaces, amber dates, and sea glass status in their established roles.
- **Do** preserve poster colors, the rás mark, visible focus, and reduced-motion behavior.
- **Do** distinguish a waiting continuation from a failure through both language and status treatment.

### Don't:

- **Don't** turn the waiting shelf into an urgent task backlog.
- **Don't** use amber alone to communicate an error or a healthy state.
- **Don't** make calendar cells, tables, and dense rows look like detached floating cards.
- **Don't** hide download-starting or file-removing actions behind unlabeled icons.
