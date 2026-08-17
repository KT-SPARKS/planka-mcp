# API coverage

Generated against the published Planka spec (**70 paths, 100
operations**) and checked against the client source, so it does not drift.

**35 of 100 operations are wired**, plus `GET /users/me`, which the spec
omits. The rest are either out of scope for an agent or a deliberate refusal —
each is accounted for below.

The tool surface is intentionally *not* one tool per endpoint. 33 tools cover the
35 operations, because a tool matches an intention ("claim this task")
rather than a route.

## Parameter coverage on the endpoints we use

Being wired is not the same as being fully exposed. Every field Planka accepts on
the endpoints this server calls, and whether a tool can set it:

| Field | Endpoint | Set by |
| --- | --- | --- |
| `name`, `type`, `description` | `POST/PATCH /projects` | `create_board`, `update_board` |
| `isHidden`, `isFavorite` | `PATCH /projects` | `update_board` |
| `ownerProjectManagerId` | `PATCH /projects` | — internal only; nulling it converts private → shared |
| `backgroundType`, `backgroundGradient`, `backgroundImageId` | `PATCH /projects` | — cosmetic, not exposed |
| `name`, `position`, `type` | `POST /projects/{id}/boards` | `create_project`, `copy_project_structure` |
| `defaultView`, `defaultCardType`, `limitCardTypesToDefaultOne`, `alwaysDisplayCardCreator`, `expandTaskListsByDefault` | `PATCH /boards/{id}` | `create_project`, `update_project` |
| `importType`, `importFile` | `POST /projects/{id}/boards` | — Trello import, not exposed |
| `isSubscribed` | `PATCH /boards`, `PATCH /cards` | — an agent does not need notifications |
| `name`, `position`, `type`, `color` | lists | `create_list`, `update_list` (`after=` computes position) |
| `boardId` | `PATCH /lists/{id}` | — moving a whole list between projects is not exposed |
| `name`, `position`, `color` | labels | `manage_labels` (`colors=` per label) |
| `name`, `description`, `type`, `position`, `dueDate` | `POST /lists/{id}/cards` | `create_task` (`into_list=` picks the stage) |
| `name`, `description`, `type`, `dueDate`, `isDueCompleted` | `PATCH /cards/{id}` | `update_task_details` |
| `listId`, `boardId`, `position` | `PATCH /cards/{id}` | `update_status`, `move_task` |
| `stopwatch` | cards | — time tracking is not exposed |
| `coverAttachmentId` | `PATCH /cards/{id}` | — cover images are not exposed |
| `name`, `position`, `isCompleted`, `assigneeUserId`, `linkedCardId` | tasks | `update_checklist` (`assign_items=`, `depends_on_tasks=`) |
| `taskListId` | `PATCH /tasks/{id}` | — moving an item between checklists is not exposed |
| `userId`, `role`, `canComment` | board memberships | `assign_people`, `set_project_members` |
| `type`, `url`, `name` | `POST /cards/{id}/attachments` | `attach_link` — `type=link` only, no file upload |
| `text` | comments | `add_comment`, `add_result` |
| `role` | `PATCH /users/{id}` | `admin_manage_person` — never to `admin`, never its own account |
| `apiKey` | `PATCH /users/{id}` | `admin_manage_person(revoke_api_key)` |
| `email`, `password`, `username`, `avatar`, `isDeactivated` | `PATCH /users/{id}` | — account lifecycle stays with a human |

## The gaps that matter

1. **Custom fields are entirely unexposed** (9 operations). A board that keeps
   real data in custom fields — a client name, an invoice number — is partly
   invisible: `get_task` will not show it and no tool can set it. This is the
   largest functional hole.
2. **No bulk list operations.** `POST /lists/{id}/clear`, `/sort` and
   `/move-cards` are unwired. Sorting or emptying a column takes one call in the
   API and many through `move_task`.
3. **No task duplication.** `POST /cards/{id}/duplicate` would make templated
   work cheap.
4. **No file attachments.** Only links. An agent producing a file has to host it
   somewhere and attach the URL.
5. **No time tracking.** The `stopwatch` field is ignored.

Everything else absent is a deliberate refusal rather than an oversight: no hard
deletion of anything holding work, no editing other people's comments, no
account lifecycle, no instance configuration.

## Coverage by area

### `access-tokens` — 1/5

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `POST /access-tokens` | User login | internal (login) |
| `POST /access-tokens/accept-terms` | Accept terms and conditions | — |
| `POST /access-tokens/exchange-with-oidc` | Exchange OIDC code for access token | — |
| `DELETE /access-tokens/me` | User logout | — |
| `POST /access-tokens/revoke-pending-token` | Revoke pending token | — |

