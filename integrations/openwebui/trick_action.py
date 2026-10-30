"""
title: Trick
author: hakkisabah
author_url: https://github.com/hakkisabah/trick
funding_url: https://github.com/hakkisabah/trick
version: 0.6.0
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
body { font: 14px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; margin: 0; padding: 12px 14px; }
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
        messages = body.get("messages") if isinstance(body, dict) else None

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
        if chat:
            blocks.append((self._chat_block(chat, dropped), ""))
        if rows:
            blocks.append((self._rows_block(rows), ""))
        if not blocks:
            blocks.append((f"<p class='none'>Nothing matched <b>{html.escape(query)}</b>.</p>", ""))
        return self._card(f"Trick: {query}", blocks, degraded)

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
    def _card(self, title: str, blocks: list[tuple[str, str]], degraded: list | None = None) -> "_HTMLResponse":
        parts = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'>",
            f"<style>{STYLE}</style></head><body>",
            f"<div class='meta'>{html.escape(title)}</div>",
        ]
        for block, _ in blocks:
            parts.append(block)
        if degraded:
            note = "; ".join(html.escape(str(item)) for item in degraded if item)
            if note:
                parts.append(f"<p class='note'>degraded: {note}</p>")
        parts.append(
            "<p class='note'>* re-ordered by laya, - retrieval only. "
            "S4/S5 mean a source or the scorer did not answer; the ranking is then "
            "text-only, not wrong-but-hidden.</p>"
        )
        parts.append("</body></html>")
        return _HTMLResponse(content="".join(parts), headers={"Content-Disposition": "inline"})

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
