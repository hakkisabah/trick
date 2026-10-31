"""
title: Trick
author: hakkisabah
author_url: https://github.com/hakkisabah/trick
funding_url: https://github.com/hakkisabah/trick
version: 0.9.1
required_open_webui_version: 0.5.0
description: Find a passage in a long conversation; every hit links to its turn.
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
import urllib.parse
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

# The results block, appended to the message the button was pressed on.
#
# Open WebUI renders an HTMLResponse embed in an iframe whose sandbox is
# "allow-scripts allow-popups allow-downloads" with
# ``iframeSandboxAllowSameOrigin`` defaulting to false (read out of the built
# frontend of 0.12.4). Such a frame has an opaque origin, so it cannot read the
# chat document, cannot be granted top-level navigation, and the page accepts
# only three message types from it ("input:prompt", "input:prompt:submit",
# "action:submit" - Chat.svelte, onMessageHandler). There is therefore no way
# for a card inside that frame to scroll the chat: any design that tries is
# dead code that looks alive.
#
# What does work is the mechanism Open WebUI uses for its own search hits: a
# link to "?message=<id>" on the chat route, which Chat.svelte turns into
# ``messagesRef.scrollToMessage(target)``. Markdown written into a message is
# rendered in the page itself, not in a frame, so those links are ordinary
# anchors and need no privileges. The action therefore returns the message
# back with the results appended, and every row is a real link.
MARKER = "<!-- trick:results -->"
# The block is rebuilt on every press: everything from MARKER to the end of the
# message is replaced, so repeated searches do not stack up. The original text
# above the marker is never touched.


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
            default="never",
            description=(
                "Whether to list catalogue sources at all. 'never' (default) keeps this "
                "button a find-in-conversation tool; 'auto' lists them only when the "
                "conversation had no match; 'always' lists them beside the hits. "
                "Catalogue search stays available in the CLI and the MCP server."
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

        # This button is a find for the conversation it sits in. The catalogues
        # are a different product surface (CLI, MCP) and answering a chat
        # question with eight R packages read as noise, so they are off by
        # default and the reader opts in: 'auto' shows them only when the
        # conversation had nothing, 'always' shows them beside every hit.
        mode = str(self.valves.SOURCES or "never").strip().lower()
        show_rows = bool(rows) and (mode == "always" or (mode == "auto" and not chat))
        block = self._results_html(
            query,
            chat,
            rows if show_rows else [],
            hidden=0 if show_rows else len(rows),
            sources_mode=mode,
            # 'weak' is a property of the conversation ranking, so it lives in
            # chat_report; reading 'report' silently hid the warning.
            weak=bool((result.get("chat_report") or result.get("report") or {}).get("weak")) and bool(chat),
            degraded=degraded,
            transcript=bool(transcript),
            origin=origin,
            dropped=dropped,
        )

        attached = self._attach(body, block)
        if attached:
            return attached
        # No message id (an API call, not a browser): there is nothing to write
        # the block into, so fall back to the embed.
        return self._card(f"Trick: {query}", [(block, "")], degraded)

    # -- the results block --------------------------------------------------
    def _results_html(
        self,
        query: str,
        chat: list,
        catalogue: list,
        *,
        hidden: int = 0,
        sources_mode: str = "never",
        weak: bool = False,
        degraded: list | None = None,
        transcript: bool = False,
        origin: str = "",
        dropped: int = 0,
    ) -> str:
        """The block written into the message. Raw HTML, so the rows are links.

        Every row links to ``?message=<id>`` on this chat route, which is how
        Open WebUI reveals a search hit itself (Chat.svelte reads the ``message``
        query parameter and calls ``scrollToMessage``). A message is rendered in
        the page, not in a sandboxed frame, so the anchor needs no permission and
        no script. ``<details>`` makes the block collapsible without any JS.
        """
        parts: list[str] = ["<details open>"]
        count = len(chat)
        limit = max(1, int(self.valves.CHAT_RESULTS))
        shown = min(count, limit)
        head = f"<b>Trick</b> · {html.escape(query)}"
        if count:
            head += f" · {count} passage{'' if count == 1 else 's'} match"
            if count > shown:
                # Saying "8 matches" above three rows reads as a bug; say both.
                head += f", showing the closest {shown}"
        parts.append(f"<summary>{head}</summary>")

        if origin == "unavailable":
            parts.append(
                "<p>This conversation could not be read, so only the catalogues were "
                "searched. Try again, or use <code>trick tui --transcript</code> with an "
                "exported chat.</p>"
            )
        elif transcript and not chat:
            parts.append(
                "<p>No passage in this conversation contains the words that distinguish "
                f"<b>{html.escape(query)}</b>.</p>"
            )
        if weak:
            parts.append(
                "<p><i>Weak match: no passage carries the distinctive words of this "
                "question, so these are the closest turns. Check the matched terms "
                "before trusting a row.</i></p>"
            )
        if chat:
            parts.append("<ol>")
            for row in chat[:limit]:
                parts.append(self._row_html(row))
            parts.append("</ol>")
            if dropped:
                parts.append(f"<p><small>The oldest {dropped} turns were not searched.</small></p>")
        if catalogue:
            parts.append(self._catalogue_html(catalogue))
        elif hidden:
            parts.append(
                f"<p><small>{hidden} catalogue source{'s' if hidden != 1 else ''} matched "
                f"outside this conversation and {'are' if hidden != 1 else 'is'} hidden "
                f"(SOURCES={html.escape(sources_mode)}).</small></p>"
            )
        if not chat and not catalogue and origin != "unavailable" and not transcript:
            parts.append(f"<p>Nothing matched <b>{html.escape(query)}</b>.</p>")
        note = "; ".join(str(item) for item in (degraded or []) if item)
        if note:
            parts.append(f"<p><small>degraded: {html.escape(note)}</small></p>")
        parts.append("</details>")
        return "".join(parts)

    def _row_html(self, row: dict) -> str:
        role = "you" if str(row.get("role")) == "user" else str(row.get("role") or "assistant")
        label = f"turn {int(row.get('turn') or 0)} · {role}"
        mid = str(row.get("message_id") or "")
        head = html.escape(label)
        if mid:
            quoted = urllib.parse.quote(mid, safe="")
            head = f"<a href='?message={quoted}'>{html.escape(label)}</a>"
        terms = ""
        matched = [str(term) for term in (row.get("matched") or []) if term]
        if matched:
            terms = f" · matched: {html.escape(', '.join(matched))}"
            try:
                # Coverage is the share of the question's distinctive weight this
                # passage carries; below the strong threshold the row is the
                # closest turn, not an answer. Say so on the row itself.
                if 0.0 < float(row.get("coverage") or 0.0) < 0.5:
                    terms += " (partial)"
            except (TypeError, ValueError):
                pass
        if self.valves.SHOW_SCORES:
            try:
                terms += f" · {float(row.get('final_score') or 0.0):.2f}"
            except (TypeError, ValueError):
                pass
        text = " ".join(str(row.get("text") or "").split())
        if len(text) > 240:
            text = text[:237].rstrip() + "..."
        return f"<li>{head}{terms}<br>{html.escape(text)}</li>"

    @staticmethod
    def _catalogue_html(rows: list) -> str:
        """Catalogue hits, kept plain: they are a different corpus, not a passage."""
        items = []
        for row in rows[:26]:
            key = str(row.get("key") or row.get("id") or "")
            source = str(row.get("source") or "")
            title = str(row.get("title") or "")
            label = f"{source}/{key}" if source else key
            try:
                score = f" · {float(row.get('final_score') or 0.0):.2f}"
            except (TypeError, ValueError):
                score = ""
            summary = " ".join(str(row.get("summary") or "").split())
            if len(summary) > 160:
                summary = summary[:157].rstrip() + "..."
            tail = f"<br>{html.escape(summary)}" if summary else ""
            items.append(f"<li>{html.escape(label)}{score} — {html.escape(title)}{tail}</li>")
        return "<p><small>Catalogue sources:</small></p><ul>" + "".join(items) + "</ul>"

    @staticmethod
    def _attach(body, block: str) -> dict:
        """Return the message back with the block appended, or {} when it cannot.

        Open WebUI applies ``res.messages`` to the chat and saves it, which is
        the documented way for an action to update the message it ran on. Only
        the message the button was pressed on is touched, and only below the
        marker: a second search replaces the previous block instead of stacking.
        """
        mid = str((body or {}).get("id") or "").strip()
        if not mid:
            return {}
        current = ""
        for item in (body.get("messages") or []):
            if isinstance(item, dict) and str(item.get("id")) == mid:
                current = str(item.get("content") or "")
                break
        base = current.split(MARKER)[0].rstrip()
        content = f"{base}\n\n{MARKER}\n{block}" if base else f"{MARKER}\n{block}"
        return {"messages": [{"id": mid, "content": content}]}

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
    def _card(self, title: str, blocks, degraded: list | None = None) -> "_HTMLResponse":
        """Fallback embed, used only when the caller sends no message id.

        A browser call writes the results into the message instead (see
        ``_results_html``): that is where the links work. An API call has no
        message to write into, so it gets a framed copy. No script, because
        there is nothing in this frame to click.
        """
        body = "".join(block for block, _ in blocks)
        note = "; ".join(str(item) for item in (degraded or []) if item)
        if note:
            body += f"<p>degraded: {html.escape(note)}</p>"
        return _HTMLResponse(
            content=(
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<title>{html.escape(title)}</title></head><body>{body}</body></html>"
            ),
            headers={"Content-Disposition": "inline"},
        )

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
            # The id travels with the turn so a result can point back at it; the
            # service ignores it when ranking. An empty id is left out entirely,
            # so a client that sends none looks exactly like it did before.
            turn = {"role": role, "content": content}
            mid = str(item.get("id") or "")
            if mid:
                turn["id"] = mid
            cleaned.append(turn)
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