Login is wired. Logout, OIDC exchange and terms acceptance are not: the server holds one credential for its lifetime.

### `attachments` — 0/2

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /attachments/{id}` | Delete attachment | — |
| `PATCH /attachments/{id}` | Update attachment | — |

File upload and attachment editing are out of scope; `attach_link` covers the case that matters for an agent reporting work.

### `background-images` — 0/1

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /background-images/{id}` | Delete background image | — |

Cosmetic.

### `base-custom-field-groups` — 0/3

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `POST /base-custom-field-groups/{baseCustomFieldGroupId}/custom-fields` | Create custom field in base custom field group | — |
| `DELETE /base-custom-field-groups/{id}` | Delete base custom field group | — |
| `PATCH /base-custom-field-groups/{id}` | Update base custom field group | — |

**Real gap.** Same as custom-fields.

### `board-memberships` — 2/2

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /board-memberships/{id}` | Delete board membership | `set_project_members(role='none')` |
| `PATCH /board-memberships/{id}` | Update board membership | `set_project_members` |

### `boards` — 5/9

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `GET /boards/{boardId}/actions` | Get board actions | — |
| `POST /boards/{boardId}/board-memberships` | Create board membership | `assign_people`, `set_project_members`, `copy_project_structure` |
| `POST /boards/{boardId}/custom-field-groups` | Create board custom field group | — |
| `POST /boards/{boardId}/labels` | Create label | `manage_labels`, `copy_project_structure` |
| `POST /boards/{boardId}/lists` | Create list | `create_list`, `create_project`, `copy_project_structure` |
| `POST /boards/{boardId}/notification-services` | Create notification service for board | — |
| `DELETE /boards/{id}` | Delete board | — |
| `GET /boards/{id}` | Get board details | every read — one call returns the whole graph |
| `PATCH /boards/{id}` | Update board | `update_project`, `create_project` (settings) |

Board deletion is deliberately absent — nothing holding work is deletable. Action feeds and duplication are unexposed.

### `bootstrap` — 0/1

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `GET /bootstrap` | Get application bootstrap | — |

Instance metadata; nothing needs it.

### `cards` — 10/17

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `GET /cards/{cardId}/actions` | Get card actions | — |
| `POST /cards/{cardId}/attachments` | Create attachment | `attach_link` |
| `POST /cards/{cardId}/card-labels` | Add label to card | `set_task_labels`, `create_task` |
| `DELETE /cards/{cardId}/card-labels/labelId:{labelId}` | Remove label from card | `set_task_labels(remove=…)` |
| `POST /cards/{cardId}/card-memberships` | Add user to card | `claim_task`, `assign_people` |
| `DELETE /cards/{cardId}/card-memberships/userId:{userId}` | Remove user from card | `release_task`, `unassign_people` |
| `POST /cards/{cardId}/comments` | Create comment | `add_comment`, `add_result`, `release_task` |
| `GET /cards/{cardId}/comments` | Get card comments | `get_task`, `find_tasks`, `find_informal_assignments` |
| `POST /cards/{cardId}/custom-field-groups` | Create card custom field group | — |
| `DELETE /cards/{cardId}/custom-field-value/customFieldGroupId:{customFieldGroupId}:customFieldId:${customFieldId}` | Delete custom field value | — |
| `PATCH /cards/{cardId}/custom-field-values/customFieldGroupId:{customFieldGroupId}:customFieldId:${customFieldId}` | Create or update custom field value | — |
| `POST /cards/{cardId}/task-lists` | Create task list | `update_checklist`, `create_task` |
| `DELETE /cards/{id}` | Delete card | — |
| `GET /cards/{id}` | Get card details | `get_task`, `claim_task`, every card write |
| `PATCH /cards/{id}` | Update card | `update_status`, `move_task`, `update_task_details` |
| `POST /cards/{id}/duplicate` | Duplicate card | — |
| `POST /cards/{id}/read-notifications` | Mark card notifications as read | — |

Card deletion and duplication are deliberately absent, as are the stopwatch, cover images and the read-notification endpoints.

### `comments` — 0/2

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /comments/{id}` | Delete comment | — |
| `PATCH /comments/{id}` | Update comment | — |

Editing and deleting other people's comments is deliberately absent; the record stays intact.

### `config` — 0/3

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `GET /config` | Get application configuration | — |
| `PATCH /config` | Update application configuration | — |
| `POST /config/test-smtp` | Test SMTP configuration | — |

Instance configuration belongs in the admin UI.

### `custom-field-groups` — 0/4

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `POST /custom-field-groups/{customFieldGroupId}/custom-fields` | Create custom field in custom field group | — |
| `DELETE /custom-field-groups/{id}` | Delete custom field group | — |
| `GET /custom-field-groups/{id}` | Get custom field group details | — |
| `PATCH /custom-field-groups/{id}` | Update custom field group | — |

