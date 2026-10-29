"""Command line entry point: ``python -m trick``.

Subcommands:

    trick query "mixed effects convergence" [--live] [--limit 8] [--json]
    trick tui                        interactive terminal session (Enter submits)
    trick serve                      the core service: unix socket + loopback HTTP
    trick mcp                        MCP server on stdio, for Gemini CLI / Open WebUI
    trick ingest [--source cran,bioc] [--max-docs N]
    trick doctor
    trick probe                      one real laya call, prints latency and usage

``tui``, ``mcp`` and the HTTP bridge all talk to the same core: if ``trick serve``
is running they forward to it, otherwise they build an engine in process. That is
why a query typed in the terminal and a query asked through Open WebUI cannot
disagree about ranking.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

from .bridge import BridgeServer, cors_from_env
from .credentials import DEFAULT_CREDENTIALS_PATH, load_laya_credentials
from .errors import ConfigError, TrickError
from .laya import LayaClient, build_scorer
from .mcp import EngineBackend, McpServer, build_backend
from .render import render_json, render_outcome
from .retrieval import (
    CRAN_DOWNLOAD_REF,
    CranDownloadsAdapter,
    bulk_adapters,
    log_weight,
)
from .search import SearchEngine, build_engine
from .service import (
    Service,
    ServiceClient,
    UnixServiceServer,
    load_or_create_token,
)
from .textindex import DocumentStore

DEFAULT_HOME = Path(os.environ.get("TRICK_HOME", Path.home() / ".local" / "share" / "trick"))
DEFAULT_DB = DEFAULT_HOME / "trick.db"
DEFAULT_MATRIX = DEFAULT_HOME / "scores.trk"
DEFAULT_SOCKET = DEFAULT_HOME / "run" / "trick.sock"
DEFAULT_TOKEN = DEFAULT_HOME / "bridge.token"
DEFAULT_HISTORY = DEFAULT_HOME / "history"


def _engine(args) -> SearchEngine:
    db = Path(args.db or DEFAULT_DB)
    matrix = Path(args.matrix) if getattr(args, "matrix", None) else DEFAULT_MATRIX
    return build_engine(db, matrix_path=matrix, scorer_mode=args.scorer, use_cache=not args.no_cache)


def _engine_factory(args):
    """A zero-argument constructor, so a service builds its engine lazily."""

    def factory() -> SearchEngine:
        return _engine(args)

    return factory


def _backend(args):
    """Prefer the running core service; otherwise search in this process."""
    socket_path = Path(getattr(args, "socket", None) or DEFAULT_SOCKET)
    client = ServiceClient(socket_path)
    try:
        client.call("ping")
    except TrickError:
        client.close()
        return EngineBackend(_engine(args))
    return client


def cmd_query(args) -> int:
    engine = _engine(args)
    try:
        outcome = engine.search(args.text, limit=args.limit, use_live=None if not args.offline else False)
    finally:
        engine.store.close()

    if args.json:
        print(render_json(outcome))
        return 0 if outcome.rows else 1

    print(render_outcome(outcome, verbose=args.verbose))
    return 0 if outcome.rows else 1


def cmd_tui(args) -> int:
    from .tui import TerminalUI

    backend = _backend(args)
    ui = TerminalUI(
        backend,
        limit=args.limit,
        live=not args.offline,
        history_path=Path(args.history) if args.history else DEFAULT_HISTORY,
    )
    return ui.run()


def cmd_mcp(args) -> int:
    socket_path = Path(args.socket or DEFAULT_SOCKET)
    backend = build_backend(socket_path, _engine_factory(args))
    server = McpServer(backend)
    try:
        return server.serve()
    finally:
        closer = getattr(backend, "close", None)
        if callable(closer):
            closer()


def cmd_serve(args) -> int:
    """Run the core service: one engine, a unix socket and an optional bridge."""
    socket_path = Path(args.socket or DEFAULT_SOCKET)
    service = Service(_engine_factory(args))
    server = UnixServiceServer(socket_path, service)
    try:
        server.bind()
    except TrickError as exc:
        print(f"trick serve: {exc.s_code} {exc}", file=sys.stderr)
        return 1
    server.start()
    print(f"trick serve: socket {socket_path}", file=sys.stderr, flush=True)

    bridge = None
    if not args.no_http:
        token_path = Path(args.token_file or DEFAULT_TOKEN)
        try:
            token = load_or_create_token(token_path)
            bridge = BridgeServer(
                service,
                token,
                host=args.http_host,
                port=args.http_port,
                cors_origins=cors_from_env(),
            )
            port = bridge.start()
        except TrickError as exc:
            print(f"trick serve: bridge disabled: {exc.s_code} {exc}", file=sys.stderr, flush=True)
            bridge = None
        else:
            print(
                f"trick serve: http http://{args.http_host}:{port}/v1/search  token {token_path}",
                file=sys.stderr,
                flush=True,
            )

    stopping = {"done": False}

    def _stop(signum, _frame):  # noqa: ANN001 - signal handler signature
        if stopping["done"]:
            return
        stopping["done"] = True
        print(f"trick serve: stopping (signal {signum})", file=sys.stderr, flush=True)
        server.stop()
        if bridge is not None:
            bridge.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass

    try:
        while not stopping["done"]:
            time.sleep(0.2)
    except KeyboardInterrupt:  # pragma: no cover - Ctrl-C without a handler
        _stop(2, None)
    finally:
        server.stop()
        if bridge is not None:
            bridge.stop()
        service.close()
    return 0


def cmd_ingest(args) -> int:
    wanted = [name.strip() for name in (args.source or "cran,bioc").split(",") if name.strip()]
    mapping = {"cran": "cran", "bioc": "bioconductor", "bioconductor": "bioconductor"}
    db = Path(args.db or DEFAULT_DB)
    store = DocumentStore(db)
    total = 0
    try:
        for adapter in bulk_adapters():
            if adapter.name not in [mapping.get(w, w) for w in wanted]:
                continue
            print(f"fetching {adapter.name} snapshot ...", flush=True)
            try:
                docs = adapter.fetch(timeout=args.timeout)
            except TrickError as exc:
                print(f"  {adapter.name}: {exc.s_code} {exc}", file=sys.stderr)
                continue
            if args.max_docs:
                docs = docs[: args.max_docs]
            rows = store.upsert_many(doc.as_row() for doc in docs)
            total += rows
            print(f"  {adapter.name}: {rows} documents indexed")
            weights = self_weights(adapter.name, docs, popularity=not args.no_popularity)
            if weights:
                stored = store.set_weights(weights)
                print(f"  {adapter.name}: {stored} ranking priors stored")
    finally:
        store.close()
    print(f"store now holds {total} upserted rows at {db}")
    return 0


def self_weights(source: str, docs, *, popularity: bool) -> list[tuple[str, str, float, str]]:
    """Build the canonicality prior for a batch of freshly ingested documents."""
    if source == "bioconductor":
        return [
            (d.source, d.key, float(d.meta.get("weight", 0.0)), f"revdeps={d.meta.get('revdeps', 0)}")
            for d in docs
        ]
    if source == "cran":
        if not popularity:
            return []
        names = [d.key for d in docs]
        try:
            counts = CranDownloadsAdapter().downloads(names)
        except TrickError as exc:
            print(f"  cran: popularity skipped ({exc.s_code})", file=sys.stderr)
            return []
        return [
            ("cran", name, log_weight(count, CRAN_DOWNLOAD_REF), f"downloads={count}")
            for name, count in counts.items()
        ]
    return []


def cmd_doctor(args) -> int:
    engine = _engine(args)
    info = engine.health()
    engine.store.close()
    try:
        creds = load_laya_credentials()
        info["credentials"] = {
            "origin": creds.origin,
            "base_url": creds.base_url,
            "timeout_ms": creds.timeout_ms,
            "key_present": bool(creds.api_key),
        }
    except ConfigError as exc:
        info["credentials"] = {"error": str(exc), "s_code": exc.s_code}
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def cmd_probe(args) -> int:
    """One authenticated call, timed, with usage counters."""
    import time

    try:
        creds = load_laya_credentials()
    except ConfigError as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return 2
    client = LayaClient(creds)
    started = time.monotonic()
    try:
        ranked = client.rank(
            args.text,
            [
                "[cran/r-package] lme4: Linear Mixed-Effects Models using Eigen and S4",
                "[arxiv/preprint] Mixed effects modelling in genomics",
                "[crossref/work] Spatial data analysis with random effects",
            ],
        )
    except TrickError as exc:
        print(f"probe: {exc.s_code} {exc}", file=sys.stderr)
        return 1
    elapsed = (time.monotonic() - started) * 1000.0
    print(f"probe: ok in {elapsed:.0f} ms, usage={client.last_usage}")
    for item in ranked:
        print(f"  {item.probability:5.3f}  candidate {item.index}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trick", description="Trick: scored retrieval powered by laya")
    parser.add_argument("--db", help=f"document store path (default {DEFAULT_DB})")
    parser.add_argument("--matrix", help=f"score matrix path (default {DEFAULT_MATRIX})")
    parser.add_argument("--scorer", default=os.environ.get("TRICK_SCORER", "auto"),
                        choices=["auto", "remote", "mock"], help="scorer backend")
    parser.add_argument("--no-cache", action="store_true", help="ignore the decision cache")
    sub = parser.add_subparsers(dest="command", required=True)

    query = sub.add_parser("query", help="run one search")
    query.add_argument("text")
    query.add_argument("--limit", type=int, default=8)
    query.add_argument("--live", action="store_true", help="include live source APIs (default on)")
    query.add_argument("--offline", action="store_true", help="local store and matrix only")
    query.add_argument("--json", action="store_true")
    query.add_argument("--verbose", action="store_true", help="show score components")
    query.set_defaults(func=cmd_query)

    tui = sub.add_parser("tui", help="interactive terminal session")
    tui.add_argument("--limit", type=int, default=8)
    tui.add_argument("--offline", action="store_true")
    tui.add_argument("--socket", help=f"core service socket (default {DEFAULT_SOCKET})")
    tui.add_argument("--history", help=f"history file (default {DEFAULT_HISTORY})")
    tui.set_defaults(func=cmd_tui)

    serve = sub.add_parser("serve", help="run the core service (unix socket + loopback HTTP)")
    serve.add_argument("--socket", help=f"socket path (default {DEFAULT_SOCKET})")
    serve.add_argument("--http-host", default="127.0.0.1")
    serve.add_argument("--http-port", type=int, default=8765)
    serve.add_argument("--token-file", help=f"bridge token file (default {DEFAULT_TOKEN})")
    serve.add_argument("--no-http", action="store_true", help="unix socket only")
    serve.set_defaults(func=cmd_serve)

    mcp = sub.add_parser("mcp", help="MCP server on stdio")
    mcp.add_argument("--socket", help=f"core service socket (default {DEFAULT_SOCKET})")
    mcp.set_defaults(func=cmd_mcp)

    ingest = sub.add_parser("ingest", help="download bulk catalogues into the store")
    ingest.add_argument("--source", default="cran,bioc")
    ingest.add_argument("--max-docs", type=int, default=0)
    ingest.add_argument("--timeout", type=float, default=90.0)
    ingest.add_argument("--no-popularity", action="store_true",
                        help="skip CRAN download counts (the ranking prior)")
    ingest.set_defaults(func=cmd_ingest)

    doctor = sub.add_parser("doctor", help="print environment, store and credential state")
    doctor.set_defaults(func=cmd_doctor)

    probe = sub.add_parser("probe", help="one real laya call")
    probe.add_argument("text", nargs="?", default="mixed effects model does not converge")
    probe.set_defaults(func=cmd_probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"trick: {exc.s_code} {exc}", file=sys.stderr)
        return 2
    except TrickError as exc:
        print(f"trick: {exc.s_code} {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
