# planka-mcp

An MCP server that lets an AI agent autonomously pick up and complete work on a
[Planka](https://planka.app) board.

It is **not** a wrapper around the Planka REST API. The tool surface is shaped
around what an autonomous worker actually does — find ready work, claim it,
report on it — and the guardrails are enforced in code, not in prompt text.

## Vocabulary

The tools speak your words, not Planka's schema:

| You say | Planka calls it | What it is |
| --- | --- | --- |
| **board** | project | the container (a tab strip) |
| **project** | board | the tab you actually work in: lists, cards, members |
| **task** | card | one piece of work |

## What the agent sees

**Doing work**

| Tool | Purpose |
| --- | --- |
| `list_actionable_tasks` | Pre-filtered queue of genuinely ready work, most urgent first |
| `find_tasks` | Search every project — other people's work included — by text, assignee, status, label or overdue. A person's results cover task assignment, checklist items **and** comment mentions |
| `claim_task` / `release_task` | Atomic claim; `already_taken` on a race |
| `get_task` | Full detail of a chosen task |
| `update_status` | `todo → in_progress → review → done`, illegal jumps rejected |
| `add_result` / `add_comment` / `attach_link` | Report output, context, deliverables |

**Shaping work**

| Tool | Purpose |
| --- | --- |
| `create_task` / `update_task_details` | File and refine work |
| `update_checklist` | Add steps, tick them off, reopen them |
| `set_task_labels` | Change priority/size (both are labels in Planka) |
| `move_task` | Move to any list, including stages outside the status flow, or to another project |

**Structure**

| Tool | Purpose |
| --- | --- |
| `list_boards` / `list_projects` / `get_project` | Discover containers, tabs, and your role on each |
| `create_project` / `update_project` | New tab with workflow lists; rename |
| `create_list` / `update_list` / `delete_list` | Stages; retire via `inactive`; delete only when empty |
| `manage_labels` | Create, rename, delete-if-unused |

**People**

| Tool | Purpose |
| --- | --- |
| `whoami` | What this server may do, and under what ceiling |
| `list_people` | Project members with roles, or the whole directory |
| `assign_people` / `unassign_people` | Put people on a task, adding them to the project first |
| `set_project_members` | Add, re-role (`worker`/`editor`/`guest`) or remove members |
| `find_informal_assignments` | Work handed over in a comment but never actually assigned |
| `admin_manage_person` | Instance roles, board managers, API key minting and revocation — off unless enabled |

Plus one prompt, **“Work through my task queue”**, as a one-click entry point.

## Roles

Planka 2.4.1 has two role axes, and the swagger documents neither correctly:
instance role (`admin`, `projectOwner`, `boardUser`, `guestOnly`) and board
membership role (`editor`, **`worker`**, `guest`). This server folds them into one
effective role per project:

| Condition | Effective role | Can |
| --- | --- | --- |
| instance `admin`, or manager of the parent board | `admin` | everything, incl. membership |
| board membership `editor` | `editor` | work, author, assign, structure |
| board membership `worker` | `worker` | work on tasks; no structure, no assigning others |
| `guest` or no membership | `guest` | read only |

`PLANKA_ACT_AS=worker|editor|admin` lowers that ceiling but never raises it — one
admin credential can still run a deliberately limited agent. Every refusal names
the role, where it came from, and what was attempted.

## How Planka concepts are mapped

Planka has no status, priority or effort field, so the server derives them:

* **Status = which list the card is in.** List names are matched by heuristic
  (`backlog`/`todo`/`ready` → `todo`, `doing`/`in progress`/`wip` →
  `in_progress`, `review`/`qa` → `review`, `done`/`closed` → `done`). Lists of
  type `waiting`/`inactive`, and Planka's own system lists, are outside the flow;
  type `closed` counts as done. Override the
  mapping with `PLANKA_STATUS_LISTS` (see `.env.example`).
* **Priority** comes from label conventions (`p0`/`urgent`/`critical` → urgent,
  `p1`/`high`, `p2`/`medium`, `p3`/`low`), defaulting to medium.
* **Effort** comes from size labels (`size: M`, `small`, `XL`) or point labels
  (`sp: 3`), falling back to the subtask count.
* **Dependencies** come from checklist tasks that link to another card
  (`linkedCardId`). Reported as `waiting_on` in `get_task`; only used as a filter
  when `PLANKA_REQUIRE_DEPS_MET=true`.

### What counts as actionable

A card is returned by `list_actionable_tasks` when **all** of these hold:

1. it sits in a list mapped to `todo`, and that list is of type `active`;
2. the card is not closed;
3. it has no assignees, or the agent's own account is one of them;
4. it is on a `project` board — `library` and `collection` boards hold documents,
   not work (`PLANKA_BOARD_TYPES`);
5. its board is inside `PLANKA_BOARD_IDS`, when that allowlist is set.

Two further filters exist but are **off by default**: `PLANKA_BLOCKED_LABELS`
(hide cards carrying e.g. a `blocked` label) and `PLANKA_REQUIRE_DEPS_MET`
(hide cards whose linked dependency cards are unfinished).

### How claiming is made safe

Planka has no compare-and-swap on card fields, but card assignment *is* a unique
row (`cardId` + `userId`), and the API answers `409` when it already exists. So:

1. read the card fresh; if someone else is assigned → `already_taken`;
2. `POST` the assignment — `200` = claimed, `409` = you already held it
   (idempotent, so a retried call is harmless);
3. re-read and compare assignment timestamps; if another worker's row is older,
   the server removes its own row and reports `already_taken`.

**Caveat, stated plainly:** step 1→2 is a read-then-write. Two *different*
accounts writing inside the same few milliseconds are resolved by the step-3
reconcile, not prevented outright. Two runs of the *same* agent account cannot
double-claim — the `409` makes that path genuinely atomic.

### Guardrails enforced in code

* **Nothing that holds work is deleted.** `delete_list` refuses a list containing
  cards; `manage_labels` refuses a label still on a card; there is no tool to
  delete a task, comment, project or board. Retiring a stage is a type change to
  `inactive`, which is reversible.
* **Credentials are handled explicitly.** Minting an API key requires
  `PLANKA_ALLOW_USER_ADMIN=true` plus an instance-admin account, reports whether
  it replaced an existing key, and refuses to touch the key this server is itself
  authenticating with.
* **Other people's work is untouchable.** Editing, moving, relabelling or
  releasing a task claimed by someone else is refused.
* **Claims are only ever for yourself.** `claim_task`/`release_task` take no user
  parameter; assigning others is a separate, higher-privileged tool.
* **Role is checked per project, per action**, and a refusal explains why.
* **Escalation is blocked.** `admin_manage_person` is off unless
  `PLANKA_ALLOW_USER_ADMIN=true`, and even then it will not change its own
  account's role, grant instance `admin`, or create an account (that needs a
  password, which this server never handles). `set_project_members` will not
  remove its own access.