**Real gap.** Same as custom-fields.

### `custom-fields` — 0/2

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /custom-fields/{id}` | Delete custom field | — |
| `PATCH /custom-fields/{id}` | Update custom field | — |

**Real gap.** A board that stores data in custom fields is partly invisible to the agent.

### `labels` — 2/2

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /labels/{id}` | Delete label | `manage_labels(delete_unused=…)` — unused only |
| `PATCH /labels/{id}` | Update label | `manage_labels` (rename, recolour) |

### `lists` — 3/8

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /lists/{id}` | Delete list | `delete_list` — empty lists only |
| `GET /lists/{id}` | Get list details | — |
| `PATCH /lists/{id}` | Update list | `update_list` |
| `POST /lists/{id}/clear` | Clear list | — |
| `POST /lists/{id}/move-cards` | Move cards | — |
| `POST /lists/{id}/sort` | Sort cards in list | — |
| `POST /lists/{listId}/cards` | Create card | `create_task` |
| `GET /lists/{listId}/cards` | Get cards in list | — |

Clear, sort and move-all-cards are unexposed: each is a bulk, hard-to-review change. `move_task` moves one task at a time.

### `notification-services` — 0/3

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /notification-services/{id}` | Delete notification service | — |
| `PATCH /notification-services/{id}` | Update notification service | — |
| `POST /notification-services/{id}/test` | Test notification service | — |

Instance plumbing.

### `notifications` — 0/4

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `GET /notifications` | Get user notifications | — |
| `POST /notifications/read-all` | Mark all notifications as read | — |
| `GET /notifications/{id}` | Get notification details | — |
| `PATCH /notifications/{id}` | Update notification | — |

The agent has no inbox to triage.

### `project-managers` — 0/1

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /project-managers/{id}` | Delete project manager | — |

Removing a manager is unexposed; adding one is available.

### `projects` — 5/9

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `POST /projects` | Create project | `create_board` |
| `GET /projects` | Get all accessible projects | `list_boards`, `list_projects`, scope resolution |
| `DELETE /projects/{id}` | Delete project | — |
| `GET /projects/{id}` | Get project details | — |
| `PATCH /projects/{id}` | Update project | `update_board` |
| `POST /projects/{projectId}/background-images` | Upload background image | — |
| `POST /projects/{projectId}/base-custom-field-groups` | Create base custom field group | — |
| `POST /projects/{projectId}/boards` | Create board | `create_project`, `copy_project_structure` |
| `POST /projects/{projectId}/project-managers` | Create project manager | `admin_manage_person` (add_board_manager) |

Background image upload and base custom field groups are unexposed.

### `task-lists` — 1/4

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /task-lists/{id}` | Delete task list | — |
| `GET /task-lists/{id}` | Get task list details | — |
| `PATCH /task-lists/{id}` | Update task list | — |
| `POST /task-lists/{taskListId}/tasks` | Create task | `update_checklist` (items and dependencies) |

Deleting a checklist is deliberately absent; items are reopened, not removed.

### `tasks` — 1/2

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `DELETE /tasks/{id}` | Delete task | — |
| `PATCH /tasks/{id}` | Update task | `update_checklist` (tick, reopen, assign) |

Deleting a checklist item is deliberately absent.

### `terms` — 0/1

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `GET /terms` | Get terms and conditions | — |

Unused.

### `users` — 5/11

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `POST /users` | Create user | wired, deliberately refused — needs a password |
| `GET /users` | Get all users | `list_people`, person resolution |
| `DELETE /users/{id}` | Delete user | — |
| `GET /users/{id}` | Get user details | internal (identity fallback) |
| `PATCH /users/{id}` | Update user | `admin_manage_person` (role, key revoke) |
| `POST /users/{id}/api-key` | Create user API key | `admin_manage_person` (create_api_key) |
| `POST /users/{id}/avatar` | Update user avatar | — |
| `PATCH /users/{id}/email` | Update user email | — |
| `PATCH /users/{id}/password` | Update user password | — |
| `PATCH /users/{id}/username` | Update user username | — |
| `POST /users/{userId}/notification-services` | Create notification service for user | — |

Deleting users, avatars, email/password/username changes are deliberately absent: account lifecycle stays with a human.

### `webhooks` — 0/4

| Endpoint | What it is | Exposed as |
| --- | --- | --- |
| `POST /webhooks` | Create webhook | — |
| `GET /webhooks` | Get all webhooks | — |
| `DELETE /webhooks/{id}` | Delete webhook | — |
| `PATCH /webhooks/{id}` | Update webhook | — |

Instance plumbing, not agent work.
