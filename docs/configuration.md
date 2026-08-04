# Configuration

Everything is read from environment variables — from a `.env` file next to the
package, or from the `env` block of your MCP client's config. No secret is ever
hardcoded, and `.env` is git-ignored.

## Minimal setup

Three values, no install, no clone:

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

`uvx` (part of [uv](https://docs.astral.sh/uv/)) downloads and runs the server,
the way `npx` does for Node. Nothing else is required, and none of the variables
below are mandatory.

Swap the credentials for an API key if you prefer — Planka: user settings → API
key, shown once:

```json
      "env": {
        "PLANKA_BASE_URL": "https://planka.example.com",
        "PLANKA_API_KEY": "your-api-key"
      }
```

Claude Code:

```bash
claude mcp add planka \
  --env PLANKA_BASE_URL=https://planka.example.com \
  --env PLANKA_API_KEY=your-api-key \
  -- uvx --refresh-package planka-mcp --from https://github.com/KT-SPARKS/planka-mcp/releases/latest/download/planka_mcp-latest.tar.gz planka-mcp
```

Check it worked by asking the agent to call `whoami`.

## Staying up to date, or pinning

| Goal | `args` |
| --- | --- |
| **Auto-update** on client restart | `["--refresh-package", "planka-mcp", "--from", "<latest URL>", "planka-mcp"]` |
| **Pin** a known version | `["--from", "<that release's wheel URL>", "planka-mcp"]` |

The `latest` URL is a stable link that GitHub resolves to the newest release:

```
https://github.com/KT-SPARKS/planka-mcp/releases/latest/download/planka_mcp-latest.tar.gz
```

`--refresh-package planka-mcp` matters. Without it `uv` serves its cached copy
of that URL forever — the content behind the link changes, your install does
not. The flag costs roughly a second at launch and only revalidates this one
package, not its dependencies.

Pinned installs use the versioned **wheel** from a specific release; the
auto-updating link is an **sdist**, because a wheel filename must carry a real
version number and so cannot be called `latest`.

## Windows

* The desktop app spawns the command directly, so `uvx` may not resolve from
  `PATH`. Use the full path with doubled backslashes:
  `"command": "C:\\Users\\You\\AppData\\Local\\hermes\\bin\\uvx.exe"` — find it
  with `where uvx`.
* An `env` block **replaces** the process environment. That is why a
  `git+https://...` source fails with `Git executable not found` even when git is
  installed: uv is launched without a usable `PATH`. Installing from the wheel URL
  needs no git. If you must use a git source, add `"PATH"` to the `env` block.
* Config path: `%APPDATA%\Claude\claude_desktop_config.json`. Quit from the tray
  icon and reopen; closing the window is not a restart.

## Installing from source

Needs git available to the process:

```bash
uvx --from git+https://github.com/KT-SPARKS/planka-mcp planka-mcp
```

Development checkout:

```bash
git clone https://github.com/KT-SPARKS/planka-mcp.git
cd planka-mcp
uv venv && uv pip install -e .
cp .env.example .env      # a .env file is only read in this mode
.venv/bin/planka-mcp
```

## Recommended setup: a dedicated agent account

Do **not** point this at your own admin account. Create a Planka user for the
agent, then decide what it may touch:

1. Create a user in Planka (Admin panel → Users), instance role `boardUser`.
2. Add that user to the boards it should work on. Give it the **`worker`** role
   to let it work on tasks only, or **`editor`** to also let it shape lists and
   labels.
3. Log in as that user, open user settings, and create an **API key**.
4. Put the key in `PLANKA_API_KEY` and leave email/password unset.
5. Optionally set `PLANKA_BOARD_IDS` so the server refuses everything else.

The instance role governs the Planka admin panel; it does not grant access to
project content. Membership is what matters.

## Authentication

| Variable | Meaning |
| --- | --- |
| `PLANKA_BASE_URL` | Root URL of your Planka. `/api` is appended if missing. **Required.** |
| `PLANKA_API_KEY` | Long-lived key from the user's settings, sent as `X-Api-Key`. Preferred: revocable without changing the password. |
| `PLANKA_EMAIL` / `PLANKA_PASSWORD` | Fallback. A token is fetched on demand, sent as `Authorization: Bearer`, and refreshed automatically on `401`. |

Set the API key **or** the email/password pair, not both.

## Scope

| Variable | Default | Meaning |
| --- | --- | --- |
| `PLANKA_BOARD_IDS` | *(unset)* | Comma-separated project ids. When set, every tool refuses anything outside the list. The strongest single safety control here. |
| `PLANKA_BOARD_TYPES` | `project` | Which board types hold work. `library` and `collection` boards store documents, so they are skipped. Set empty to scan everything. |

Find project ids by running the server and calling `list_projects`, or read them
from the URL when you open the tab in Planka.

## Roles and limits

| Variable | Default | Meaning |
| --- | --- | --- |
| `PLANKA_ACT_AS` | *(unset)* | Ceiling on what the agent may do: `guest`, `worker`, `editor` or `admin`. Lowers the account's real rights, never raises them. |
| `PLANKA_ALLOW_USER_ADMIN` | `false` | Enables `admin_manage_person`: instance roles, board managers, and minting or revoking API keys. Even when on, the server refuses to change its own account's role, grant instance `admin`, or create accounts. |
| `PLANKA_ALLOW_REOPEN` | `false` | Allow `done → todo` / `done → in_progress`. By default `done` is terminal. |

`PLANKA_ACT_AS` is useful when you must use a powerful account but want a
deliberately limited agent — for example an instance admin credential capped to
`worker`.

## Workflow mapping

Planka has no status field: a task's status is the list it sits in. List names
are matched by heuristic:

| Status | Matches names like |
| --- | --- |
| `todo` | To Do, Backlog, Ready, Up Next, Inbox, Planned |
| `in_progress` | In Progress, Doing, WIP, Started, Development |
| `review` | Review, Code Review, QA, Testing, Approval |
| `done` | Done, Completed, Shipped, Released, Closed |

Any list whose **type** is `closed` counts as done regardless of name. Lists of
type `waiting` or `inactive`, and Planka's own system lists, are treated as
outside the flow.

If your board uses different names, override the mapping:

```bash
# by name, applied to every project
PLANKA_STATUS_LISTS={"todo":["Icebox","Up Next"],"in_progress":["Active"]}

# or exactly, per project, by list id
PLANKA_STATUS_LISTS={"<projectId>":{"todo":"<listId>","done":"<listId>"}}
```

Priority and effort are label conventions, not fields:

* priority — `p0`/`urgent`/`critical`, `p1`/`high`, `p2`/`medium`, `p3`/`low`
* effort — `size: S`/`M`/`L`/`XL`, or points like `sp: 3`

Name your labels along those lines and the queue sorts itself sensibly.

## Optional queue filters

| Variable | Default | Meaning |
| --- | --- | --- |
| `PLANKA_BLOCKED_LABELS` | *(unset)* | Comma-separated label names that hide a task from the queue, e.g. `blocked,on hold`. |
| `PLANKA_REQUIRE_DEPS_MET` | `false` | Hide tasks whose checklist links point at unfinished tasks. Dependencies are always reported as `waiting_on` either way. |

## Transport

| Variable | Default | Meaning |
| --- | --- | --- |
| `PLANKA_TRANSPORT` | `stdio` | `stdio` for a local client, `http` for StreamableHTTP. |
| `PLANKA_HTTP_HOST` / `PLANKA_HTTP_PORT` | `127.0.0.1` / `8000` | Bind address for HTTP mode; endpoint is `/mcp`. |
| `PLANKA_TIMEOUT` | `20` | Per-request timeout in seconds. |
| `PLANKA_USER_AGENT` | `planka-mcp/0.1` | Some WAFs (Cloudflare among them) block default Python user agents with `403 error code: 1010`. |

**HTTP mode has no authentication of its own.** It is meant for a trusted
network or a tunnel to a single user. Anyone who can reach the port acts as the
configured Planka account. Do not expose it to the internet as-is.

## Connecting a client

Claude Code — see the one-liner in [Quick start](#quick-start--no-install).

Claude Desktop (`claude_desktop_config.json`) or Cursor (`.cursor/mcp.json`) —
with an API key:

```json
{
  "mcpServers": {
    "planka": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/KT-SPARKS/planka-mcp@v0.1.0", "planka-mcp"],
      "env": {
        "PLANKA_BASE_URL": "https://planka.example.com",
        "PLANKA_API_KEY": "your-api-key",
        "PLANKA_BOARD_IDS": "1234567890123456789",
        "PLANKA_ACT_AS": "worker"
      }
    }
  }
}
```

…or with email and password:

```json
{
  "mcpServers": {
    "planka": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/KT-SPARKS/planka-mcp@v0.1.0", "planka-mcp"],
      "env": {
        "PLANKA_BASE_URL": "https://planka.example.com",
        "PLANKA_EMAIL": "agent@example.com",
        "PLANKA_PASSWORD": "your-password",
        "PLANKA_BOARD_IDS": "1234567890123456789",
        "PLANKA_ACT_AS": "worker"
      }
    }
  }
}
```

Running from a checkout instead? Use the absolute path to the installed script:

```json
{ "command": "/absolute/path/to/planka-mcp/.venv/bin/planka-mcp" }
```

## Searching across projects

`find_tasks` scans every project in scope rather than one board. Filters:

| Argument | Example |
| --- | --- |
| `text` | `"login"` — matches title or description, case-insensitive |
| `assignee` | `"Ada"`, an email, an id, `"me"`, `"unassigned"`, `"anyone"` |
| `status` | `todo`, `in_progress`, `review`, `done` |
| `label` | `"priority: high"` |
| `overdue_only` | `true` — due date already passed |
| `include_done` | `true` — finished work is excluded by default |
| `project_id` / `board_id` | narrow to one tab, or one container |

An ambiguous name is refused with the candidates listed, never guessed. Results
carry a `url` to open the card. Scanning is one request per project, cached
briefly, so a workspace with dozens of projects stays responsive.

### Searching for a person

Work reaches someone three ways, and `assignee` finds all of them:

| `matched_by` | Meaning |
| --- | --- |
| `assigned to the task` | they are a member of the card |
| `assigned a checklist item` | a checklist item on the card is assigned to them, even if the card itself is not |
| `mentioned in a comment` | someone wrote `@[Their Name](id)` in a comment |

Each result carries the specific `their_checklist_items` and `mentions`, and the
response summarises the split under `how_they_are_involved`. Mentions match on
**user id**, not display name, so renaming a person does not break history. Set
`include_mentions: false` to skip the comment scan — it only reads cards that
report having comments, so it is usually a handful of extra requests.

## API keys

With `PLANKA_ALLOW_USER_ADMIN=true` **and** an instance-admin account,
`admin_manage_person` can mint and revoke keys:

```
admin_manage_person(action="create_api_key", person="ada@example.com")
admin_manage_person(action="revoke_api_key", person="ada@example.com")
```

Understand what this is before turning it on:

* A key is a **long-lived credential that acts as that person**. Minting one for
  someone else is, in effect, permission to impersonate them until it is revoked.
* Planka shows the value **once**. It arrives in the tool result, which means it
  passes through the model's context and into any transcript or MCP log the
  client writes to disk. Treat a minted key as compromised if that transcript is
  not trusted, and revoke it.
* Minting **replaces** any key the account already had, so whatever was using the
  old one stops working. The response reports `replaced_an_existing_key`.
* The server refuses to mint or revoke a key for its own account when it is
  authenticating with one, since that would cut off its own access mid-session.

Minting keys in the Planka UI avoids the transcript problem entirely. Prefer that
unless you specifically need it automated.

## Work handed over in comments

A common habit is to hand a task over by writing a comment:

```
Assigned to: @[Ada Hopper](1234567890123456789)
```

Planka stores that as text. The person is **not** assigned: the task stays out of
their queue, no board view shows them as owner, and nothing counts it as theirs.

`find_informal_assignments` scans comments for mentions where the named person is
not a member of the task, and returns the exact `assign_people` call that would
make each one real. It changes nothing by itself.

* Only comments with hand-off wording are reported by default
  (`assigned`, `please`, `can you`, `over to`, `to review`, …). Pass
  `include_any_mention: true` for every mention.
* A mention of someone already assigned is not reported — that one is reconciled.
* Finished tasks are skipped unless `include_done: true`.

Treat findings as questions, not instructions: a mention can be "have a look at
this" rather than "this is yours". The server tells the agent to confirm with a
human before assigning.

## Checking it works

Ask the agent to call `whoami` — it reports the account, its instance role, the
configured ceiling and whether user administration is enabled. Then
`list_projects` shows every project in scope with the role you hold on each.

## Compatibility

Developed and verified against **Planka 2.4.1**. The published swagger (2.0.1)
is out of date in ways that matter — see
[planka-api-notes.md](planka-api-notes.md). On older Planka the role names and
list types differ, and some tools will not behave as documented here.
