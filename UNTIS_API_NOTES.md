# WebUntis API reconnaissance

These notes describe implementation details observed in the public WebUntis
frontend bundles. They are not an official or stable API contract. Every MCP
operation must still be capability-driven, scoped to the signed-in user, and
verified against the connected tenant.

## Observed deployment

- Classic frontend bundle: `WebUntis/static/2027.1.6/js/untis/main.js`
- New frontend environment version: `1.96.0-20260821`
- New frontend uses both REST endpoints and selected legacy JSON-RPC services.
- The configured event transport is `wss://events.webuntis.com/`.

The MCP must discover bundle/environment versions at runtime instead of
hard-coding these values.

## Authentication and session

### Browser login

The classic login posts URL-encoded form data to
`/WebUntis/j_spring_security_check` with these fields:

- `school`
- `j_username`
- `j_password`
- `token` (second-factor flow, when requested)
- `newPassword` (forced-password-change flow, when requested)

Observed response states include `SUCCESS`, `NO_MANDANT`,
`MUST_SET_PASSWORD`, `MUST_UPDATE_LEGACY_PASSWORD`, `TOKEN_REQUIRED`, and
`LOGIN_ERROR`. The resulting WebUntis session cookie must remain in one HTTP
session. Password changes and second-factor challenges should be completed
interactively rather than guessed by the MCP.

### REST bearer token

After cookie login, the new frontend performs `GET /api/token/new`. A
successful response body is a JWT string. Subsequent REST requests set:

- `Authorization: Bearer <JWT>`
- `Tenant-Id: <tenant id>`
- `X-Webuntis-Api-School-Year-Id: <school year id>` where applicable

The JWT contains user/person IDs, roles and API permissions. The frontend
refreshes an expiring token through `/api/token/new` and redirects to the login
page when the cookie session has expired. Session status is checked with
`POST /api/rest/view/v1/session/status` and a JSON body.

Tokens, cookies, passwords and personally identifiable response values must
never be logged or returned by diagnostic MCP tools.

## Sanitized live capture

A logged-in student session confirmed these calls and response structures:

- `GET /api/rest/view/v1/timetable/grid?timetableType=...` returns display
  formats, weekday/time-grid definitions and slot durations.
- `GET /api/rest/view/v1/timetable/filter` returns the preselected resource and
  the classes/students/teachers/rooms visible to the account.
- `GET /api/rest/view/v1/timetable/entries` returns days with resource metadata,
  grid entries, period IDs, durations, status, layout positions, subjects,
  teachers, rooms, lesson text and substitution text.
- `GET /api/rest/view/v1/timetable/entries/settings` returns visibility and
  highlighting flags.
- `GET /api/rest/view/v1/messages` returns incoming-message summaries including
  sender shape, timestamps, read state, attachment state and reply permissions.
- `GET /api/rest/view/v1/messages/status` returns the unread count.

The capture contained only GET traffic. It does not yet establish the concrete
JSON DTO fields for absence/homework filters or any write operation.

## REST surface found in the bundle

The bundle contains generated API clients, including parameter assertions,
HTTP methods and paths. Important user-facing groups include:

### Bootstrap and permissions

- `GET /api/rest/view/v1/schoolyears`
- `GET /api/rest/view/v1/app/data`
- `GET /api/rest/view/v1/app/platform-application/menus`
- `GET /api/rest/view/v1/app/third-party/data`
- `GET /api/rest/view/v2/trigger/startup`
- `GET /api/rest/view/v1/messages/status`
- `GET /api/rest/view/v1/messages/permissions`

Menus, JWT permissions and endpoint responses should be used to advertise only
operations available to the current account.

### Timetable

`GET /api/rest/view/v1/timetable/entries` accepts:

- required `start`, `end`, `resourceType`
- optional `format`, comma-separated `resources`, comma-separated
  `periodTypes`, `timetableType`, and `layout`

Other discovered reads include `entriesWeekOverview`, `filter`, `grid`,
`calendar`, `menu`, `search`, `availableRooms`, and timetable settings.

### Absences and class register

- `POST /api/rest/view/v4/classreg/absences` loads absences with a JSON filter.
- `PUT /api/rest/view/v3/classreg/absences` updates an absence with JSON.
- `DELETE /api/rest/view/v1/classreg/absences/{absenceId}` deletes an absence.
- `GET /api/rest/view/v1/classreg/absences/meta` provides form/filter metadata.
- `POST /api/rest/view/v1/classreg/homework/list` loads homework with JSON.
- `GET /api/rest/view/v1/classreg/homework/meta` provides homework metadata.
- Lesson-topic and open-period endpoints are also present.

Exact request DTO fields must come from sanitized live captures before write
tools are enabled.

### Messages

- Inbox/sent/draft reads exist in versions 1 and 2.
- `POST /api/rest/view/v2/messages` sends multipart data with a JSON `request`
  part and zero or more `attachments` parts.
- `POST /api/rest/view/v2/messages/users` is the corresponding user-recipient
  flow.
- Draft create/update, reply, revoke, read-confirmation, attachment and bulk
  delete operations are present.

A future MCP must require explicit confirmation before sending, replying,
revoking or deleting messages.

### Exams

Student, guardian and class reads use:

- `GET /api/rest/view/v1/exams/for-student`
- `GET /api/rest/view/v1/exams/for-guardian`
- `GET /api/rest/view/v1/exams/for-class`

They accept optional `start` and `end` dates. Administrative exam creation,
editing, locks, types and statistics are also present but must be permission
gated.

### Additional groups

The generated clients expose rooms, buildings, subjects, teachers, students,
student duties, absence reasons, calendar entries, iCalendar subscriptions,
files, profile settings, parent-teacher days, platform applications and
advanced timetable-planning endpoints. Presence in the JavaScript bundle does
**not** imply that the current user is authorized to use them.

## Legacy JSON-RPC

The classic helper posts JSON-RPC 2.0 envelopes to
`/WebUntis/jsonrpc_web/<service>` with this shape:

```json
{"id": 0, "method": "methodName", "params": [], "jsonrpc": "2.0"}
```

The public login page was observed calling `jsonCalendarService`. JSON-RPC is a
fallback for functionality not exposed through the newer REST clients; REST
should be preferred where the current frontend already uses it.

## MCP implementation sequence

1. Implement school URL discovery, cookie login and interactive handling of
   token/password-change states.
2. Exchange the cookie session for the short-lived REST JWT and refresh it
   without exposing it to MCP responses.
3. Add read-only diagnostics, current-user capability discovery, school years,
   timetable, homework, absences, exams and message summaries.
4. Add a constrained generic REST/JSON-RPC diagnostic call that blocks foreign
   origins and redacts sensitive data.
5. Enable write operations only after their request DTOs have been captured and
   tested. Require explicit confirmation for messages, absence changes,
   calendar changes and deletions.
6. Add fixture-based tests for token expiry, role restrictions, pagination,
   malformed responses and tenant-origin enforcement.
