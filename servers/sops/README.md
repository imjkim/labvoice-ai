# Synthetic standard operating procedures

Written for this demo. They are plausible but **not validated procedures and not for clinical use**.
They exist so the SOP MCP server has a realistic corpus to search: overlapping vocabulary (every stain
type has "controls"), procedures that depend on LIMS state (what to do when an instrument is on QC
hold), and questions they deliberately don't answer.

Format: one file per SOP, `# SOP-NNN Title`, a metadata line, then numbered `## N. Section`
headings. The server indexes one chunk per section and cites it as `SOP-NNN §N`.
