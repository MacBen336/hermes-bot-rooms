"""Backend für hermes-bot-rooms — Bot-Mode-Gruppenräume im Web-Dashboard.

**Datenquelle ist der Stand der Desktop-Umgebung**, keine zweite Raumwelt: die
Gruppen-Projektion, die die Desktop-App in die Profil-Metadaten des Gateways
spiegelt — ``ui_meta["hermes-bots-groups"]`` in ``profile.yaml`` des Profils
``default``. Räume, Mitglieder und Verlauf kommen von dort; ein Post geht als
Log-Eintrag in dieselbe Projektion zurück, mit CAS-Revision, genau wie die App
es macht.

Vorbild aus der App (Desktop-App, belegt 17-10-2026):
  * Schreiben: ``profiles.configure`` mit ``ui_meta`` **und**
    ``ui_meta_expected_revisions`` (CAS) — ``apps/desktop/src/plugins/hermes-bots/
    group-chat.ts`` Params ab :1175, Read-back-Prüfung ab :1194.
  * Raumform: ``{name, holdDetection, members:[{name,handle,connectionId,…}],
    revision, omitted}`` plus ``log:[{id,from:{kind,name},text,at,thread}]``.
  * Die App liest die Revision aus ``profiles.list``; geschrieben wird CAS-pflichtig.

Beide RPC-Methoden werden **in-process** über die Methoden-Registry des Wirts
gerüstet (``tui_gateway.server._methods``). Wichtig: die Methoden antworten im
JSON-RPC-Rahmen — die Nutzlast steckt unter ``result`` (siehe ``_call``).

Harte Regeln nach der Review-Runde vom 17-10-2026:
  * Lese- und Schreibwurzel sind **dieselbe**: ``get_default_hermes_root()``.
    Ein ``HERMES_HOME`` unterhalb von ``~/.hermes`` wird von Hermes ignoriert
    (``hermes_constants.py:219-236``) — deshalb liest dieses Modul nicht über
    ``get_hermes_home()``, sonst schreibt es neben seiner eigenen Lesequelle.
  * Kein synthetischer Leer-Snapshot: fehlt die Projektion, gibt es einen Fehler
    statt einer Struktur, die ein späterer Writer anlegt.
  * CAS ist **Pflicht**. Ohne bekannte Revision wird nicht geschrieben; die
    gesamte ``ui_meta``-Struktur des Keys wird key-wise ersetzt, also ist ein
    Write ohne Compare ein Datenverlust-Risiko.
  * Nach dem Write wird nachgelesen und die Revision geprüft.

Mitglieder-Runden: ``/send`` mit ``turn: true`` (Vorgabe) startet nach dem Write
eine Runde im Daemon-Thread — je adressiertem Mitglied ``session.create``
(``room_plumbing``) → ``prompt.submit`` mit dem Prompt nach
``group-round-prompt.ts`` → Warten auf den Terminalframe über
``session.events.since`` → Antworten gesammelt als ``member``-Einträge zurück in
denselben Thread. Grenzen dieser Fassung: **eine** Runde je Post, sequenziell,
150 s je Mitglied; die Runde lebt im Dashboard-Prozess (Neustart bricht sie ab).

Kein Geheimniswert verlässt dieses Modul; ``/send`` verlangt ``confirm: true``.
Absender: ``/send`` ohne ``sender`` schreibt als **Nutzer** (``kind=user``,
``name="You"`` — die Form der App), mit ``sender`` als **Mitglied** des Raums
(``kind=member``, Name + Verbindungslabel). Der Name muss zu einem Mitglied des
Raums passen; unbekannte Namen liefern 400. Ohne dieses Feld wäre ein Agenten-Post
im Verlauf nicht von einer Zeile des Nutzers zu unterscheiden.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

PLUGIN_ID = "hermes-bot-rooms"
META_KEY = "hermes-bots-groups"
GATEWAY_PROFILE = "default"
LOG_CAP = 200
TEXT_LIMIT = 8000
USER_NAME = "You"
# 64 KB Cap des Wirts für den gesamten ui_meta-Blob (methods_profiles.py:610).
UI_META_CAP = 65536


# ── RPC des Wirts (in-process) ────────────────────────────────────────────────

def _methods():
    from tui_gateway import server  # noqa: PLC0415 — nur im Wirt vorhanden
    methods = getattr(server, "_methods", None)
    if not isinstance(methods, dict) or "profiles.configure" not in methods:
        raise HTTPException(
            status_code=503,
            detail="Methoden-Registry des Wirts fehlt (profiles.configure nicht registriert)")
    return methods


def _call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """RPC-Aufruf des Wirts und **Auspacken** der Antwort.

    Die Host-Methoden antworten im JSON-RPC-Rahmen
    ``{"jsonrpc","id","result"}`` bzw. ``{"jsonrpc","id","error"}`` — die
    Nutzlast steckt unter ``result``. Wer nur die oberste Ebene liest, hält
    jede erfolgreiche Antwort für leer (belegt: ``applied = {}``).
    """
    rid = f"bot-rooms-{secrets.token_hex(6)}"
    try:
        response = _methods()[method](rid, params)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{method} im Wirt fehlgeschlagen: {type(exc).__name__}: {exc}") from exc
    if not isinstance(response, dict):
        raise HTTPException(status_code=502, detail=f"{method} lieferte kein Objekt")
    if "error" in response:
        error = response.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        raise HTTPException(status_code=400, detail=f"{method}: {detail}")
    payload = response.get("result")
    return payload if isinstance(payload, dict) else response


def _home() -> Path:
    """Schreib- und Lesewurzel: dieselbe, die der Wirt für ``default`` nutzt."""
    from hermes_constants import get_default_hermes_root  # noqa: PLC0415
    return Path(str(get_default_hermes_root()))


def _gateway_row() -> dict[str, Any] | None:
    """Profil-Zeile des Gateways (``ui_meta`` + ``ui_meta_revisions``) oder None."""
    result = _call("profiles.list", {"include_sessions": False})
    rows = result.get("profiles")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("name") == GATEWAY_PROFILE:
            return row
    return None


def _revisions_of(row: dict[str, Any]) -> int:
    revisions = row.get("ui_meta_revisions")
    if not isinstance(revisions, dict):
        revisions = row.get("_ui_meta_revisions")
    if not isinstance(revisions, dict):
        return 0
    return int(revisions.get(META_KEY) or 0)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _existing_meta() -> dict[str, Any]:
    """Alle ``ui_meta``-Keys der Zieldatei — der Wirt mittelt sie mitgeschrieben."""
    path = _home() / "profile.yaml"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    meta = doc.get("ui_meta") if isinstance(doc, dict) and isinstance(doc.get("ui_meta"), dict) else {}
    return dict(meta)


def _ui_meta_blob_bytes(snapshot: dict[str, Any]) -> int:
    """Byte-Größe des Blobs, den der Wirt tatsächlich prüfen wird (Cap 64 KB).

    Der Wirt misst ``json.dumps`` über **alle** mitgegebenen ``ui_meta``-Keys
    (methods_profiles.py:610-612) — nicht nur über unseren. Also werden die
    bestehenden Keys mitgezählt und Bytes gemessen, nicht Zeichen.
    """
    blob = json.dumps({**_existing_meta(), META_KEY: snapshot})
    return len(blob.encode("utf-8"))


def _snapshot() -> tuple[dict[str, Any], int]:
    """Projektion und Revision — oder ein Fehler, aber **kein** Ersatzobjekt.

    Gelesen wird die Datei, aus der auch der CAS-Wirt liest
    (``_ui_meta_revisions``). Trägt die Datei keine Revision für diesen Key, wird
    sie über ``profiles.list`` nachgefragt — sonst würde CAS-Pflicht jeden Write
    blockieren, obwohl der Wirt eine Revision kennt. Erst wenn beide Wege keine
    Projektion liefern, gibt es eine Fehlermeldung.
    """
    path = _home() / "profile.yaml"
    file_error: str | None = None
    if path.is_file():
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            file_error = f"{path}: {type(exc).__name__}: {exc}"
            doc = None
        if isinstance(doc, dict):
            meta = doc.get("ui_meta") if isinstance(doc.get("ui_meta"), dict) else {}
            revisions = doc.get("_ui_meta_revisions")
            snapshot = meta.get(META_KEY)
            if isinstance(snapshot, dict):
                revision = _as_int(revisions.get(META_KEY)) if isinstance(revisions, dict) else 0
                if not revision:
                    row = _gateway_row()
                    if row is not None:
                        revision = _revisions_of(row)
                return snapshot, revision
            file_error = f"{path}: ui_meta.{META_KEY} fehlt"
    else:
        file_error = f"{path} nicht vorhanden"

    row = _gateway_row()
    if row is not None:
        meta = row.get("ui_meta") if isinstance(row.get("ui_meta"), dict) else {}
        snapshot = meta.get(META_KEY)
        if isinstance(snapshot, dict):
            return snapshot, _revisions_of(row)

    raise HTTPException(
        status_code=503,
        detail=("Projektion nicht lesbar — kein Reset auf leere Räume. "
                f"Datei: {file_error}"))


# ── Anzeigeformen (keine Geheimnisse) ─────────────────────────────────────────

def _members(room: dict[str, Any]) -> list[dict[str, Any]]:
    raw = room.get("members")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for member in raw:
        if isinstance(member, dict):
            out.append({
                "name": member.get("name"),
                "handle": member.get("handle") or member.get("name"),
                "connection_id": member.get("connectionId"),
                "connection_label": member.get("connectionLabel"),
            })
    return out


def _entry_view(entry: Any) -> dict[str, Any]:
    row = dict(entry) if isinstance(entry, dict) else {}
    sender = row.get("from") if isinstance(row.get("from"), dict) else {}
    at = row.get("at")
    at_iso = None
    if isinstance(at, (int, float)) and at:
        try:
            at_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(at) / 1000.0))
        except (TypeError, ValueError, OSError):
            at_iso = None
    return {
        "id": row.get("id"),
        "sender_kind": sender.get("kind"),
        "sender_name": sender.get("name"),
        "sender_source": sender.get("source"),
        "text": row.get("text"),
        "at": at,
        "at_iso": at_iso,
        "thread": row.get("thread"),
    }


def _room_view(key: str, room: Any) -> dict[str, Any]:
    row = dict(room) if isinstance(room, dict) else {}
    log = row.get("log") if isinstance(row.get("log"), list) else []
    return {
        "key": key,
        "name": row.get("name") or key,
        "members": _members(row),
        "revision": row.get("revision"),
        "omitted": row.get("omitted"),
        "hold_detection": bool(row.get("holdDetection", True)),
        "entry_count": len(log),
        "last": _entry_view(log[-1]) if log else None,
    }


# ── Mitglieder-Runden (wie die Desktop-App) ──────────────────────────────────
#
# Vorbild: apps/desktop/src/plugins/hermes-bots/group-round-members.ts (Prompt +
# Reihenfolge) und group-turns.ts (session.create → prompt.submit → Terminalframe).
# Der Server hat keinen Event-Tap wie die App; deshalb wird `session.events.since`
# gepollt, bis eine Antwort des Mitglieds im Transkript steht.

TURN_TIMEOUT_S = 150.0
POLL_EVERY_S = 2.0
DELTA_ENTRIES = 12
BOT_TEXT_LIMIT = 4000
GROUP_DELTA_HEADER = "New messages in the room since your last turn (oldest first):"
GROUP_RULES = [
    "Rules for this room:",
    ("- Reply with ONE conversational message ONLY if you have something new worth adding: build on "
     "what was just said, claim or hand off work, answer a question aimed at you, or report a real "
     "result. Keep chatter short (1-3 sentences) — but when you are delivering a result, an answer "
     "the user asked for, or substantive work, give it at full quality and length; never thin out "
     "real content to fit the room."),
    '- If you have nothing new to add, reply with exactly "(pass)". Passing is good — it lets the '
    "conversation settle.",
    ("- Mention a teammate as @name to pull them in; mention @user only for a judgment call or a "
     "result the user needs. Do not repeat points already made."),
    "- Never reveal content from your private 1:1 chats. Your reply text goes to the room verbatim "
    "— no preamble, no meta-commentary.",
]

MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9._:-]{0,63})")
RESERVED_MENTIONS = frozenset({"all", "everyone", "user", "hermes"})
_runs: dict[str, dict[str, Any]] = {}
_runs_lock = threading.Lock()


def _mint_thread() -> str:
    """Thread-ID wie ``mintGroupThreadId`` im Desktop: ``t<base36-ms>-<5 Zeichen>``."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    n = int(time.time() * 1000)
    stamp = ""
    while n:
        n, rest = divmod(n, 36)
        stamp = alphabet[rest] + stamp
    return f"t{stamp}-{''.join(secrets.choice(alphabet) for _ in range(5))}"


