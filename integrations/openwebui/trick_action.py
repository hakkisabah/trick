"""
title: Trick
author: hakkisabah
author_url: https://github.com/hakkisabah/trick
funding_url: https://github.com/hakkisabah/trick
version: 0.10.0
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
# What does work is the id the chat page puts on every message element:
# ``id="message-<id>"``, the same one its own ``scrollToMessage`` looks up. A
# Markdown link to ``#message-<id>`` is scrolled by the browser itself - no
# script, no frame, no privilege, no router round-trip. Markdown is rendered in
# the page, and raw HTML is not: this build shows a message body's tags as
# their own text. The action therefore returns the message back with the
# results appended, and every row is a real Markdown link.
MARKER = "**Trick** · "
# What the previous release wrote. Existing chats still carry it, so a new
# search cuts the block at whichever of the two appears first - otherwise a
# chat that was searched before would keep a dead copy above the new one.
LEGACY_MARKER = "<!-- trick:results -->"


def _md(text: str) -> str:
    """Escape the characters that would break a Markdown block's structure.

    A passage is arbitrary user text: a stray ``*`` or ``[`` would turn the rest
    of the row into emphasis or a link that points somewhere it should not.
    Quotes are left alone - Markdown needs no entity for them, and escaping them
    is what made the previous block arrive on screen as ``&quot;``.
    """
    collapsed = " ".join(str(text).split())
    out: list[str] = []
    for char in collapsed:
        if char in "\\`*_[]<>|":
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out)


def _cut_block(current: str) -> str:
    """The message text above our block, whichever release wrote it."""
    cut = len(current)
    for token in (MARKER, LEGACY_MARKER):
        at = current.find(token)
        if at != -1:
            cut = min(cut, at)
    return current[:cut].rstrip()


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
        block = self._results_markdown(
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
        # the block into, so fall back to a plain framed copy. It cannot jump
        # anywhere, so it does not pretend to: the Markdown is shown as text.
        return self._text_card(f"Trick: {query}", block, degraded)

    # -- the results block --------------------------------------------------
    def _results_markdown(
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
        """The block written into the message, in Markdown only.

        Raw HTML is not an option here. This Open WebUI build shows a message
        body as text when it carries HTML tags, so ``<details>`` and ``<a>``
        arrive on screen as their own source - measured on a real chat, not
        assumed. Markdown is rendered, and a Markdown link becomes a real anchor
        in the page itself, which is what a jump needs: the embed frame a card
        would be drawn in is sandboxed without ``allow-same-origin`` and cannot
        reach the chat at all.

        Every row links to ``#message-<id>``, the anchor the chat page gives each
        message element. That is the jump Open WebUI's own ``scrollToMessage``
        performs, but done by the browser: a fragment needs no script, no frame,
        and no router round-trip. Its ``?message=<id>`` parameter is only read
        while the chat page mounts, so a same-page navigation to it can arrive
        without scrolling.

        The trade-off is kept visible rather than hidden: Markdown has no
        collapsible element, so the block is capped at three rows with a short
        quote, and a second search replaces it instead of stacking.
        """
        lines: list[str] = []
        count = len(chat)
        limit = max(1, int(self.valves.CHAT_RESULTS))
        shown = min(count, limit)
        head = f"{MARKER}{_md(query)}"
        if count:
            head += f" · {count} passage{'' if count == 1 else 's'} match"
            if count > shown:
                # Saying "8 matches" above three rows reads as a bug; say both.
                head += f", showing the closest {shown}"
        lines.append(head)
        lines.append("")

        if origin == "unavailable":
            lines.append(
                "This conversation could not be read, so only the catalogues were "
                "searched. Try again, or use `trick tui --transcript` with an "
                "exported chat."
            )
            lines.append("")
        elif transcript and not chat:
            lines.append(f"No passage in this conversation contains “{_md(query)}”.")
            lines.append("")
        if weak:
            lines.append(
                "_Weak match: no passage carries the distinctive words of this "
                "question, so these are the closest turns. Check the matched terms "
                "before trusting a row._"
            )
            lines.append("")

        for index, row in enumerate(chat[:limit], start=1):
            lines.extend(self._row_markdown(index, row))
        if dropped:
            lines.append(f"_The oldest {dropped} turns were not searched._")
            lines.append("")
        if catalogue:
            lines.extend(self._catalogue_markdown(catalogue))
        elif hidden:
            lines.append(
                f"_{hidden} catalogue source{'s' if hidden != 1 else ''} matched "
                f"outside this conversation and {'are' if hidden != 1 else 'is'} "
                f"hidden (SOURCES={sources_mode})._"
            )
            lines.append("")
        if not chat and not catalogue and origin != "unavailable" and not transcript:
            lines.append(f"Nothing matched “{_md(query)}”.")
            lines.append("")
        note = "; ".join(str(item) for item in (degraded or []) if item)
        if note:
            lines.append(f"_degraded: {_md(note)}_")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _row_markdown(self, index: int, row: dict) -> list[str]:
        """One result row: a Markdown link, then the passage it points at.

        The link is the whole jump. ``#message-<id>`` is the id the chat page
        gives the message element, so the browser scrolls to it natively: no
        script, no frame, no permission, and no dependency on the router.
        """
        role = "you" if str(row.get("role")) == "user" else str(row.get("role") or "assistant")
        label = f"turn {int(row.get('turn') or 0)} · {role}"
        mid = str(row.get("message_id") or "")
        if mid:
            # "#message-<id>" is the id the chat page gives every message
            # element, and the one its own scrollToMessage looks up. A plain
            # fragment is scrolled by the browser itself: no script, no router
            # round-trip, no permission. "?message=<id>" is only read while the
            # chat page mounts, so a same-page navigation to it can land
            # without scrolling - the fragment cannot.
            quoted = urllib.parse.quote(mid, safe="")
            head = f"[{_md(label)}](#message-{quoted})"
        else:
            # No id means no jump target. Say so on the row instead of drawing a
            # link that does nothing: a dead anchor looks like a broken feature.
            head = f"{_md(label)} (no jump target)"
        detail = head
        matched = [str(term) for term in (row.get("matched") or []) if term]
        if matched:
            detail += f" · matched: {_md(', '.join(matched))}"
            try:
                # Coverage is the share of the question's distinctive weight this
                # passage carries; below the strong threshold the row is the
                # closest turn, not an answer. Say so on the row itself.
                if 0.0 < float(row.get("coverage") or 0.0) < 0.5:
                    detail += " (partial)"
            except (TypeError, ValueError):
                pass
        if self.valves.SHOW_SCORES:
            try:
                detail += f" · {float(row.get('final_score') or 0.0):.2f}"
            except (TypeError, ValueError):
                pass
        text = " ".join(str(row.get("text") or "").split())
        if len(text) > 240:
            text = text[:237].rstrip() + "..."
        # Three spaces indent the quote into the list item.
        return [f"{index}. {detail}", f"   > {_md(text)}", ""]

    @staticmethod
    def _catalogue_markdown(rows: list) -> list[str]:
        """Catalogue hits, kept plain: they are a different corpus, not a passage."""
        lines = ["_Catalogue sources (not from this conversation):_", ""]
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
            line = f"- {_md(label)}{score} — {_md(title)}"
            if summary:
                line += f"\n  > {_md(summary)}"
            lines.append(line)
        lines.append("")
        return lines

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
        base = _cut_block(current)
        content = f"{base}\n\n{block}" if base else block
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

    def _text_card(self, title: str, block: str, degraded: list | None = None) -> "_HTMLResponse":
        """Fallback embed for a caller with no message to write into.

        An API call has no chat message, so the results cannot be attached and
        no row can jump. The Markdown is therefore shown as preformatted text
        rather than dressed up as something clickable that is not.
        """
        note = "; ".join(str(item) for item in (degraded or []) if item)
        body = block + (f"\n\ndegraded: {note}" if note else "")
        return _HTMLResponse(
            content=(
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<title>{html.escape(title)}</title>"
                "<style>body{font:14px/1.5 system-ui,sans-serif;margin:1rem;"
                "color:#222}pre{white-space:pre-wrap;word-break:break-word}"
                "</style></head><body>"
                f"<pre>{html.escape(body)}</pre></body></html>"
            ),
            headers={"Content-Disposition": "inline"},
        )

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
