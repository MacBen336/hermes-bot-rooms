# hermes-bot-rooms

A backend plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/docs) that puts **Bot
Mode group rooms** into the web dashboard.

The plugin has **no room storage of its own**. It reads and writes the group projection that the
desktop app mirrors into the gateway metadata: `ui_meta["hermes-bots-groups"]` inside
`profile.yaml` of the `default` profile. That means the desktop app is the source of truth for
rooms and membership.

## What it can do

- Show a room with its members and message history.
- Append a message to a room as the user, or as one of the room's members.
- Probe the gateway for the methods the plugin relies on.

## Requirements

- Hermes Agent with dashboard plugin support.
- A `default` profile whose `profile.yaml` carries the group projection, i.e. the desktop app has
  created groups at least once.

## Install

```
<HERMES_HOME>/plugins/hermes-bot-rooms/
```

Then restart the gateway and the dashboard. The tab appears after *Sessions*.

## Backend routes

Namespace `/api/plugins/hermes-bot-rooms/`:

| Route | Purpose |
|---|---|
| `GET /state` | rooms, members and the projection source |
| `GET /room` | one room with its history |
| `POST /send` | append a message — without `sender` as the user (`kind=user`, name `You`), with `sender=<member>` as that member (`kind=member`) |
| `GET /probe` | gateway methods this plugin needs (`session.create`, `prompt.submit`, …) |

A `sender` that is not a member of the room is rejected.

## Status

Version 0.2.1.