* **Status transitions are validated**; `done` is terminal unless
  `PLANKA_ALLOW_REOPEN=true`.
* `add_result` takes an idempotency key, so a retry cannot double-post.
* `attach_link` accepts `http`/`https` only.
* `PLANKA_BOARD_IDS` hard-scopes every tool to specific projects.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

65 offline tests run against an in-memory fake that reproduces Planka's
semantics — assignment unique on card+user with `409` on re-insert, cards in a
`closed` list finished by the server, board role separate from instance role.
They cover: claim → idempotent re-claim → lost-race rollback; illegal
transitions and `done` terminality; ownership checks on status/result/release/
edit/move; the board allowlist; role folding across both axes and the
`PLANKA_ACT_AS` cap; worker-vs-editor-vs-admin gating; the assign-and-add-as-
worker flow with ambiguity refusal; refusal to delete non-empty lists or in-use
labels; refusal to self-remove or self-promote.

## Setup

Nothing to install or clone. Add this to your MCP client config, fill in three
values, restart:

```json
{
  "mcpServers": {
    "planka": {
      "command": "uvx",
      "args": ["--refresh-package", "planka-mcp", "--from", "https://github.com/KT-SPARKS/planka-mcp/releases/latest/download/planka_mcp-latest.tar.gz", "planka-mcp"],
      "env": {
        "PLANKA_BASE_URL": "https://planka.example.com",
        "PLANKA_EMAIL": "you@example.com",
        "PLANKA_PASSWORD": "your-password"
      }
    }
  }
}
```

