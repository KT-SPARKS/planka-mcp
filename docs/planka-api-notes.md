# Planka API notes

Everything here was verified against a live **Planka 2.4.1** instance. The
published swagger is **2.0.1** and lags reality in several places, so trust this
file over the spec where they disagree.

Source spec: <https://plankanban.github.io/planka/swagger-ui/swagger.json>

## Spec drift found in 2.4.1

| Behaviour | Swagger 2.0.1 says | 2.4.1 actually does |
| --- | --- | --- |
| Current user | no such route | `GET /users/me` works, returns `item` + `included.notificationServices` |
| Board create | `{name, position}` | also **requires** `type` ∈ `project`, `library`, `collection` |
| Board create in a personal project | not mentioned | `422 Project must not be service` |
| Project create | `POST /projects {type,name}` | `404 E_NOT_FOUND` unless the caller is `admin`/`projectOwner` |
| Project `type` | not on the Project schema | create-only; `PATCH {type}` returns `200` and is silently ignored |
| Private → shared | not documented | `PATCH /projects/{id} {"ownerProjectManagerId": null}` |
| Add project manager | `POST /projects/{id}/project-managers` | `404` while the project has an owner (i.e. is private); works once it is shared |
| Board membership role | `editor`, `viewer` | `editor`, **`worker`**, `guest` — plus per-member switches `canSeeOnlyAssignedCards`, `canCreateCards`, `canUseComments`, `canAccessInbox`, `canInteractWithGuests`, `hideIdentityFromGuests` |
| Instance user role | `admin`, `projectOwner`, `boardUser` | adds **`guestOnly`** |
| List type | `active`, `closed`, `archive`, `trash` | creatable: `active`, `closed`, **`waiting`**, **`inactive`**. Plus system lists Planka makes per board, with `type` in `inbox`, `recurring`, `archive`, `trash` and `name: null` — visible in `GET /boards/{id}`, not creatable |
| Card type | `project`, `story` | adds **`link`** |

## Data model, as this server uses it

* **There is no status field.** A card's status is the list it sits in.
  `List.type` ∈ `active`, `closed`, `archive`, `trash`. This server maps list
  *names* to `todo`/`in_progress`/`review`/`done`, treats `closed` as done, and
  ignores `archive`/`trash` entirely. See `domain.classify_list`.
* **There is no priority or effort field.** Both are label conventions.
  `domain.derive_priority` / `domain.derive_effort`. A priority label is never
  read as a size — `priority: medium` must not become size `m`.
* **Dependencies exist** as checklist tasks carrying `linkedCardId`. They are
  reported as `waiting_on` and, when `PLANKA_REQUIRE_DEPS_MET=true`, filter the
  queue. They are excluded from subtask counts — a dependency is not work.
* **Board types matter.** Only `project` boards hold work; `library` and
  `collection` boards hold documents and references, so `client.board_ids()`
  skips them unless `PLANKA_BOARD_TYPES` says otherwise.
* **Card ordering** is a float `position`; moving a card requires sending both
  `listId` and `position`. New position = max in the target list + 65536.

## Atomicity

`POST /cards/{cardId}/card-memberships` is the only compare-and-swap primitive
that matters here: the row is unique on `(cardId, userId)` and Planka answers
**409** when it already exists. Verified live — five concurrent `claim_task`
calls produced exactly one membership row, one `claimed` and four
`already_yours`.

Plain fields have no `If-Match`/version, so a claim across *different* accounts
is a read-then-write; `claim_task` closes it by re-reading after the write and
rolling its own row back if a rival's row has an earlier `createdAt`.

## Permissions

* Instance role (`admin`, `projectOwner`, `boardUser`) governs the admin panel
  and project creation. **It does not grant access to project content** — an
  instance admin still cannot see a private project they do not manage.
* A private project has `ownerProjectManagerId` set and accepts exactly one
  manager. Null that field to make it shared, then managers can be added.
* Read access to a board does not imply write access. Planka returns `403`
  (`E_FORBIDDEN`), which the client surfaces as "the Planka account lacks rights
  for this board/action" rather than crashing the tool.

## Auth

* `POST /access-tokens {emailOrUsername, password}` → `{"item": "<jwt>"}`. The
  JWT `sub` claim is the user id (used as a fallback if `/users/me` is absent).
  Observed lifetime on 2.4.1: one year.
* `POST /users/{id}/api-key` mints a long-lived key; send it the same way, as
  `Authorization: Bearer <key>`.
* All errors come back as `{code, message, problems[]}`; the client folds those
  into one readable string.

## Infrastructure gotcha

An instance behind Cloudflare rejects default Python
HTTP user agents with `403 error code: 1010` — an HTML WAF page, not a Planka
error. Anything that isn't a known bot UA passes. Hence `PLANKA_USER_AGENT`,
defaulting to `planka-mcp/0.1`.

## Authoring endpoints (used by the write tools)

| Need | Call |
| --- | --- |
| Create a card | `POST /lists/{listId}/cards` — `type` is **required** (`project`/`story`) |
| Edit a card | `PATCH /cards/{id}` — moving needs `listId` **and** `position` |
| Apply a label | `POST /cards/{cardId}/card-labels {labelId}` — `409` if already applied, treated as success |
| Checklist container | `POST /cards/{cardId}/task-lists {name, position}` |
| Checklist item | `POST /task-lists/{taskListId}/tasks {name, position}` — `position` required |
| Tick an item | `PATCH /tasks/{id} {isCompleted}` |
| Link attachment | `POST /cards/{cardId}/attachments` — **multipart/form-data even for links**: `type=link`, `url`, `name` |

The link attachment is the one that bites: the route has no JSON body variant,
so the fields go through `multipart/form-data` with no file part
(`client.attach_link`).

## Role model (2.4.1, verified by probing the validation whitelists)

```
instance role   admin | projectOwner | boardUser | guestOnly
board role      editor | worker | guest
```

Sending an invalid enum value returns the real whitelist in the `problems[]`
text — that is how every table in this file was confirmed against the running
server rather than the spec.

How this server folds the two axes into one effective role per board
(`roles.resolve_effective_role`):

| Condition | Effective role |
| --- | --- |
| instance role `admin` | `admin` |
| project manager of the parent project | `admin` |
| board membership `editor` | `editor` |
| board membership `worker` | `worker` |
| board membership `guest`, or no membership | `guest` (read only) |

`PLANKA_ACT_AS` lowers this ceiling but never raises it.
