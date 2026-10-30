"""
title: Trick
author: hakkisabah
author_url: https://github.com/hakkisabah/trick
funding_url: https://github.com/hakkisabah/trick
version: 0.5.0
required_open_webui_version: 0.5.0
description: Scored retrieval over the conversation and scientific catalogues, ranked by laya.
"""

# Trick as an Open WebUI Pipe.
#
# A Pipe in Open WebUI is a model: it appears in the model list and its ``pipe``
# method answers a chat turn. That is exactly the shape Trick wants - the user
# picks "Trick", types a question, presses Enter, and gets ranked sources
# instead of prose. No prompt engineering, no tool round-trip, no model in the
# middle inventing citations.
#
# This file holds no ranking logic. It is a client for ``trick serve``'s loopback
# bridge, because a second copy of the ranking code inside a container would
# drift from the terminal's answer within a week.
#
# Two deployment facts shape the code:
#
# * Open WebUI usually runs in a container, so ``127.0.0.1`` in this file means
#   "the Open WebUI container", not the host. The compose file ships
#   ``extra_hosts: host.docker.internal:host-gateway``, so the default valve
#   points there. A native install uses ``127.0.0.1``.
# * the bridge binds to loopback with a bearer token, so the token is a valve.
#   It is pasted once from ``$TRICK_HOME/bridge.token``; it is never logged here
#   and never echoed back in an error message.

from __future__ import annotations

import json
import urllib.error
import urllib.request

from pydantic import BaseModel, Field

SOURCE_LABELS = {
    "cran": "CRAN",
    "bioconductor": "Bioconductor",
    "arxiv": "arXiv",
    "openalex": "OpenAlex",
    "crossref": "Crossref",
    "local": "local",
}


class Pipe:
    class Valves(BaseModel):
        TRICK_URL: str = Field(
            default="http://host.docker.internal:8765/v1/search",
            description="Trick bridge endpoint (trick serve --http-port).",
        )
        TRICK_TOKEN: str = Field(
            default="",
            description="Bearer token from $TRICK_HOME/bridge.token (0600 file).",
        )
        LIMIT: int = Field(default=8, description="How many sources to show (1-26).")
        LIVE: bool = Field(default=True, description="Query the live source APIs as well.")
        TIMEOUT_S: int = Field(default=25, description="Bridge timeout in seconds.")
        SHOW_SCORES: bool = Field(default=True, description="Show the blended score per row.")
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

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "trick"
        self.name = "Trick: "
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": "trick-search", "name": "Trick - scored scientific sources"}]

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
    def pipe(self, user_message: str, model_id: str, messages: list, body: dict):
        query = " ".join(str(user_message or "").split())
        if not query:
            return "Trick needs a question: type what you are looking for."

        transcript, dropped = self._transcript(messages)
        payload = {
            "query": query[:2000],
            "limit": max(1, min(int(self.valves.LIMIT), 26)),
            "live": bool(self.valves.LIVE),
        }
        if transcript:
            payload["messages"] = transcript
            payload["scope"] = "both"

        answer = self._post(payload)
        if not answer.get("ok"):
            return self._explain_failure(answer)

        result = answer.get("result") or {}
        rows = result.get("rows") or []
        chat = result.get("chat") or []
        report = result.get("report") or {}
        degraded = result.get("degraded_text") or self._explain_codes(result.get("degraded") or [])

        if not rows and not chat:
            return f"**Nothing matched `{query}`.**\n\n" + self._degraded_block(degraded)

        lines: list[str] = []
        if chat:
            lines.extend(self._chat_lines(chat, dropped))
            lines.append("")
        if not rows:
            lines.append(f"**No catalogue source matched `{query}`.**")
            block = self._degraded_block(degraded)
            if block:
                lines.append("")
                lines.append(block)
            return "\n".join(lines)

        lines.append(f"**{len(rows)} scored sources for `{query}`**")
        lines.append("")
        for row in rows:
            label = SOURCE_LABELS.get(row.get("source", ""), row.get("source", "?"))
            score = row.get("final_score")
            marker = "*" if row.get("score_label") == "laya" else "-"
            suffix = ""
            if self.valves.SHOW_SCORES and isinstance(score, (int, float)):
                suffix = f" - `{score:.3f}`"
            title = str(row.get("title") or row.get("key") or "").strip()
            url = str(row.get("url") or "").strip()
            head = f"**[{title}]({url})**" if url else f"**{title}**"
            lines.append(f"{row.get('rank', '?')}. {head}")
            summary = str(row.get("summary") or "").strip().replace("\n", " ")
            if summary:
                if len(summary) > 240:
                    summary = summary[:237].rstrip() + "..."
                lines.append(f"   {summary}")
            lines.append(f"   `{label}` / `{row.get('key', '')}` {marker}{suffix}")
            lines.append("")
        lines.append("`*` scored by laya, `-` retrieval only. `S4` or `S5` mean a source or the")
        lines.append("scorer did not answer; the ranking is then text-only, not wrong-but-hidden.")
        block = self._degraded_block(degraded)
        if block:
            lines.append("")
            lines.append(block)
        return "\n".join(lines)

    # -- conversation scope ------------------------------------------------
    def _transcript(self, messages) -> tuple[list[dict], int]:
        """Window the chat down to what may be sent, and say how much was dropped.

        The service refuses a transcript bigger than its contract instead of
        silently truncating, so the window is chosen here, where the number of
        dropped turns can be shown to the reader. Text is taken as-is: a chat
        message is evidence, so rewriting it would change what is being searched.
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
                # Everything older than this message is out; count it, because
                # "the oldest N turns were not searched" has to be true.
                cut = position + 1
                break
            kept.append(message)
        dropped += cut
        kept.reverse()
        return kept, dropped

    def _chat_lines(self, chat: list, dropped: int) -> list[str]:
        shown = chat[: max(1, int(self.valves.CHAT_RESULTS))]
        head = f"**{len(chat)} conversation match{'es' if len(chat) != 1 else ''} in this chat**"
        if dropped:
            head += f" _(oldest {dropped} turns not searched)_"
        lines = [head, ""]
        for row in shown:
            role = "you" if row.get("role") == "user" else str(row.get("role") or "assistant")
            turn = row.get("turn")
            marker = "*" if row.get("scored_by_laya") else "-"
            lines.append(f"- **turn {turn}** ({role}) {marker}")
            text = " ".join(str(row.get("text") or "").split())
            if len(text) > 400:
                text = text[:397].rstrip() + "..."
            lines.append(f"  > {text}")
        return lines

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _explain_codes(codes: list) -> list:
        from_text = {
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
        return [from_text.get(str(code), str(code)) for code in codes]

    @staticmethod
    def _degraded_block(degraded: list) -> str:
        if not degraded:
            return ""
        return "> degraded: " + "; ".join(str(item) for item in degraded)

    def _explain_failure(self, answer: dict) -> str:
        status = answer.get("status")
        error = answer.get("error") or {}
        code = error.get("s_code") or ""
        message = error.get("message") or "unknown error"
        if status == 401:
            hint = (
                "The bridge rejected the token. Paste the contents of "
                "`$TRICK_HOME/bridge.token` into the TRICK_TOKEN valve of this Pipe."
            )
        elif status in (0, None, 502, 503):
            hint = (
                "The Trick service is not answering. Start it with `trick serve` "
                "on the host and check that the TRICK_URL valve points at it "
                "(inside Docker: `http://host.docker.internal:8765/v1/search`)."
            )
        else:
            hint = "The service answered with an error."
        return f"**Trick could not search.** {hint}\n\n`{code} {message}`"