That URL always resolves to the newest release, and `--refresh-package` makes
`uv` check for a new build on every launch — so restarting your client is the
whole update process. Drop the flag and you stay on whatever version you first
downloaded, because `uv` caches by URL.

Prefer an API key over a password (Planka: user settings → API key):

```json
      "env": {
        "PLANKA_BASE_URL": "https://planka.example.com",
        "PLANKA_API_KEY": "your-api-key"
      }
```

Claude Code, one line:

```bash
claude mcp add planka --env PLANKA_BASE_URL=https://planka.example.com --env PLANKA_API_KEY=your-api-key -- uvx --refresh-package planka-mcp --from https://github.com/KT-SPARKS/planka-mcp/releases/latest/download/planka_mcp-latest.tar.gz planka-mcp
```

To pin a version instead, point at that release's wheel and drop the refresh
flag:

```json
      "args": ["--from", "https://github.com/KT-SPARKS/planka-mcp/releases/download/v0.1.5/planka_mcp-0.1.5-py3-none-any.whl", "planka-mcp"]
```

That is the whole setup. Everything below is optional.

<details>
<summary>Windows notes</summary>

Two things trip up the Windows desktop app:

* **`uvx` not found** — the app spawns the command directly, so give the full
  path, with doubled backslashes:
  `"command": "C:\\Users\\You\\AppData\\Local\\hermes\\bin\\uvx.exe"`.
  Find it with `where uvx`.
* **`Git executable not found`** — an `env` block replaces the process
  environment, so a `git+https://...` source cannot run git. The release URLs
  above need no git and avoid this entirely. If you do want to install from
  source on Windows, add `"PATH"` to the `env` block.

Config lives at `%APPDATA%\Claude\claude_desktop_config.json`. Quit from the
tray icon and reopen — closing the window does not restart the app.

</details>

<details>
<summary>Locking it down (recommended for unattended agents)</summary>

The setup above gives the agent whatever the account can do. To narrow it:

```json
      "env": {
        "PLANKA_BASE_URL": "https://planka.example.com",
        "PLANKA_API_KEY": "your-api-key",
        "PLANKA_BOARD_IDS": "1234567890123456789",
        "PLANKA_ACT_AS": "worker"
      }
```

* `PLANKA_BOARD_IDS` — every tool refuses anything outside these projects.
* `PLANKA_ACT_AS` — ceiling of `guest`, `worker`, `editor` or `admin`, applied on
  top of the account's real rights.

Better still, create a dedicated Planka user for the agent, add it to just the
boards it should touch as a `worker`, and use its API key — rather than pointing
this at an admin account.

</details>

<details>
<summary>Source installs and development</summary>

From source (needs git available to the process):

```bash
uvx --from git+https://github.com/KT-SPARKS/planka-mcp planka-mcp
```

For development:

```bash
git clone https://github.com/KT-SPARKS/planka-mcp.git
cd planka-mcp
uv venv && uv pip install -e .
cp .env.example .env       # only read in this mode
.venv/bin/planka-mcp
```

</details>

**Full configuration reference** — every environment variable, status mapping,
HTTP transport — is in [docs/configuration.md](docs/configuration.md).

## Notes on the live API

Developed and verified against **Planka 2.4.1**, whose published swagger (2.0.1)
lags the server in several places — different role names, different list types,
undocumented routes. Everything learned from the live instance —
spec drift, permission rules, the private→shared conversion, the claim/CAS
semantics, the Cloudflare WAF gotcha — is written up in
[docs/planka-api-notes.md](docs/planka-api-notes.md).

Errors from Planka are returned to the model as clean
`{"ok": false, "error": "..."}` results rather than raised as exceptions.
