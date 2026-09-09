---
id: core-interaction-rules
tags:
  - base
  - interactions
  - rules
---

# DeepSeek Interaction Rules

This document defines the communication protocol between the developer and DeepSeek within the VS Code extension.
Tag this note as `@base` so it is automatically available in every chat session.

## Requesting Additional Information via Adjustment YAML

If DeepSeek needs more information before implementation, it must return fenced `yaml` using this simple shape instead of a workflow plan:

```yaml
adjustment-request:
  updated-objective: "updated original objective"
  files-needed:
    - "path/to/file"
    - "path/to/dir"       # directory listing
    - "path/to/dir/**"    # full recursive tree
  clarifications-needed:
    - "What should the output look like?"
    - type: selection
      prompt: "Pick one approach"
      options:
        - option-a
        - option-b
```

## Proposing File Changes

To propose a change to an existing workspace file, output a **unified diff** inside a fenced code block tagged `diff`.

Rules:
- Always include `--- a/<path>` and `+++ b/<path>` headers so the developer knows which file the diff applies to.
- Keep diffs minimal and targeted — change only what is necessary.
- One diff block per file.
- Briefly explain the change in plain text before each diff block.
- Plan the entire diff before outputting it — avoid generating a partial/wrong diff and then appending a corrected version; the parser treats each diff block as a separate patch.
- All diffs must be inside a fenced ```diff block — never write raw --- a/ lines outside a code fence.
