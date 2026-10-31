"""
title: Trick
author: hakkisabah
author_url: https://github.com/hakkisabah/trick
funding_url: https://github.com/hakkisabah/trick
version: 0.7.0
required_open_webui_version: 0.5.0
description: Find a passage in a long conversation, and scored sources beside it.
"""

# Trick as an Open WebUI Action.
#
# An Action is a button in the message toolbar, which is the shape this tool
# needs: press it, type what you are looking for in the dialog that opens,
# press Enter, get the passage back. It is a find, not a chat model, so it must
# not appear in the model selector and must not answer a turn.
#
# Two facts from the Open WebUI 0.12.4 source decide the code below:
#
# * ``action`` is called as ``action(**params)`` where ``params`` always holds
#   ``body`` and only those reserved arguments whose exact names appear in the
#   signature. Every other call shape is version-dependent, so nothing here may
#   require an argument beyond ``body``.
# * ``__event_call__({"type": "input", ...})`` opens the input dialog, and it
#   returns a dict with an ``error`` key when the browser cannot answer (an API
#   call, a closed tab). That path must not raise.
#
# No ranking logic lives here. This file is a client for ``trick serve``'s
# loopback bridge, because a second copy of the ranking code inside a container
# would drift from the terminal's answer within a week.

from __future__ import annotations

import asyncio
import html
import json
import urllib.error
import urllib.request

from pydantic import BaseModel, Field

try:  # Open WebUI ships fastapi; the tests must import this file without it.
    from fastapi.responses import HTMLResponse as _HTMLResponse
except Exception:  # pragma: no cover - exercised only outside Open WebUI

    class _HTMLResponse:  # type: ignore[no-redef]
        def __init__(self, content: str = "", headers: dict | None = None) -> None:
            self.body = content.encode("utf-8")
            self.headers = headers or {}
            self.status_code = 200


SOURCE_LABELS = {
    "cran": "CRAN",
    "bioconductor": "Bioconductor",
    "arxiv": "arXiv",
    "openalex": "OpenAlex",
    "crossref": "Crossref",
    "local": "local",
}

DEGRADED_TEXT = {
    "S1": "score matrix not built yet",
    "S2": "score matrix corrupt, emergency matrix in use",
    "S3": "not enough memory for the index, text-only mode",
    "S4": "a live source did not answer",
    "S5": "laya did not answer, results are retrieval-only",
    "S6": "storage problem",
    "S7": "another writer holds the lock",
    "S8": "input rejected",
    "S9": "configuration problem",
}

STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; padding: 10px 14px; }
details#trick-card > summary { display: flex; align-items: baseline; gap: 8px; margin: 0 0 6px; cursor: pointer; list-style: none; }
details#trick-card > summary::-webkit-details-marker { display: none; }
details#trick-card > summary .title { flex: 1; font-size: 12px; opacity: .7; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
details#trick-card > summary .x { font-size: 15px; line-height: 1; padding: 2px 6px; opacity: .6; }
details#trick-card > summary:hover .x { opacity: 1; }
details#trick-card:not([open]) > summary .x { transform: rotate(45deg); }
h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; opacity: .65; margin: 14px 0 6px; }
h3:first-of-type { margin-top: 0; }
ol, ul { margin: 0; padding-left: 18px; }
li { margin: 0 0 10px; }
.turn { font-variant-numeric: tabular-nums; opacity: .7; font-size: 12px; }
.quote { border-left: 3px solid rgba(127,127,127,.4); padding-left: 8px; margin-top: 2px; }
.meta { font-size: 12px; opacity: .7; }
a { color: inherit; }
.note { font-size: 12px; opacity: .75; margin-top: 10px; }
.none { opacity: .7; }
"""

# Closing the card. Open WebUI renders an HTMLResponse embed in a sandboxed
# iframe, so this document cannot remove its own frame from the parent page.
# Two mechanisms are used because only one of them is guaranteed: the card is a
# native ``<details>`` element, so the summary line collapses it with no script
# at all, and the script only adds Escape and the parent height report. A panel
# that could only be closed by script stayed open, which is what a reader
# reported.
SCRIPT = """
<script>
(function () {
  var card = document.getElementById('trick-card');
  if (!card) { return; }
  function report() {
    try {
      parent.postMessage({ type: 'iframe:height', height: document.body.scrollHeight }, '*');
    } catch (error) { /* a host that does not listen is not an error */ }
  }
  card.addEventListener('toggle', report);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') { card.open = false; }
  });
  window.addEventListener('load', report);
  report();
})();
</script>
"""


class Action:
    class Valves(BaseModel):
        TRICK_URL: str = Field(
            default="http://host.docker.internal:8765/v1/search",
            description="Trick bridge endpoint (trick serve --http-port).",
        )
        TRICK_TOKEN: str = Field(
            default="",
            description="Bearer token from $TRICK_HOME/bridge.token (0600 file).",
        )
        LIMIT: int = Field(default=8, description="How many catalogue sources to show (1-26).")
        LIVE: bool = Field(default=True, description="Query the live source APIs as well.")
        SOURCES: str = Field(
            default="auto",
            description=(
                "When to list catalogue sources beside the conversation hits: "
                "'auto' (only when the conversation had no match), 'always', or 'never'."
            ),
        )
        SEARCH_CHAT: bool = Field(
            default=True,
            description="Also search inside the conversation itself, not only the catalogues.",
        )
        CHAT_TURNS: int = Field(
            default=200,
            description="How many of the most recent turns to search (the oldest are dropped).",
        )
        CHAT_MAX_CHARS: int = Field(
            default=150000,
            description="Character budget for the transcript sent to the service.",
        )
        CHAT_RESULTS: int = Field(default=3, description="How many conversation passages to show.")
        SHOW_SCORES: bool = Field(default=True, description="Show the blended score per row.")
        TIMEOUT_S: int = Field(default=25, description="Bridge timeout in seconds.")
        priority: int = Field(default=-5, description="Button order; lower shows first.")

    def __init__(self) -> None:
        self.valves = self.Valves()
        # The icon is a URL, not base64: icons are re-sent in every /api/models
        # response, so an inline image would cost every page load.
        self.icon = "🔎"

    # -- transport ---------------------------------------------------------
    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.valves.TRICK_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.valves.TRICK_TOKEN}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=float(self.valves.TIMEOUT_S)) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                error = json.loads(raw).get("error", {})
            except ValueError:
                error = {"s_code": "", "message": raw[:300]}
            return {"ok": False, "status": exc.code, "error": error}
        except urllib.error.URLError as exc:
            return {
                "ok": False,
                "status": 0,
                "error": {"s_code": "", "message": f"cannot reach {self.valves.TRICK_URL}: {exc.reason}"},
            }
        except (TimeoutError, OSError) as exc:
            return {"ok": False, "status": 0, "error": {"s_code": "", "message": f"transport error: {exc}"}}

    # -- the Open WebUI entry point ---------------------------------------
    async def action(
        self,
        body: dict,
        __event_emitter__=None,
        __event_call__=None,
        __user__=None,
    ):
        # body carries the conversation, the chat id and the id of the message
        # the button was pressed on. Nothing else is guaranteed across versions.
        messages, origin = await self._conversation(body)

        query, ask_failed = await self._ask(__event_call__)
        if query is None:
            # A cancelled dialog means "never mind": search nothing. A dialog
            # that could not open at all (no browser, API call) falls back to
            # the last user message so the button still does something useful.
            if not ask_failed:
                return self._card("Trick", [("<p class='none'>No question was entered.</p>", "")])
            query = self._last_user_message(messages)
        if not query:
            return self._card("Trick", [("<p class='none'>No question was entered.</p>", "")])

        transcript, dropped = self._transcript(messages)
        payload = {
            "query": query[:2000],
            "limit": max(1, min(int(self.valves.LIMIT), 26)),
            "live": bool(self.valves.LIVE),
        }
        if transcript:
            payload["messages"] = transcript
            payload["scope"] = "both"

        answer = await asyncio.to_thread(self._post, payload)
        if not answer.get("ok"):
            return self._failure_card(answer)

        result = answer.get("result") or {}
        rows = result.get("rows") or []
        chat = result.get("chat") or []
        degraded = result.get("degraded_text") or [
            DEGRADED_TEXT.get(str(code), str(code)) for code in (result.get("degraded") or [])
        ]

        blocks: list[tuple[str, str]] = []
        if origin == "unavailable":
            blocks.append((self._unread_chat_block(), ""))
        elif transcript and not chat:
            blocks.append((self._no_passage_block(query), ""))
        if chat:
            blocks.append((self._chat_block(chat, dropped), ""))

        # A find in a long conversation must not bury the answer under eight
        # near-random catalogue rows: when the conversation answered, the
        # sources are suppressed unless the reader asked for them. When it did
        # not answer, they are the useful next step and are shown.
        mode = str(self.valves.SOURCES or "auto").strip().lower()
        show_rows = bool(rows) and (mode == "always" or (mode == "auto" and not chat))
        if show_rows:
            blocks.append((self._rows_block(rows), ""))
        elif rows and chat:
            blocks.append(
                (
                    f"<p class='note'>{len(rows)} catalogue sources matched too; "
                    "set the SOURCES valve to 'always' to list them.</p>",
                    "",
                )
            )
        if not blocks:
            blocks.append((f"<p class='none'>Nothing matched <b>{html.escape(query)}</b>.</p>", ""))
        return self._card(f"Trick: {query}", blocks, degraded, with_rows=show_rows)

    # -- dialog ------------------------------------------------------------
    async def _ask(self, event_call) -> tuple[str | None, bool]:
        """Ask for the query. Returns (text, whether the dialog could not open)."""
        if event_call is None:
            return None, True
        try:
            response = await event_call(
                {
                    "type": "input",
                    "data": {
                        "title": "Trick: find in this conversation",
                        "message": "Text to look for. Every turn is searched; laya re-orders the top hits.",
                        "placeholder": "e.g. the threshold we settled on",
                    },
                }
            )
        except Exception:
            return None, True
        if isinstance(response, dict):
            # The browser could not answer: no tab, or the caller timed out.
            return None, True
        if response is None:
            return None, False
        text = " ".join(str(response).split())
        return (text or None), False

    # -- rendering ---------------------------------------------------------
    def _card(
        self,
        title: str,
        blocks: list[tuple[str, str]],
        degraded: list | None = None,
        with_rows: bool | None = None,
    ) -> "_HTMLResponse":
        parts = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'>",
            f"<style>{STYLE}</style></head><body>",
            "<details id='trick-card' open>",
            "<summary>",
            f"<span class='title'>{html.escape(title)}</span>",
            "<span class='x' role='button' aria-label='Close' title='Close (Esc)'>&#215;</span>",
            "</summary>",
            "<div id='trick-body'>",
        ]
        for block, _ in blocks:
            parts.append(block)
        if degraded:
            note = "; ".join(html.escape(str(item)) for item in degraded if item)
            if note:
                parts.append(f"<p class='note'>degraded: {note}</p>")
        if with_rows:
            parts.append(
                "<p class='note'>* re-ordered by laya, - retrieval only. "
                "S4/S5 mean a source or the scorer did not answer; the ranking is then "
                "text-only, not wrong-but-hidden.</p>"
            )
        parts.append("</div>")
        parts.append("</details>")
        parts.append(SCRIPT)
        parts.append("</body></html>")
        return _HTMLResponse(content="".join(parts), headers={"Content-Disposition": "inline"})

    @staticmethod
    def _unread_chat_block() -> str:
        return (
            "<p class='none'>This conversation could not be read, so only the "
            "catalogues were searched. Send the message again, or use "
            "<code>trick tui --transcript</code> with the exported chat.</p>"
        )

    @staticmethod
    def _no_passage_block(query: str) -> str:
        return (
            f"<p class='none'>No passage in this conversation contains "
            f"<b>{html.escape(query)}</b>. What follows is outside the chat.</p>"
        )

    def _chat_block(self, chat: list, dropped: int) -> str:
        shown = chat[: max(1, int(self.valves.CHAT_RESULTS))]
        head = f"<h3>Conversation ({len(chat)} match{'es' if len(chat) != 1 else ''}"
        if dropped:
            head += f", oldest {dropped} turns not searched"
        head += ")</h3>"
        items = []
        for row in shown:
            role = "you" if row.get("role") == "user" else str(row.get("role") or "assistant")
            marker = "*" if row.get("scored_by_laya") else "-"
            text = " ".join(str(row.get("text") or "").split())
            if len(text) > 400:
                text = text[:397].rstrip() + "..."
            items.append(
                "<li>"
                f"<span class='turn'>turn {html.escape(str(row.get('turn', '?')))} - {html.escape(role)} {marker}</span>"
                f"<div class='quote'>{html.escape(text)}</div>"
                "</li>"
            )
        return head + "<ol>" + "".join(items) + "</ol>"

    def _rows_block(self, rows: list) -> str:
        items = []
        for row in rows:
            label = SOURCE_LABELS.get(row.get("source", ""), str(row.get("source", "?")))
            title = str(row.get("title") or row.get("key") or "").strip()
            url = str(row.get("url") or "").strip()
            marker = "*" if row.get("score_label") == "laya" else "-"
            suffix = ""
            score = row.get("final_score")
            if self.valves.SHOW_SCORES and isinstance(score, (int, float)):
                suffix = f" - {score:.3f}"
            head = html.escape(title)
            if url.startswith("http"):
                head = f"<a href='{html.escape(url, quote=True)}' target='_blank' rel='noopener noreferrer'>{head}</a>"
            summary = " ".join(str(row.get("summary") or "").split())
            if len(summary) > 240:
                summary = summary[:237].rstrip() + "..."
            items.append(
                "<li>"
                f"{head}"
                f"<div class='meta'>{html.escape(label)} / {html.escape(str(row.get('key', '')))} {marker}{html.escape(suffix)}</div>"
                + (f"<div class='quote'>{html.escape(summary)}</div>" if summary else "")
                + "</li>"
            )
        return f"<h3>Sources ({len(rows)})</h3><ol>" + "".join(items) + "</ol>"

    def _failure_card(self, answer: dict) -> "_HTMLResponse":
        status = answer.get("status")
        error = answer.get("error") or {}
        code = error.get("s_code") or ""
        message = error.get("message") or "unknown error"
        if status == 401:
            hint = (
                "The bridge rejected the token. Paste the contents of "
                "$TRICK_HOME/bridge.token into this function's TRICK_TOKEN valve."
            )
        elif status in (0, None, 502, 503):
            hint = (
                "The Trick service is not answering. Start it with "
                "'.venv/bin/python -m trick serve --http-host 172.17.0.1 --expose-network' "
                "on the host, and check that TRICK_URL points at it."
            )
        else:
            hint = "The service answered with an error."
        block = (
            "<p><b>Trick could not search.</b> "
            f"{html.escape(hint)}</p><p class='meta'>{html.escape(code)} {html.escape(str(message))}</p>"
        )
        return self._card("Trick", [(block, "")])

    # -- conversation scope ------------------------------------------------
    async def _conversation(self, body) -> tuple[list[dict], str]:
        """Find the conversation the button was pressed in.

        Open WebUI 0.12.4 hands the action the *message object* (``id``,
        ``chat_id``, ``session_id``, ``model``) and no ``messages`` array at all,
        so reading ``body["messages"]`` searched the catalogues and silently
        skipped the half of the job the tool exists for. Three shapes are
        accepted, cheapest first:

        ``body``     a ``messages`` list, or a ``chat`` dict, as older or newer
                     releases send it;
        ``storage``  the chat row itself, read in-process from Open WebUI's own
                     models - the path that works on 0.12.4;
        ``unavailable`` a chat id was given but its content could not be read,
                     which the card states instead of pretending there was
                     nothing to find.

        Ownership is not re-checked here: ``verify_chat_ownership`` runs in
        ``main.py`` before the action is dispatched, on the same ``chat_id``.
        """
        if not isinstance(body, dict) or not self.valves.SEARCH_CHAT:
            return [], ""
        inline = body.get("messages")
        if isinstance(inline, list) and inline:
            return [item for item in inline if isinstance(item, dict)], "body"
        if isinstance(body.get("chat"), dict):
            ordered = self._order(body["chat"], str(body.get("id") or ""))
            if ordered:
                return ordered, "body"
        chat_id = str(body.get("chat_id") or "").strip()
        if not chat_id:
            return [], ""
        ordered = await self._from_storage(chat_id, str(body.get("id") or ""))
        return (ordered, "storage") if ordered else ([], "unavailable")

    @classmethod
    def _order(cls, chat: dict, anchor: str = "") -> list[dict]:
        """Rebuild the branch the button was pressed on, oldest turn first.

        A chat is a tree: editing a message forks it. Walking ``parentId`` from
        the pressed message keeps the search on the branch the reader sees,
        instead of mixing in abandoned edits that they no longer have on screen.
        """
        history = chat.get("history") if isinstance(chat.get("history"), dict) else {}
        raw = history.get("messages") or chat.get("messages")
        start = anchor or str(history.get("currentId") or chat.get("currentId") or "")
        if isinstance(raw, dict):
            return cls._walk(raw, start)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        return []

    @classmethod
    def _walk(cls, messages: dict, anchor: str) -> list[dict]:
        node = messages.get(anchor) if anchor else None
        if not isinstance(node, dict):
            # No anchor: the timestamps are the only ordering that cannot lie.
            return sorted(
                (item for item in messages.values() if isinstance(item, dict)),
                key=lambda item: float(item.get("timestamp") or 0),
            )
        chain: list[dict] = []
        seen: set[int] = set()
        while isinstance(node, dict) and id(node) not in seen:
            seen.add(id(node))
            chain.append(node)
            parent = node.get("parentId")
            node = messages.get(parent) if parent else None
        chain.reverse()
        return chain

    async def _from_storage(self, chat_id: str, message_id: str) -> list[dict]:
        """Read the chat from Open WebUI's own models, inside its own process."""
        try:  # imported late: the host test venv has no open_webui package
            from open_webui.models.chats import Chats  # type: ignore
        except Exception:
            return []
        try:
            chat = await Chats.get_chat_by_id(chat_id)
        except Exception:
            return []
        data = getattr(chat, "chat", None)
        if not isinstance(data, dict):
            return []
        return self._order(data, message_id)

    @staticmethod
    def _last_user_message(messages) -> str:
        if not isinstance(messages, list):
            return ""
        for item in reversed(messages):
            if isinstance(item, dict) and str(item.get("role")) == "user":
                content = item.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(part.get("text") or "") for part in content if isinstance(part, dict)
                    )
                if isinstance(content, str) and content.strip():
                    return " ".join(content.split())[:500]
        return ""

    def _transcript(self, messages) -> tuple[list[dict], int]:
        """Window the chat down to what may be sent, and say how much was dropped.

        The service refuses a transcript bigger than its contract instead of
        silently truncating, so the window is chosen here, where the number of
        dropped turns can be shown to the reader. Text is taken as-is: a chat
        message is evidence, so rewriting it would change what is searched.
        """
        if not self.valves.SEARCH_CHAT or not isinstance(messages, list):
            return [], 0
        cleaned: list[dict] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user")
            content = item.get("content")
            if isinstance(content, list):
                parts = [
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("text")
                ]
                content = "\n".join(parts)
            if not isinstance(content, str) or not content.strip():
                continue
            cleaned.append({"role": role, "content": content})
        limit = max(1, int(self.valves.CHAT_TURNS))
        dropped = max(0, len(cleaned) - limit)
        if dropped:
            cleaned = cleaned[-limit:]
        budget = max(1000, int(self.valves.CHAT_MAX_CHARS))
        total = 0
        kept: list[dict] = []
        cut = 0
        for position in range(len(cleaned) - 1, -1, -1):
            message = cleaned[position]
            total += len(message["content"])
            if total > budget and kept:
                cut = position + 1
                break
            kept.append(message)
        dropped += cut
        kept.reverse()
        return kept, dropped
