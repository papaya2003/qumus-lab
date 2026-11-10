# Debug page: design plan

Skill used: Anthropic's public **frontend-design** skill (github.com/anthropics/skills,
`skills/frontend-design/SKILL.md`). A search of this account's skills (SearchSkills: "frontend design",
"web dashboard", "html ui", "control panel") returned nothing, so the public skill was read and followed:
ground the design in the subject, write a token plan, review it against the generic defaults, then build,
then critique with screenshots.

## Brief (as understood)

- Subject: the Qumus lab bench, an exfoliation + stacking robot for 2D materials.
- Audience: Jialei at the Windows lab PC during the first real tests (L1-L2), one instrument at a time.
- Primary job: see every instrument of both modules live, do one capped action safely, and stop
  everything instantly. Secondary: unlock after an unknown outcome, see the event log.

## Tokens

| Token | Light | Dark | Role |
| --- | --- | --- | --- |
| `--table` | `#E6E9ED` | `#14181D` | page: the optical breadboard (dot grid, 25 px pitch) |
| `--panel` | `#F8F9FA` | `#1D232A` | module columns |
| `--ink` | `#1B2430` | `#E3E7EC` | text |
| `--muted` | `#5B6673` | `#97A2AE` | secondary text |
| `--oxide` | `#5E46A0` | `#A893E6` | interactive accent: the violet of a SiO2/Si chip |
| `--ok` / `--warn` / `--fault` | `#2E7A4E` / `#A86A12` / `#BE2F28` | lighter variants | device state |
| `--estop` / `--estop-band` | `#C8102E` / `#F2C200` | same | the STOP ALL band only |

Type: **Barlow** (body, readouts with tabular figures) and **Barlow Semi Condensed** (device names,
column heads). Barlow's grotesque comes from road signage, which suits a control panel; readouts use
`font-variant-numeric: tabular-nums` instead of a monospace face. System fonts are the fallback when the
lab PC is offline.

Layout (changed 11-09, Jialei): one module at a time. A left column lists Exfoliation and Stacking with
their state (Ready / Busy / Locked); the chosen module fills the main area (also in the address, `#stacking`).
Each camera row shows its live picture under its controls (about 5 per second while the module is on
screen; "Live" turns it off). Jog buttons step once on a click and keep stepping while held (each step is
an ordinary capped jog, sent only after the last one finished; at most 20 steps per hold, so a mouse-up
lost over a remote desktop cannot keep an axis going). "Move to" sends `move_axis` with a typed target
after a confirm dialog. The original layout follows.

Original layout (11-08): the page is the bench seen from above. Exfoliation is the left column and stacking the right
column, as on the real table. Devices are rows separated by hairlines inside each column (no card grid),
each row = name + connection dot + readouts + the controls that fit that kind of device. The event log
sits under both columns. One sticky e-stop band runs along the bottom edge.

```
+-------------------------------------------------------------+
| Qumus lab bench   demo/real   control: [Take control]        |
+------------------------------+------------------------------+
| Exfoliation (left)           | Stacking (right)             |
|  status line, Read all       |  status line, Read all       |
|  arm_left   x y z   jog ...  |  arm_right  x y z   jog ...  |
|  ...                          |  ...                         |
+------------------------------+------------------------------+
| Events (both modules)                                        |
+=============================================================+
| [ STOP ALL ]  stops every axis and switches heaters off      |
+-------------------------------------------------------------+
```

Principles: the one bold element is the STOP ALL band (red button on a yellow/black hazard band, always
visible); everything else is quiet. Numbers are the content, so they get the size. Disabled controls
say why (no lease, locked, not connected). Copy uses plain verbs in sentence case: "Take control",
"Read all", "Heat", "Unlock".

## Review against the generic defaults

- First plan used a near-black page with a single bright green accent for "online" states. That is
  default #2 in the skill, so the page became the light breadboard grey with a dot grid and the chip
  violet accent; green is kept only for "connected".
- First plan had one card per device with the same radius and shadow (the SaaS card kit). Replaced by
  rows with hairlines inside two panels.
- First plan labelled sections with small uppercase eyebrows and monospace readouts. Removed: sentence
  case headings, tabular Barlow numbers.
