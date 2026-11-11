---
name: Local Audiobook Workbench
description: Interface for chapter production, listening review, and character voice work.
colors:
  paper: "#f5f3ed"
  surface: "#fffefb"
  surface-raised: "#ffffff"
  ink: "#26372f"
  muted: "#59685e"
  line: "#d8ddd3"
  green: "#315946"
  green-hover: "#284a39"
  green-text: "#315946"
  green-soft: "#e1e9dd"
  green-pale: "#eef2eb"
  gold: "#e4c87f"
  gold-deep: "#68511f"
  gold-soft: "#f3ead0"
  rust: "#9a4327"
  rust-soft: "#f4e0d1"
  danger: "#8c2d22"
  focus: "#a64829"
  dark-paper: "#202320"
  dark-surface: "#292c28"
  dark-surface-raised: "#30332e"
  dark-ink: "#f0eee7"
  dark-muted: "#c0c1b9"
  dark-line: "#4b4e47"
  dark-green: "#3e6849"
  dark-green-hover: "#4d7958"
  dark-green-text: "#b4d0b5"
  dark-green-soft: "#344538"
  dark-green-pale: "#2e382f"
  dark-gold: "#dfc278"
  dark-gold-deep: "#f1d893"
  dark-gold-soft: "#4a432f"
  dark-rust: "#bd6849"
  dark-rust-soft: "#4b352d"
  dark-danger: "#f0a092"
  dark-focus: "#e6a07d"
typography:
  label:
    fontFamily: "Source Sans 3, Microsoft YaHei UI, Microsoft YaHei, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
  body:
    fontFamily: "Source Sans 3, Microsoft YaHei UI, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.55
  title:
    fontFamily: "Source Sans 3, Microsoft YaHei UI, Microsoft YaHei, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.5
  headline:
    fontFamily: "Source Sans 3, Microsoft YaHei UI, Microsoft YaHei, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.4
rounded:
  control: "12px"
spacing:
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  2xl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.green}"
    textColor: "{colors.surface}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "6px 13px"
    height: "36px"
  button-warm:
    backgroundColor: "{colors.gold}"
    textColor: "#3d3422"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "6px 13px"
    height: "36px"
  field:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "7px 10px"
    height: "36px"
  select:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "8px 10px"
    height: "40px"
  navigation-active:
    backgroundColor: "transparent"
    textColor: "{colors.green-text}"
    typography: "{typography.body}"
    padding: "8px 2px"
    height: "46px"
  surface:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "14px"
---

# Local Audiobook Workbench Design System

## Overview

The interface supports chapter production, listening review, and character voice management. The topbar provides access to these three work areas and keeps book, theme, cost, and exit controls nearby. The chapter, transcript, and voice surfaces prioritize readable content and direct actions.

The light theme uses warm paper and near-white surfaces. The dark theme uses charcoal surfaces with the same green, gold, rust, and status roles. The 12px corner radius, compact type scale, and fixed playback dock support repeated work with chapters, transcripts, and forms.

| Characteristic | Description |
| --- | --- |
| Work areas | Chapter production, listening review, and character voice management each have a topbar destination. |
| Color roles | Green identifies actions and active selections. Gold identifies secondary actions and pending review. Rust identifies caution and correction. |
| Themes | Light and dark themes use the same semantic color roles. |
| Content | The interface uses compact lists, transcript text, forms, and persistent playback controls. |

## Colors

The light and dark themes use the same green, gold, and rust roles with different surface and text values.

### Primary
| Token | Light value | Dark value | Use |
| --- | --- | --- | --- |
| `green` | `#315946` | `#3e6849` | Primary actions, active destinations, playback, and progress. |
| `gold` | `#e4c87f` | `#dfc278` | Secondary actions, selected review passages, and pending states. |
| `rust` | `#9a4327` | `#bd6849` | Caution and correction emphasis. |

### Secondary
| Token | Light value | Dark value | Use |
| --- | --- | --- | --- |
| `green-hover` | `#284a39` | `#4d7958` | Hover state for primary actions. |
| `green-text` | `#315946` | `#b4d0b5` | Active navigation text and selected-state text. |
| `green-soft` | `#e1e9dd` | `#344538` | Selected rows and current candidate surfaces. |
| `green-pale` | `#eef2eb` | `#2e382f` | Quiet information and candidate backgrounds. |
| `gold-deep` | `#68511f` | `#f1d893` | Text on gold-tinted surfaces. |
| `gold-soft` | `#f3ead0` | `#4a432f` | Review selection and pending-state surfaces. |
| `rust-soft` | `#f4e0d1` | `#4b352d` | Diagnostic and caution surfaces. |
| `danger` | `#8c2d22` | `#f0a092` | Error text and error states. |
| `focus` | `#a64829` | `#e6a07d` | Visible keyboard focus outline. |

### Neutral
| Token | Light value | Dark value | Use |
| --- | --- | --- | --- |
| `paper` | `#f5f3ed` | `#202320` | Page canvas and scroll track. |
| `surface` | `#fffefb` | `#292c28` | Main panels and playback dock. |
| `surface-raised` | `#ffffff` | `#30332e` | Inputs, selects, and raised controls. |
| `ink` | `#26372f` | `#f0eee7` | Main text. |
| `muted` | `#59685e` | `#c0c1b9` | Supporting text, labels, and timestamps. |
| `line` | `#d8ddd3` | `#4b4e47` | Borders and separators. |

