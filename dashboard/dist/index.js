/**
 * hermes-bot-rooms — Web-Dashboard-Oberflaeche (Stufe 1).
 *
 * Zeigt den **Stand der Desktop-Umgebung**: dieselben Gruppenraeume, die die
 * Desktop-App ueber profile.yaml (ui_meta["hermes-bots-groups"]) spiegelt.
 * Kein eigener Raum-Speicher, keine parallele Struktur.
 *
 * Backend-Namespace /api/plugins/hermes-bot-rooms/:
 *   GET  /state        Raeume, Mitglieder, letzte Nachricht, Revisionen
 *   GET  /room?key=    ein Raum mit vollem Verlauf
 *   POST /send         Nachricht an den Verlauf anhaengen (confirm)
 *
 * Reines IIFE-Bundle ohne Build: window.__HERMES_PLUGIN_SDK__ liefert React,
 * Hooks, Komponenten und fetchJSON.
 *
 * Nach der Review-Runde (17-10-2026): Doppelpost-Sperre ueber ein Ref (nicht
 * nur Render-State), Textlimit wie das Backend, nach dem Senden wird auch die
 * Liste neu geladen, und der offene Raum wird im Sekundentakt nachgeladen,
 * damit App-Antworten erscheinen. Die zwei Zaehler sind beschriftet:
 * "Raum-Rev" ist die Revision des Raums, "Meta-Rev" die CAS-Revision der
 * gesamten Gruppen-Projektion.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) {
    console.error("[hermes-bot-rooms] Plugin-SDK nicht gefunden");
    return;
  }

  var React = SDK.React;
  var h = React.createElement;
  var C = SDK.components || {};
  var hooks = SDK.hooks || {};

  var PLUGIN_ID = "hermes-bot-rooms";
  var BASE = "/api/plugins/" + PLUGIN_ID;
  var POLL_MS = 5000;
  var TEXT_LIMIT = 8000;

  function fetchJSON(path, options) {
    return SDK.fetchJSON(path, options);
  }

  function postJSON(path, body) {
    // confirm zentral setzen — nicht an den Aufrufstellen (dort schon vergessen).
    var payload = {};
    Object.keys(body || {}).forEach(function (k) { payload[k] = body[k]; });
    payload.confirm = true;
    return fetchJSON(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  }

  function text(value) {
    return value === null || value === undefined ? "" : String(value);
  }

  function errorText(err) {
    if (!err) return "unbekannter Fehler";
    return err.message ? err.message : String(err);
  }

  function stamp(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }) +
      " " + d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  }

  function pretty(value) {
    return text(value).replace(/\\n/g, "\n");
  }

  // ── Bausteine ───────────────────────────────────────────────────────────────

  function Badge(props) {
    if (!C.Badge) return h("span", { className: "mr-1 " + (props.className || "") }, props.children);
    return h(C.Badge, props, props.children);
  }

  function MemberList(props) {
    var members = props.members || [];
    if (!members.length) {
      return h("span", { className: "text-xs text-muted-foreground" }, "keine Mitglieder");
    }
    return h("div", { className: "flex flex-wrap gap-1" },
      members.map(function (m, i) {
        var label = "@" + text(m.handle || m.name);
        var title = m.connection_label || m.connection_id || "";
        return h(Badge, { key: i, className: "text-xs", title: title }, label);
      })
    );
  }

  function MessageRow(props) {
    var entry = props.entry || {};
    var mine = text(entry.sender_kind) === "user";
    return h("div", { className: "flex flex-col gap-1 border-b border-border/40 px-3 py-2" },
      h("div", { className: "flex items-center gap-2 text-xs text-muted-foreground" },
        h("span", { className: mine ? "font-medium text-foreground" : "font-medium" },
          mine ? (text(entry.sender_name) || "Du") : "@" + text(entry.sender_name)),
        entry.sender_source ? h("span", null, text(entry.sender_source)) : null,
        entry.thread ? h("span", { className: "rounded bg-muted px-1" }, text(entry.thread)) : null,
        h("span", { className: "ml-auto tabular-nums" }, stamp(entry.at_iso))
      ),
      h("div", { className: "whitespace-pre-wrap text-sm" }, pretty(entry.text))
    );
  }

  function Composer(props) {
    var valueState = hooks.useState ? hooks.useState("") : [null, null];
    var value = valueState[0];
    var setValue = valueState[1];
    var busyState = hooks.useState ? hooks.useState(false) : [null, null];
    var busy = busyState[0];
    var setBusy = busyState[1];
    var noteState = hooks.useState ? hooks.useState("") : [null, null];
    var note = noteState[0];
    var setNote = noteState[1];
    // Doppelpost-Sperre: ein Ref greift auch vor dem naechsten Render.
    var inFlight = hooks.useRef ? hooks.useRef(false) : { current: false };

    function submit() {
      var body = text(value).trim();
      if (!body || inFlight.current) return;
      if (body.length > TEXT_LIMIT) {
        setNote("zu lang: " + body.length + " von max " + TEXT_LIMIT + " Zeichen");
        return;
      }
      inFlight.current = true;
      setBusy(true);
      setNote("");
      postJSON(BASE + "/send", { key: props.roomKey, text: body })
        .then(function (res) {
          var confirmed = !!(res && res.read_back_ok === true);
          if (confirmed) {
            setValue("");
            setNote("geschrieben und zurueckgelesen — Meta-Rev " + text(res.revision_after));
          } else {
            // Ohne belegten Eintrag darf der Entwurf nicht verschwinden.
            setNote("nicht belegt (kein read_back_ok) — Entwurf bleibt stehen");
          }
          if (props.onSent) props.onSent();
        })
        .catch(function (err) { setNote("Fehler: " + errorText(err)); })
        .then(function () {
          inFlight.current = false;
          setBusy(false);
        });
    }

    return h("div", { className: "flex flex-col gap-2 border-t border-border p-3" },
      C.Input
        ? h(C.Input, {
            value: value,
            maxLength: TEXT_LIMIT,
            placeholder: "Nachricht an den Raum-Verlauf …",
            onChange: function (e) { setValue(e.target.value); },
            onKeyDown: function (e) {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
            }
          })
        : h("input", {
            className: "w-full rounded border border-border bg-background px-2 py-1 text-sm",
            value: value,
            maxLength: TEXT_LIMIT,
            onChange: function (e) { setValue(e.target.value); }
          }),
      h("div", { className: "flex items-center gap-2" },
        C.Button
          ? h(C.Button, { onClick: submit, disabled: busy, size: "sm" }, "Senden")
          : h("button", { onClick: submit, disabled: busy }, "Senden"),
        h("span", { className: "text-xs text-muted-foreground" },
          text(value).length + " / " + TEXT_LIMIT),
        note ? h("span", { className: "text-xs text-muted-foreground" }, note) : null
      ),
      h("div", { className: "text-xs text-muted-foreground" },
        "Der Post geht in den Raum-Verlauf der Projektion und startet die Mitglieder-Runden — " +
        "Antworten erscheinen hier im Sekundentakt.")
    );
  }

  function RoomView(props) {
    var detailState = hooks.useState ? hooks.useState(null) : [null, null];
    var detail = detailState[0];
    var setDetail = detailState[1];
    var logState = hooks.useState ? hooks.useState([]) : [null, null];
    var log = logState[0];
    var setLog = logState[1];
    var scrollRef = hooks.useRef ? hooks.useRef(null) : { current: null };
    // Generationsmarke: eine ueberholte Antwort darf die neuere nicht ueberschreiben.
    var generation = hooks.useRef ? hooks.useRef(0) : { current: 0 };
    var key = props.roomKey;

    function reload() {
      generation.current += 1;
      var mine = generation.current;
      fetchJSON(BASE + "/room?key=" + encodeURIComponent(key))
        .then(function (res) {
          if (mine !== generation.current) return;
          setDetail(res);
          setLog((res && res.log) || []);
        })
        .catch(function (err) {
          if (mine !== generation.current) return;
          // Nur der leere Erstaufruf darf in die Fehleransicht laufen; ein
          // Poll-Fehler soll die bestehende Ansicht und den Entwurf behalten.
          setDetail(function (prev) { return prev || { error: errorText(err) }; });
        });
    }

    hooks.useEffect(function () { reload(); }, [key]);

    hooks.useEffect(function () {
      var timer = setInterval(reload, POLL_MS);
      return function () { clearInterval(timer); };
    }, [key]);

    hooks.useEffect(function () {
      var node = scrollRef.current;
      if (node && node.scrollHeight) node.scrollTop = node.scrollHeight;
    }, [log.length]);

    if (detail && detail.error) {
      return h("div", { className: "p-6 text-sm" },
        h("div", { className: "font-medium" }, "Raum nicht lesbar"),
        h("div", { className: "text-muted-foreground" }, text(detail.error)));
    }

    var room = (detail && detail.room) || {};
    return h("div", { className: "flex h-full min-h-0 flex-col" },
      h("div", { className: "flex flex-col gap-2 border-b border-border p-3" },
        h("div", { className: "flex items-center gap-3" },
          h("span", { className: "text-base font-medium" }, text(room.name)),
          h("span", { className: "text-xs text-muted-foreground" }, text(room.key)),
          h("span", { className: "ml-auto text-xs text-muted-foreground" },
            "Raum-Rev " + text(room.revision)),
          h("span", { className: "text-xs text-muted-foreground" },
            "Meta-Rev " + text(detail && detail.revision))
        ),
        h(MemberList, { members: room.members }),
        room.omitted
          ? h("div", { className: "text-xs text-muted-foreground" },
              text(room.omitted) + " aeltere Eintraege ausgelassen")
          : null
      ),
      h("div", { ref: scrollRef, className: "min-h-0 flex-1 overflow-y-auto" },
        log.length
          ? log.map(function (e, i) { return h(MessageRow, { key: text(e.id) || i, entry: e }); })
          : h("div", { className: "p-4 text-sm text-muted-foreground" }, "Kein Verlauf in diesem Raum.")
      ),
      h(Composer, {
        roomKey: key,
        onSent: function () {
          reload();
          if (props.onRefreshList) props.onRefreshList();
        }
      })
    );
  }

  function BotRoomsPage() {
    var snapState = hooks.useState ? hooks.useState(null) : [null, null];
    var snapshot = snapState[0];
    var setSnapshot = snapState[1];
    var errState = hooks.useState ? hooks.useState("") : [null, null];
    var error = errState[0];
    var setError = errState[1];
    var pickState = hooks.useState ? hooks.useState(null) : [null, null];
    var picked = pickState[0];
    var setPicked = pickState[1];
    var tickState = hooks.useState ? hooks.useState(0) : [0, null];
    var tick = tickState[0];
    var bump = tickState[1];

    hooks.useEffect(function () {
      fetchJSON(BASE + "/state")
        .then(function (res) { setSnapshot(res); setError(""); })
        .catch(function (err) { setError(errorText(err)); });
    }, [tick]);

    // Ein verschwundener Raum darf nicht als Auswahl stehen bleiben.
    hooks.useEffect(function () {
      if (!picked || !snapshot) return;
      var roomsNow = snapshot.rooms || [];
      for (var i = 0; i < roomsNow.length; i += 1) {
        if (roomsNow[i].key === picked) return;
      }
      setPicked(null);
    }, [snapshot]);

    if (error) {
      return h("div", { className: "p-6 text-sm" },
        h("div", { className: "mb-2 font-medium" }, "Bot-Raeume nicht erreichbar"),
        h("div", { className: "text-muted-foreground" }, error),
        C.Button ? h(C.Button, { size: "sm", onClick: function () { bump(tick + 1); } }, "Erneut") : null,
        h("div", { className: "mt-3 text-xs text-muted-foreground" },
          "Fehlt das Backend, fehlt meist die Plugin-id in plugins.enabled der Root-Config.")
      );
    }
    if (!snapshot) {
      return h("div", { className: "p-6 text-sm text-muted-foreground" }, "Lade Bot-Raeume …");
    }

    var rooms = snapshot.rooms || [];
    // Ein verschwundener Key darf nicht als Auswahl stehen bleiben (sonst 404 statt Liste).
    var pickedLive = null;
    for (var i = 0; i < rooms.length; i += 1) {
      if (rooms[i].key === picked) pickedLive = picked;
    }
    var active = pickedLive || (rooms.length ? rooms[0].key : null);

    return h("div", { className: "flex h-full min-h-0" },
      h("div", { className: "flex w-72 shrink-0 flex-col gap-2 overflow-y-auto border-r border-border p-3" },
        h("div", { className: "flex items-center justify-between" },
          h("span", { className: "text-sm font-medium" }, "Raeume"),
          h("span", { className: "text-xs text-muted-foreground" }, "Meta-Rev " + text(snapshot.revision))
        ),
        h("div", { className: "break-words text-xs text-muted-foreground" }, text(snapshot.source)),
        rooms.length
          ? rooms.map(function (r) {
              var on = r.key === active;
              return h("button", {
                key: r.key,
                className: "rounded-md border border-border px-2 py-2 text-left text-sm " +
                  (on ? "bg-muted" : "hover:bg-muted/50"),
                onClick: function () { setPicked(r.key); }
              },
                h("div", { className: "flex items-center gap-2" },
                  h("span", { className: "font-medium" }, text(r.name)),
                  h("span", { className: "ml-auto text-xs text-muted-foreground" },
                    text(r.entry_count) + " Eintraege")
                ),
                h("div", { className: "text-xs text-muted-foreground" },
                  (r.members || []).map(function (m) { return "@" + text(m.handle || m.name); }).join(", ")),
                r.last && r.last.text
                  ? h("div", { className: "truncate text-xs text-muted-foreground" },
                      (text(r.last.sender_kind) === "user"
                        ? "Du: "
                        : "@" + text(r.last.sender_name) + ": ") + text(r.last.text))
                  : null
              );
            })
          : h("div", { className: "text-xs text-muted-foreground" },
              "Keine Gruppe in der Projektion. Gruppen legt die Desktop-App an; hier erscheinen sie, " +
              "sobald sie ueber die Metadaten des Gateways gespiegelt sind.")
      ),
      h("div", { className: "min-h-0 flex-1" },
        active
          ? h(RoomView, { roomKey: active, onRefreshList: function () { bump(tick + 1); } })
          : h("div", { className: "p-6 text-sm text-muted-foreground" }, "Raum links waehlen."))
    );
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register(PLUGIN_ID, BotRoomsPage);
  } else {
    console.error("[hermes-bot-rooms] Plugin-Registry nicht gefunden");
  }
})();
