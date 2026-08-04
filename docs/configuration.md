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
      "args": ["--from", "https://github.com/KT-SPARKS/planka-mcp/releases/download/v0.1.2/planka_mcp-0.1.2-py3-none-any.whl", "planka-mcp"],
      "env": {
        "PLANKA_BASE_URL": "https://planka.example.com",
        "PLANKA_EMAIL": "you@example.com",
        "PLANKA_PASSWORD": "your-password"
      }
    }
  }
}
```

`uvx` (part of [uv](https://docs.astral.sh/uv/)) downloads the wheel and runs it,
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
  -- uvx --from https://github.com/KT-SPARKS/planka-mcp/releases/download/v0.1.2/planka_mcp-0.1.2-py3-none-any.whl planka-mcp
```

Check it worked by asking the agent to call `whoami`.

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
| `PLANKA_ALLOW_USER_ADMIN` | `false` | Enables `admin_manage_person` (instance roles, board managers). Even when on, the server refuses to change its own account's role, grant instance `admin`, or create accounts. |
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

## Checking it works

Ask the agent to call `whoami` — it reports the account, its instance role, the
configured ceiling and whether user administration is enabled. Then
`list_projects` shows every project in scope with the role you hold on each.

## Compatibility

Developed and verified against **Planka 2.4.1**. The published swagger (2.0.1)
is out of date in ways that matter — see
[planka-api-notes.md](planka-api-notes.md). On older Planka the role names and
list types differ, and some tools will not behave as documented here.