def _thread_of(entry: Any) -> str:
    """Thread-Zuordnung wie ``groupThreadOf``: Einträge ohne Thread sind ``legacy``."""
    row = entry if isinstance(entry, dict) else {}
    return str(row.get("thread") or "legacy")


def _addressed_members(members: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """@-Scoping wie die App: nur genannte Mitglieder, sonst alle.

    ``@all`` / ``@everyone`` (und ``@user``) bedeuten das ganze Haus; unbekannte
    ``@``-Wörter zählen nicht.
    """
    tags = {match.group(1).lower() for match in MENTION_RE.finditer(text or "")}
    if not tags or (tags & RESERVED_MENTIONS):
        return list(members)
    picked = [member for member in members
              if str(member.get("handle") or member.get("name") or "").lower() in tags]
    return picked


def _prompt_for(room_name: str, members: list[dict[str, Any]], handle: str,
                entries: list[dict[str, Any]], thread: str = "legacy") -> str:
    """Prompt wie ``buildGroupChatTurnPrompt`` im Desktop."""
    peers = []
    for member in members:
        tag = str(member.get("handle") or member.get("name") or "")
        if not tag or tag == handle:
            continue
        label = member.get("connection_label") or member.get("connection_id")
        peers.append(f"@{tag} [on {label}]" if label else f"@{tag}")
    lines = [
        (f'[Group chat: "{room_name}"] You are @{handle}, one participant in a group chat with '
         f'{", ".join(peers) or "no one else yet"} and the user.'),
        "",
        GROUP_DELTA_HEADER,
    ]
    for entry in entries[-DELTA_ENTRIES:]:
        who = entry.get("sender_name") or "?"
        lines.append(f"  {who}: {entry.get('text') or ''}")
    lines.append("")
    lines.extend(GROUP_RULES)
    return "\n".join(lines)


def _session_create(profile: str, title: str) -> tuple[str | None, str | None]:
    """Plumbing-Session eines Mitglieds (wie ``session.create`` in der App)."""
    result = _call("session.create", {
        "profile": profile,
        "title": title,
        "hidden": True,
        "room_plumbing": True,
        "follow_profile_config": True,
    })
    session = result.get("session") if isinstance(result.get("session"), dict) else result
    runtime = session.get("session_id") or session.get("id")
    stored = session.get("stored_session_id") or runtime
    return (str(runtime) if runtime else None, str(stored) if stored else None)


def _frame_text(value: Any) -> str:
    """Text aus einem Event-Payload; Blocklisten werden zusammengezogen."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _frame_of(event: Any) -> tuple[str, str]:
    """``(Art, Text)`` eines Replay-Frames — die Wirtsformen sind nicht einheitlich.

    Beobachtet: ``type``/``text`` direkt am Frame und ``text`` unter ``payload``.
    Deshalb beide Ebenen abklopfen, statt eine Form zu raten.
    """
    if not isinstance(event, dict):
        return "", ""
    layers: list[dict[str, Any]] = [event]
    for key in ("params", "payload"):
        inner = event.get(key)
        if isinstance(inner, dict):
            layers.append(inner)
            nested = inner.get("payload")
            if isinstance(nested, dict):
                layers.append(nested)
    kind = ""
    for layer in layers:
        kind = str(layer.get("type") or layer.get("event") or "")
        if kind:
            break
    text = ""
    for layer in layers:
        text = _frame_text(layer.get("text")) or _frame_text(layer.get("content"))
        if text.strip():
            break
    return kind, text


def _reply_of(session_id: str, deadline: float, since: int = 0) -> tuple[str | None, str | None]:
    """Wartet auf den **Terminalframe** des Tuns (``message.complete`` / ``error``).

    Kein Partial, kein Prompt-Echo: nur der Abschluss zählt. Der Terminalframe darf
    leer sein (``response_reused`` — der Text ist schon gestreamt), dann zählt die
    Summe der ``message.delta``-Chunks. Rückgabe ``(Antwort, Fehler)``.
    """
    cursor = _as_int(since)
    stream: list[str] = []
    while time.time() < deadline:
        try:
            page = _call("session.events.since", {"session_id": session_id, "last_seen": cursor})
        except HTTPException as exc:
            return None, f"events: {exc.detail}"
        events = page.get("events") if isinstance(page.get("events"), list) else []
        cursor = max(cursor, _as_int(page.get("latest_seq")))
        for event in events:
            kind, text = _frame_of(event)
            if kind == "message.delta":
                stream.append(text)
                continue
            if kind == "error":
                return None, (text.strip() or "error")
            if kind != "message.complete":
                continue
            body = text.strip() or "".join(stream).strip()
            level = event.get("params") if isinstance(event.get("params"), dict) else event
            inner = level.get("payload") if isinstance(level.get("payload"), dict) else {}
            status = str(inner.get("status") or level.get("status") or "").lower()
            if status in ("error", "failed", "cancelled"):
                return None, (body or f"turn {status}")
            if body:
                return body, None
            return None, "leer"
        time.sleep(POLL_EVERY_S)
    return None, "timeout"


def _append_entries(key: str, entries: list[dict[str, Any]]) -> tuple[int | None, bool]:
    """Mehrere Raum-Einträge in einem CAS-Write anhängen.

    Rückgabe ``(Revision, belegt)``: ``belegt`` ist nur dann wahr, wenn **alle**
    übergebenen IDs nach dem Write im Verlauf derselben Quelle stehen.
    """
    if not entries:
        return None, True
    for attempt in (1, 2):
        snapshot, revision = _snapshot()
        if not revision:
            return None, False
        rooms = snapshot.get("rooms") if isinstance(snapshot.get("rooms"), dict) else {}
        if key not in rooms:
            return None, False
        room_row = rooms[key] if isinstance(rooms[key], dict) else {}
        log = room_row.get("log") if isinstance(room_row.get("log"), list) else []
        new_log = log + entries
        overflow = max(0, len(new_log) - LOG_CAP)
        if overflow:
            new_log = new_log[overflow:]
        omitted = _as_int(room_row.get("omitted")) + overflow
        room_row = {
            **room_row,
            "log": new_log,
            "revision": _as_int(room_row.get("revision")) + 1,
            **({"omitted": omitted} if omitted else {}),
        }
        new_snapshot = {
            **snapshot,
            "updatedAt": int(time.time() * 1000),
            "rooms": {**rooms, key: room_row},
        }
        if _ui_meta_blob_bytes(new_snapshot) > UI_META_CAP:
            return None, False
        result = _call("profiles.configure", {
            "name": GATEWAY_PROFILE,
            "ui_meta": {META_KEY: new_snapshot},
            "ui_meta_expected_revisions": {META_KEY: revision},
        })
        applied = result.get("applied") if isinstance(result.get("applied"), dict) else {}
        if applied.get("ui_meta") is not True:
            if attempt == 1:
                time.sleep(1.0)  # ein CAS-Verlierer (paralleler Writer) bekommt einen zweiten Versuch
                continue
            return None, False
        after_snapshot, after = _snapshot()
        after_rooms = after_snapshot.get("rooms") if isinstance(after_snapshot.get("rooms"), dict) else {}
        after_room = after_rooms.get(key) if isinstance(after_rooms.get(key), dict) else {}
        after_log = after_room.get("log") if isinstance(after_room.get("log"), list) else []
        present = {str(e.get("id")) for e in after_log if isinstance(e, dict)}
        found = all(str(entry.get("id")) in present for entry in entries)
        return after, found
    return None, False


def _run_round(run_id: str, key: str, room_name: str, members: list[dict[str, Any]],
               entries: list[dict[str, Any]], thread: str,
               addressed: list[dict[str, Any]]) -> None:
    """Eine Runde: jedes adressierte Mitglied einmal, Antworten danach gesammelt."""
    def status(**fields: Any) -> None:
        with _runs_lock:
            run = _runs.get(run_id)
            if run is not None:
                run.update(fields)

    answers: list[dict[str, Any]] = []
    errors: list[str] = []
    passes: list[str] = []
    try:
        for member in addressed:
            handle = str(member.get("handle") or member.get("name") or "")
            profile = str(member.get("name") or handle)
            if not handle or not profile:
                continue
            try:
                runtime, _stored = _session_create(profile, f"Group: {room_name}")
                if not runtime:
                    errors.append(f"{handle}: keine Session")
                    continue
                prompt = _prompt_for(room_name, members, handle, entries, thread)
                _call("prompt.submit", {"session_id": runtime, "text": prompt})
                reply, error = _reply_of(runtime, time.time() + TURN_TIMEOUT_S)
            except HTTPException as exc:
                errors.append(f"{handle}: {exc.detail}")
                continue
            if error:
                errors.append(f"{handle}: {error}")
                continue
            if not reply or reply.strip() == "(pass)":
                # `(pass)` ist die vereinbarte Antwort für "nichts Neues" — Beleg, kein Fehler.
                if reply and reply.strip() == "(pass)":
                    passes.append(handle)
                continue
            answers.append({
                "id": f"{secrets.token_hex(8)}-{secrets.token_hex(4)}-{secrets.token_hex(4)}",
                "from": {"kind": "member", "name": handle, "source": "Home"},
                "text": reply[:BOT_TEXT_LIMIT],
                "at": int(time.time() * 1000),
                "thread": thread,
            })
        revision: int | None = None
        written = True
        if answers:
            revision, written = _append_entries(key, answers)
        status(answers=len(answers), revision=revision, written=written,
               state="done" if (not answers or written) else "write_failed",
               passed=passes, errors=errors[-3:])
    except Exception as exc:  # pragma: no cover — Runde darf den Host nicht reißen
        status(state="failed", errors=[f"{type(exc).__name__}: {exc}"])
    finally:
        with _runs_lock:
            if len(_runs) > 20:
                stale = sorted(_runs, key=lambda k: _runs[k].get("started_at") or 0)
                for key_name in stale[:len(_runs) - 20]:
                    _runs.pop(key_name, None)




class SendBody(BaseModel):
    key: str
    text: str
    thread: str | None = None
    turn: bool = True
    confirm: bool = False
    # `sender` schreibt den Eintrag als **Mitglied** des Raums (kind=member) —
    # für Posts eines Agenten. Ohne das Feld bleibt es der Nutzer-Post
    # {"kind": "user", "name": "You"}, genau in der Form, die die App für die
    # eigenen Zeilen des Nutzers schreibt.
    sender: str | None = None


SENDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _author_of(sender: str | None, room: dict[str, Any]) -> dict[str, Any]:
    """Absender des Eintrags: ohne ``sender`` der Nutzer, mit ``sender`` das Mitglied.

    Der Name muss ein **Mitglied dieses Raums** sein (Handle oder Name, führendes
    ``@`` erlaubt). Ein unbekannter Name wird abgewiesen: sonst entstünde ein
    Sprecher, den die App keinem Bot zuordnen kann — sie zeigt ihn als rohen
    Namen ohne Avatar und die Mitglieder lesen ihn als Fremden.
    """
    wanted = str(sender or "").strip().lstrip("@")
    if not wanted:
        return {"kind": "user", "name": USER_NAME}
    if not SENDER_RE.match(wanted):
        raise HTTPException(status_code=400, detail=f"Absender unbrauchbar: {wanted[:64]!r}")
    members = _members(room)
    for member in members:
        names = [str(member.get("name") or ""), str(member.get("handle") or "")]
        if wanted.lower() in {name.lower() for name in names if name}:
            name = str(member.get("name") or member.get("handle") or wanted)
            label = member.get("connection_label") or member.get("connection_id")
            # Die App schreibt `source` nur, wenn die Verbindung ein Label hat
            # (`groupMemberAuthor`, group-membership.ts:65-77).
            return {"kind": "member", "name": name, **({"source": label} if label else {})}
    known = ", ".join(sorted({str(m.get("handle") or m.get("name") or "") for m in members}))
    raise HTTPException(
        status_code=400,
        detail=f"Kein Mitglied '{wanted}' in diesem Raum (bekannt: {known or 'keine'})")


def _require_confirm(flag: bool) -> None:
    if not flag:
        raise HTTPException(status_code=400, detail="confirm fehlt: schreibende Aktion ohne Bestätigung")


# ── Routen ────────────────────────────────────────────────────────────────────

@router.get("/state")
def state() -> dict[str, Any]:
    """Die Räume der Desktop-Umgebung, wie sie in der Projektion stehen."""
    snapshot, revision = _snapshot()
    rooms = snapshot.get("rooms") if isinstance(snapshot.get("rooms"), dict) else {}
    view = [_room_view(key, room) for key, room in rooms.items()]
    view.sort(key=lambda r: (r.get("last") or {}).get("at") or 0, reverse=True)
    return {
        "plugin": PLUGIN_ID,
        "source": f"{GATEWAY_PROFILE}:profile.yaml#ui_meta.{META_KEY}",
        "root": str(_home()),
        "revision": revision,
        "updated_at": snapshot.get("updatedAt"),
        "version": snapshot.get("version"),
        "rooms": view,
        "log_cap": LOG_CAP,
    }


@router.get("/room")
def room(key: str) -> dict[str, Any]:
    """Ein Raum mit vollem Verlauf aus der Projektion."""
    snapshot, revision = _snapshot()
    rooms = snapshot.get("rooms") if isinstance(snapshot.get("rooms"), dict) else {}
    if key not in rooms:
        raise HTTPException(status_code=404, detail=f"Raum nicht in der Projektion: {key}")
    raw = rooms[key] if isinstance(rooms[key], dict) else {}
    log = raw.get("log") if isinstance(raw.get("log"), list) else []
    return {
        "room": _room_view(key, raw),
        "revision": revision,
        "log": [_entry_view(e) for e in log],
    }


@router.post("/send")
def send(body: SendBody) -> dict[str, Any]:
    """Eine Nachricht an den Raum-Verlauf anhängen (CAS-pflichtig, mit Read-back)."""
    _require_confirm(body.confirm)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text fehlt")
    if len(text) > TEXT_LIMIT:
        raise HTTPException(status_code=400, detail=f"Text zu lang (max {TEXT_LIMIT} Zeichen)")

    snapshot, revision = _snapshot()
    if not revision:
        raise HTTPException(
            status_code=409,
            detail="Keine Revision lesbar — ohne Compare-and-Swap wird nicht geschrieben "
                   "(die Projektion wird key-wise ersetzt).")
    rooms = snapshot.get("rooms") if isinstance(snapshot.get("rooms"), dict) else {}
    if body.key not in rooms:
        raise HTTPException(status_code=404, detail=f"Raum nicht in der Projektion: {body.key}")

    room_row = rooms[body.key] if isinstance(rooms[body.key], dict) else {}
    log = room_row.get("log") if isinstance(room_row.get("log"), list) else []
    entry = {
        "id": f"{secrets.token_hex(8)}-{secrets.token_hex(4)}-{secrets.token_hex(4)}",
        "from": _author_of(body.sender, room_row),
        "text": text,
        "at": int(time.time() * 1000),
        "thread": (body.thread or "").strip() or _mint_thread(),
    }
    # Kürzen nur wie die App: der Überhang wandert in `omitted`, sonst verschwindet er still.
    new_log = log + [entry]
    overflow = max(0, len(new_log) - LOG_CAP)
    if overflow:
        new_log = new_log[overflow:]
    omitted = _as_int(room_row.get("omitted")) + overflow
    room_row = {
        **room_row,
        "log": new_log,
        "revision": _as_int(room_row.get("revision")) + 1,
        **({"omitted": omitted} if omitted else {}),
    }
    new_snapshot = {
        **snapshot,
        "updatedAt": int(time.time() * 1000),
        "rooms": {**rooms, body.key: room_row},
    }
    blob_bytes = _ui_meta_blob_bytes(new_snapshot)
    if blob_bytes > UI_META_CAP:
        raise HTTPException(
            status_code=413,
            detail=(f"ui_meta-Blob zu groß für den 64-KB-Cap des Wirts: {blob_bytes} Byte "
                    f"(Grenze {UI_META_CAP}). Der Wirt würde den Schreibvorgang still verwerfen."))

    result = _call("profiles.configure", {
        "name": GATEWAY_PROFILE,
        "ui_meta": {META_KEY: new_snapshot},
        "ui_meta_expected_revisions": {META_KEY: revision},
    })
    applied = result.get("applied") if isinstance(result.get("applied"), dict) else {}
    if applied.get("ui_meta") is not True:
        conflicts = applied.get("ui_meta_conflicts")
        raise HTTPException(
            status_code=409,
            detail=("Projektion nicht geschrieben "
                    f"(CAS-Konflikt, Gateway-Revision {revision}): {conflicts or applied}"))

    # Read-back: derselbe Eintrag muss in derselben Quelle stehen — eine gestiegene
    # Revision allein belegt den Post nicht (ein anderer Writer zählt sie ebenfalls hoch).
    after_snapshot, after = _snapshot()
    after_rooms = after_snapshot.get("rooms") if isinstance(after_snapshot.get("rooms"), dict) else {}
    after_room = after_rooms.get(body.key) if isinstance(after_rooms.get(body.key), dict) else {}
    after_log = after_room.get("log") if isinstance(after_room.get("log"), list) else []
    entry_found = any(isinstance(e, dict) and e.get("id") == entry["id"] for e in after_log)
    if not entry_found:
        raise HTTPException(
            status_code=502,
            detail=("Write nicht belegt: Revision steht auf "
                    f"{after}, der Eintrag {entry['id']} fehlt aber im Verlauf. "
                    "Der Wirt hat den Schreibvorgang offenbar verworfen."))
    written = applied.get("ui_meta_revisions") if isinstance(applied.get("ui_meta_revisions"), dict) else {}
    run_id: str | None = None
    turn_note: str | None = None
    if body.turn:
        with _runs_lock:
            busy = [k for k, v in _runs.items()
                    if v.get("key") == body.key and v.get("state") == "running"]
        if busy:
            # Der Beitrag steht schon im Verlauf; die Runde wird nur nicht doppelt gestartet.
            turn_note = f"Runde {busy[0]} läuft für diesen Raum schon — kein zweiter Start."
        else:
            run_id = f"run-{secrets.token_hex(6)}"
            fresh_snapshot, _fresh_revision = _snapshot()
            fresh_rooms = fresh_snapshot.get("rooms") if isinstance(fresh_snapshot.get("rooms"), dict) else {}
            fresh_room = fresh_rooms.get(body.key) if isinstance(fresh_rooms.get(body.key), dict) else {}
            fresh_log = fresh_room.get("log") if isinstance(fresh_room.get("log"), list) else []
            thread = _thread_of(entry)
            thread_entries = [e for e in fresh_log if _thread_of(e) == thread]
            entries = [_entry_view(e) for e in (thread_entries or fresh_log)]
            members = _members(fresh_room)
            addressed = _addressed_members(members, text)
            with _runs_lock:
                _runs[run_id] = {
                    "state": "running",
                    "key": body.key,
                    "thread": thread,
                    "started_at": int(time.time() * 1000),
                    "members": len(members),
                    "addressed": [str(m.get("handle") or m.get("name")) for m in addressed],
                    "answers": 0,
                }
            worker = threading.Thread(
                target=_run_round,
                args=(run_id, body.key, str(fresh_room.get("name") or body.key),
                      members, entries, thread, addressed),
                name=f"bot-rooms-{run_id}",
                daemon=True,
            )
            worker.start()
    return {
        "ok": True,
        "entry": _entry_view(entry),
        "revision_before": revision,
        "revision_after": after,
        "revision_written": written.get(META_KEY),
        "entry_found": entry_found,
        "read_back_ok": bool(entry_found and after > revision),
        "run_id": run_id,
        "turn_note": turn_note,
    }


@router.get("/turn/status")
def turn_status(run_id: str | None = None) -> dict[str, Any]:
    """Zustand der laufenden Mitglieder-Runde(n) — ohne Geheimnisse."""
    with _runs_lock:
        if run_id:
            return {"run": dict(_runs.get(run_id) or {"state": "unbekannt", "run_id": run_id})}
        return {"runs": {k: dict(v) for k, v in list(_runs.items())[-5:]}}


@router.get("/probe")
def probe(with_ui_meta: bool = False) -> dict[str, Any]:
    """Diagnose: Prozess, Wurzel, Methoden-Registry — keine Geheimnisse.

    Mit ``with_ui_meta=true`` wird zusätzlich ``profiles.configure`` mit einem
    **leeren** ``ui_meta``-Objekt gerufen; das kann keinen Key verändern.
    """
    import os

    info: dict[str, Any] = {"pid": os.getpid(), "cwd": os.getcwd()}
    try:
        info["root"] = str(_home())
    except Exception as exc:
        info["root"] = f"Fehler: {exc}"
    try:
        methods = _methods()
        info["methods_count"] = len(methods)
        info["has_configure"] = "profiles.configure" in methods
        info["has_prompt_submit"] = "prompt.submit" in methods
        info["has_session_create"] = "session.create" in methods
        info["has_session_events"] = "session.events.since" in methods
        info["sample_methods"] = sorted(methods)[:8]
    except HTTPException as exc:
        info["methods"] = f"HTTPException: {exc.detail}"
        return info
    except Exception as exc:
        info["methods"] = f"{type(exc).__name__}: {exc}"
        return info
    try:
        info["configure_without_ui_meta"] = _call("profiles.configure", {"name": GATEWAY_PROFILE})
    except Exception as exc:
        info["configure_without_ui_meta"] = f"{type(exc).__name__}: {exc}"
    if with_ui_meta:
        try:
            info["configure_empty_ui_meta"] = _call(
                "profiles.configure", {"name": GATEWAY_PROFILE, "ui_meta": {}})
        except Exception as exc:
            info["configure_empty_ui_meta"] = f"{type(exc).__name__}: {exc}"
    return info
