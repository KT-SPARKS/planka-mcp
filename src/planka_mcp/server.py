"""Entry point: registers every tool group on the shared MCP server.

Tool groups
-----------
* `tools_work`    - find, claim and progress work
* `tools_author`  - create and refine tasks
* `tools_manage`  - lists, labels and board (tab) structure
* `tools_people`  - membership, roles and user administration

Vocabulary note: the tools speak the user's words, where a "project" is the tab
you work in (a Planka *board*) and a "board" is the container holding those tabs
(a Planka *project*). Internals keep Planka's own names.
"""

from __future__ import annotations

import sys

from .app import mcp
from .config import ConfigError, load_config
from . import tools_author, tools_manage, tools_people, tools_work  # noqa: F401  (registers tools)

# Re-exported so tests and embedders can call the tool functions directly.
from .tools_author import (  # noqa: F401
    attach_link,
    create_task,
    update_checklist,
    update_task_details,
)
from .tools_manage import (  # noqa: F401
    create_list,
    create_project,
    delete_list,
    get_project,
    list_boards,
    list_projects,
    manage_labels,
    move_task,
    set_task_labels,
    update_list,
    update_project,
)
from .tools_people import (  # noqa: F401
    admin_manage_person,
    assign_people,
    find_informal_assignments,
    list_people,
    set_project_members,
    unassign_people,
    whoami,
)
from .tools_work import (  # noqa: F401
    add_comment,
    find_tasks,
    add_result,
    claim_task,
    get_task,
    list_actionable_tasks,
    release_task,
    update_status,
)

# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------


@mcp.prompt(title="Work through my task queue")
def work_queue(board: str | None = None) -> str:
    """One-click entry point: pick up ready work and complete it end to end."""
    scope = f" on board {board}" if board else ""
    return (
        f"Work through the Planka task queue{scope}.\n\n"
        "1. Call list_actionable_tasks and show me the queue in priority order.\n"
        "2. Take the top task, claim it with claim_task, and if the claim fails "
        "move to the next one.\n"
        "3. Read it with get_task, set it to in_progress, and do the work.\n"
        "4. Post what you produced with add_result, then set the status to done.\n"
        "5. Repeat until nothing is actionable, then summarise what you finished."
    )


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"planka-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if config.transport in ("http", "streamable-http", "streamable_http"):
        mcp.run(
            transport="streamable-http",
            host=config.http_host,
            port=config.http_port,
        )
    elif config.transport == "sse":
        mcp.run(transport="sse", host=config.http_host, port=config.http_port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
