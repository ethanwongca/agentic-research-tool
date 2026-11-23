# arx: agentic research, with the researcher in charge

**You design the pipeline. The agent only writes code you've approved, exactly as you specified, and
can't touch it again once it passes your checks.**

Lay out your research pipeline as boxes and arrows on a canvas. Each box is a step you specify:
what it's for, how it must be done, the choices it depends on, and what "correct" means.
Claude Code (or Codex) implements one approved box at a time. If something is unspecified, it has to
stop and ask you. It never proposes methods or decides things for you.

![The arx canvas: boxes connected by typed arrows, with a sticky note](docs/flow.png)

## Quick start

```bash
pip install -e .                      # once, in the environment you run Claude Code from
cd examples/text-clustering
arx ui                                # opens the canvas in your browser
```

For your own project, run `arx ui` in any folder. It offers to start an empty pipeline there.

## The loop

1. **Lay out boxes.** Press `Tab` or double-click to open quick add. Drag from an output socket ● onto
   a box to connect, or drop it on empty canvas to add a new box that's already wired up. Sockets and
   wires are colored by data type. Use sticky notes (`N`) for ideas and open questions.
2. **Specify each box.** The inspector walks you through it, and a checklist shows what's still missing.
3. **Approve** the box.
4. **Build.** Click **Build with Claude**, and the inspector streams what the agent is doing. You can
   also run `/arx-build` inside Claude Code. **Approve & build** does steps 3 and 4 in one click.
5. **Checks pass → the box locks** 🔒. If it keeps failing, it comes back to you with options:
   - revise the box
   - insert a box before it
   - try an alternative approach
   - give the agent more attempts

![Inspector: a guided spec with a checklist of what's missing](docs/spec.png)

### What you specify in a box

| | What it is | Enforced how |
|---|---|---|
| **Purpose** | One sentence: what the box produces and why | Required before approval |
| **Approach** | Your method, precise enough that two people would write the same code | Required; the agent implements it literally |
| **Decisions** | Choices the code must not make itself (model, k, seed…). The box's kind adds required ones. | Passed to the code as `params["key"]`, so they can't be hardcoded |
| **Constraints** | Rules that are hard to test, like *no network access* or *keep input order* | The agent must obey them. Use **→ check** to turn one into an invariant |
| **Contract** | Typed inputs and outputs: dataframe columns and dtypes, array shape, list item type | Checked on every run |
| **Tests** | Your hand-made input → expected output | Must pass to lock |
| **Invariants** | Properties that hold on the real data | Must pass to lock |

![Contract tab: typed, enforced inputs and outputs](docs/contract.png)

### Contracts
Every arrow carries a typed value. arx checks it on every test and every real run:

```yaml
outputs:
  - name: docs
    type: dataframe
    schema:
      rows: n                   # a symbol: must match the other ports of this box
      exact_columns: true
      columns:
        - {name: id,   dtype: str, nullable: false, unique: true}
        - {name: text, dtype: str, nullable: false}
  - name: embeddings
    type: ndarray
    schema: {dtype: float32, shape: [n, "*"], finite: true}
```

### Try it
Any built box can be run from the inspector, either on your own JSON input or on the real upstream data.
DataFrames come back as tables, arrays as stats, and labels as a bar chart. If you like what you see,
save the input as a test case.

## How Build connects to Claude

**You don't need an MCP server.** The Build button runs Claude Code headless in your project
(`claude -p … --permission-mode acceptEdits`) and streams its progress into the inspector.

Because Claude runs inside the project, its hooks still apply:
- it can only edit files of approved boxes
- it can't edit `pipeline.yaml`
- it can't approve or unlock anything

To use Codex or another CLI agent instead, change **Who builds** in Rules.

## Canvas controls

| | |
|---|---|
| `Tab` / double-click | quick add: search for a box kind or a sticky note |
| Drag an output socket ● onto a box | connect that output |
| Drag an output socket ● into empty space | add a new box that's already connected |
| Drag the pink **+** onto a box | connect (you pick the output), or "run after" for boxes without outputs |
| `V` select · `B` box · `N` note · `A` arrow | tools, also in the top dock |
| Drag a box | move it |
| Click an arrow | remove it |
| Double-click a note | edit it; selected notes have colors and delete |
| Scroll / `Space`+drag | pan |
| `⌘`+scroll | zoom |
| `⇧1` | fit to screen |

## Commands

| command | who | |
|---|---|---|
| `arx ui` | you | the canvas, at http://127.0.0.1:8765 |
| `arx` · `arx status` · `arx check [box]` | anyone | what's blocked and why |
| `arx approve` · `arx unlock` · `arx retry <box>` | **you only** | needs a real terminal (or use the UI) |
| `arx ask <box> "q" --key k` · `arx answer <box> k "v"` | agent | raise a neutral question / relay your answer |
| `arx scaffold <box>` · `arx verify <box>` · `arx run` | agent / anyone | generate the module, run checks, run the pipeline |
| `arx init <dir> [--example]` | you | set up a project (hooks, `AGENTS.md`, slash commands) |

## How it's enforced, and its limits

| layer | Claude Code | Codex |
|---|---|---|
| Protocol and live status injected every turn | ✅ hooks | `AGENTS.md` |
| Blocks edits to the spec, `.arx/`, and unapproved or locked code | ✅ PreToolUse hook | instructions only |
| Approve, unlock and retry are researcher-only | ✅ | ✅ (needs a real terminal) |
| Tamper detection via file hashes (pre-commit) | ✅ | ✅ |

The hooks keep a cooperative agent from drifting. They are not a sandbox, and hash-based tamper
detection is the backstop. If `arx` isn't installed where Claude Code runs, the hooks refuse edits
rather than letting them through unchecked.