## Typography

The interface uses Source Sans 3 with Microsoft YaHei UI and Microsoft YaHei fallbacks. Chinese and Latin text use the same font stack. Weight and size identify headings, controls, and supporting information.

| Role | Size | Weight | Line height | Use |
| --- | --- | --- | --- | --- |
| Label | 12px | 400 | 1.5 | Field labels, metadata, status, and playback time. |
| Body | 14px | 400 | 1.55 | Controls, navigation, and standard copy. |
| Title | 16px | 600 | 1.5 | Section and surface headings. |
| Headline | 20px | 600 | 1.4 | Page titles and the current work subject. |
| Transcript | 16px | 400 | 1.6 | Paragraph text in the listening review. |

Playback time uses tabular numerals.

## Layout

The sticky desktop topbar contains the chapters, listening review, and character voices destinations. Book, theme, cost, and exit controls sit in the same bar. At widths up to 780px the navigation occupies a centered row below the topbar tools. Chapter, review, and voice content use a single-column layout at this width. Voice candidates also stack vertically. At widths up to 520px the topbar tools and page heading stack, and the playback dock condenses its controls.

Use 24px and 32px for primary separation between work areas. Use 8px to 16px gaps and padding within compact control groups. The chapter view shows the chapter list with production status. The review view gives the transcript and speaker controls dedicated columns. The voice view shows the profile list beside the selected profile and its candidate options. A fixed playback dock spans the viewport bottom on all three pages.

## Elevation & Depth

Panels use the page canvas, a surface color, and a single fine border to establish depth. The select picker and floating cost panel use soft offset shadows.

### Shadow Vocabulary
| Use | Shadow |
| --- | --- |
| Select picker | `0 12px 28px rgba(38, 55, 47, .16)` in light mode and a darker shadow in dark mode. |
| Cost panel | `0 10px 24px rgba(38, 55, 47, .16)`. |

## Shapes

Controls, cards, and selected content use 12px corners. Borders are 1px wide and use the theme divider color. The playback play control and status marks use their component-specific dimensions. The playback dock spans the viewport and has one top border.

## Components

| Component | Appearance and behavior |
| --- | --- |
| Primary button | The button uses green `#315946`, near-white text, 12px corners, 6px 13px padding, and a 36px minimum height. Hover uses `#284a39`. |
| Warm button | The button uses gold `#e4c87f` with dark text for secondary confirmation and adoption actions. |
| Quiet button | The button uses a transparent surface and green text. Hover uses the soft green surface. |
| Focus and disabled states | Keyboard focus uses a 2px outline with a 3px offset. Disabled controls reduce opacity. |

### Cards / Containers
| Property | Treatment |
| --- | --- |
| Corners | Cards use a 12px radius. |
| Surfaces | Cards use the theme surface over the page canvas. |
| Borders | A single 1px divider outlines the card. Selected cards use the appropriate green or gold surface. |
| Padding | Common padding is 12px to 16px. Larger work areas use 24px. |
| Shadow | Floating panels and open pickers use the shadows listed above. |

### Inputs / Fields
| Property | Treatment |
| --- | --- |
| Text input | The field uses a raised surface, a 1px divider, a 12px radius, 14px text, and a 36px minimum height. |
| Speaker and speed selects | Both selects use 14px text, 40px height, and a 12px radius. |
| Open select | The picker, options, and checkmark use colors from the active theme. Open, hovered, and selected states use the soft green color. |
| Keyboard | Native select controls support Enter, Escape, and arrow keys. Focus remains visible. |
| Textarea | The control uses 14px text. Transcript passages use 16px text with 1.6 line height. |

### Navigation
The topbar navigation has three text destinations with 14px semibold labels. Inactive links use muted text. Hover uses the main text color. The active link uses green text and an underline in light mode. Dark mode uses gold text and an underline. At 780px and below the navigation wraps into a centered full-width row beneath the tools.

### Playback Dock
The dock stays fixed to the viewport bottom across all three destinations. It contains track identity, stop and play controls, speed selection, seek position, elapsed and total time, and playback state. Metadata uses 12px text and time uses tabular numerals.

### Voice Candidates
On desktop the current and proposed candidates appear side by side. Each candidate shows its state, description, and audition or adoption actions. At 780px and below they stack vertically. Green identifies the current candidate and gold identifies the proposed candidate.

### Browser Surfaces
Text selection uses the gold background with the current text color. The scrollbar track follows the page canvas and the thumb uses a muted gray. Keyboard-operated controls and links use the visible focus outline.

## Do's and Don'ts

### Do
- Do keep chapters, listening review, and character voices together in the topbar navigation.
- Do use warm paper in light mode and warm charcoal in dark mode with the same green, gold, and rust roles.
- Do style speaker and playback speed selects with 14px text, 40px height, and a 12px radius.
- Do theme the closed select, open picker, option hover, and selected option.
- Do preserve Enter, Escape, and arrow-key operation for native selects.
- Do use 24px and 32px for primary work-area separation and 8px to 16px inside compact control groups.
- Do show candidate states through color and text labels.

### Don't
- Don't introduce blue or purple into the interface palette.
- Don't assign different semantic colors to the same action or state in light and dark modes.
- Don't use color alone to identify selected, pending, error, or playback states.
- Don't leave the select picker or its options in an unrelated browser theme.
- Don't use large card shadows as the default depth treatment.
