"""Command line entry point: ``python -m trick``.

Subcommands:

    trick query "mixed effects convergence" [--live] [--limit 8] [--json]
    trick tui                        interactive terminal session (Enter submits)
    trick serve                      the core service: unix socket + loopback HTTP
    trick mcp                        MCP server on stdio, for any MCP host
    trick ingest [--source cran,bioc] [--max-docs N] [--no-matrix]
    trick matrix build [--limit N]    rebuild the int8 score matrix from the store
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
from .errors import ConfigError, ContractViolation, TrickError
from .laya import LayaClient, build_scorer
from .matrix import MatrixIndex, MatrixMissing
from .matrixbuild import build_from_store
from .mcp import EngineBackend, McpServer, build_backend
from .render import render_json, render_outcome
from .retrieval import (
    CRAN_DOWNLOAD_REF,
    CranDownloadsAdapter,
    bulk_adapters,
    log_weight,
)
from .search import DEFAULT_LIVE_CAP, DEFAULT_LOCAL_FLOOR, SearchEngine, build_engine
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
    return build_engine(
        db,
        matrix_path=matrix,
        scorer_mode=args.scorer,
        use_cache=not args.no_cache,
        local_floor=int(getattr(args, "local_floor", DEFAULT_LOCAL_FLOOR)),
        live_cap=int(getattr(args, "live_cap", DEFAULT_LIVE_CAP)),
    )


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


def _load_transcript(path: str | None) -> list[dict] | None:
    """Read a saved conversation (JSON array of {role, content}) for chat scope."""
    if not path:
        return None
    file = Path(path)
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractViolation(f"cannot read transcript {file}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ContractViolation(f"transcript {file} is not JSON: {exc}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        payload = payload["messages"]
    if not isinstance(payload, list):
        raise ContractViolation(f"transcript {file} must be a JSON array of {{role, content}}")
    return payload


def cmd_query(args) -> int:
    engine = _engine(args)
    messages = _load_transcript(getattr(args, "transcript", None))
    try:
        outcome = engine.search(
            args.text,
            limit=args.limit,
            use_live=None if not args.offline else False,
            messages=messages,
            scope="both" if messages else "sources",
        )
    finally:
        engine.store.close()

    if args.json:
        print(render_json(outcome))
        return 0 if outcome.rows or outcome.chat else 1

    print(render_outcome(outcome, verbose=args.verbose))
    return 0 if outcome.rows or outcome.chat else 1


def cmd_tui(args) -> int:
    from .tui import TerminalUI

    backend = _backend(args)
    ui = TerminalUI(
        backend,
        limit=args.limit,
        live=not args.offline,
        history_path=Path(args.history) if args.history else DEFAULT_HISTORY,
        messages=_load_transcript(getattr(args, "transcript", None)),
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
                allow_network=bool(getattr(args, "expose_network", False)),
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
    if not getattr(args, "no_matrix", False):
        matrix = Path(args.matrix) if getattr(args, "matrix", None) else DEFAULT_MATRIX
        _print_matrix(build_matrix(db, matrix))
    return 0


def build_matrix(db: Path, path: Path, *, limit: int = 0) -> dict:
    """Rebuild the score matrix from the store; safe to run at any time."""
    store = DocumentStore(db)
    try:
        return build_from_store(store, path, limit=limit)
    finally:
        store.close()


def _print_matrix(info: dict) -> None:
    print(
        f"matrix: {info['records']} records, {info['bytes']} bytes, "
        f"{info['seconds']}s at {info['path']}"
    )


def cmd_matrix(args) -> int:
    path = Path(args.matrix) if getattr(args, "matrix", None) else DEFAULT_MATRIX
    if args.action == "build":
        db = Path(args.db or DEFAULT_DB)
        _print_matrix(build_matrix(db, path, limit=args.limit))
        return 0
    try:
        index = MatrixIndex(path)
    except MatrixMissing as exc:
        print(json.dumps({"path": str(path), "present": False, "reason": str(exc)}, indent=2))
        return 0
    try:
        info = {
            "path": str(path),
            "present": True,
            "records": index.count,
            "synthetic": index.synthetic,
            "bytes": Path(path).stat().st_size,
        }
    finally:
        index.close()
    print(json.dumps(info, indent=2))
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


def _remote(args):
    from .vaultremote import RemoteVault, read_token

    home = Path(args.home or DEFAULT_HOME)
    url = args.remote or os.environ.get("TRICK_VAULT_URL")
    if not url:
        raise ConfigError(
            "--remote https://host:port is required, or set TRICK_VAULT_URL for a timer"
        )
    return RemoteVault(
        url,
        read_token(home),
        fingerprint=args.fingerprint or os.environ.get("TRICK_VAULT_FINGERPRINT"),
        ca_file=args.ca,
        timeout=args.timeout,
        allow_plaintext=args.allow_plaintext,
    )


def cmd_backup(args) -> int:
    """Encrypted memory: the decision cache, never the catalogue.

    The catalogue is 44 MB of public data that one ingest rebuilds; the decision
    cache is a few hundred rows that cost model calls to produce. That asymmetry
    is what makes a 512 MB cloud box enough, and it is why this command refuses to
    be a full-disk copy.

    ``push`` and ``pull`` move the same archive to a store that cannot read it.
    ``push`` reads the object back before it reports success, because an upload
    nobody verified is not a backup.
    """
    from .vault import (
        VaultError,
        describe,
        export_archive,
        import_archive,
        read_archive,
        read_passphrase,
    )

    home = Path(args.home or DEFAULT_HOME)
    try:
        if args.action == "export":
            passphrase = read_passphrase(confirm=not args.no_confirm)
            result = export_archive(home, Path(args.file), passphrase)
            print(json.dumps(result, indent=2))
            return 0
        if args.action == "info":
            payload = read_archive(Path(args.file), read_passphrase())
            print(json.dumps(describe(payload), indent=2))
            return 0
        if args.action == "import":
            passphrase = read_passphrase()
            result = import_archive(Path(args.file), home, passphrase)
            print(json.dumps(result, indent=2))
            return 0
        if args.action == "run":
            # What a timer calls: export, push, clean up, in one step. A backup
            # that needs a human to remember it is not a backup.
            import tempfile

            remote = _remote(args)
            if not os.environ.get("TRICK_BACKUP_PASSPHRASE"):
                raise VaultError(
                    "backup run needs TRICK_BACKUP_PASSPHRASE in the environment: a timer has "
                    "no terminal to ask on, and a passphrase in an argument is a leaked one"
                )
            workdir = Path(tempfile.mkdtemp(prefix="trick-backup-"))
            archive = Path(args.file) if args.file else workdir / "memory.trk"
            try:
                exported = export_archive(home, archive, read_passphrase())
                payload = read_archive(archive, read_passphrase())
                pushed = remote.put(
                    archive.read_bytes(),
                    rows=len(payload.get("decisions") or []),
                    created_at=int(payload.get("created_at") or 0),
                )
            finally:
                if archive.exists() and not args.file:
                    archive.unlink()
                if workdir.exists():
                    for leftover in workdir.iterdir():
                        leftover.unlink(missing_ok=True)
                    workdir.rmdir()
            print(
                json.dumps(
                    {
                        "bytes": exported["bytes"],
                        "decisions": exported["decisions"],
                        "sha256": pushed["sha256"],
                        "verified": pushed["verified"],
                        "already_stored": pushed["already_stored"],
                    },
                    indent=2,
                )
            )
            return 0
        if args.action == "push":
            remote = _remote(args)
            archive = Path(args.file)
            if not archive.exists():
                raise VaultError(f"no such archive: {archive}")
            payload = read_archive(archive, read_passphrase())
            result = remote.put(
                archive.read_bytes(),
                rows=len(payload.get("decisions") or []),
                created_at=int(payload.get("created_at") or 0),
            )
            result["account"] = remote.usage()["account"]
            print(json.dumps(result, indent=2))
            return 0
        if args.action == "pull":
            remote = _remote(args)
            if not args.file:
                raise VaultError("pull needs a snapshot id (see: trick backup list)")
            out = Path(args.out or "memory.trk")
            passphrase = read_passphrase() if args.verify else None
            result = remote.pull(args.file, out, passphrase)
            print(json.dumps(result, indent=2))
            return 0
        if args.action == "list":
            print(json.dumps(_remote(args).listing(), indent=2))
            return 0
        if args.action == "usage":
            print(json.dumps(_remote(args).usage(), indent=2))
            return 0
        if args.action == "delete":
            if not args.file:
                raise VaultError("delete needs a snapshot id")
            print(json.dumps(_remote(args).delete(args.file), indent=2))
            return 0
    except VaultError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"backup: {exc.s_code} {exc}", file=sys.stderr)
        return 2
    print(f"unknown backup action {args.action!r}", file=sys.stderr)
    return 2


def cmd_vault(args) -> int:
    """The server half: what runs on the box that holds the ciphertext."""
    from .vaultremote import (
        VaultServer,
        VaultStore,
        init_store,
        load_accounts,
        server_ssl_context,
    )

    if args.action == "init":
        result = init_store(Path(args.root), name=args.account)
        print(json.dumps(result, indent=2))
        print(
            "keep this token in the client's TRICK_VAULT_TOKEN, or in "
            f"{Path(args.home or DEFAULT_HOME) / 'vault.token'} with mode 600",
            file=sys.stderr,
        )
        return 0
    if args.action == "plans":
        from .plans import plans_table

        print(json.dumps(plans_table(), indent=2))
        return 0
    if args.action in ("plan", "device"):
        accounts_path = Path(args.accounts or (Path(args.root) / "accounts.json"))
        store = VaultStore(Path(args.root), load_accounts(accounts_path), accounts_path=accounts_path)
        try:
            if args.action == "plan":
                if not args.set:
                    print(json.dumps(store.account(args.account).as_dict(), indent=2))
                    return 0
                result = store.apply_plan(args.account, args.set)
            else:
                result = store.add_device(args.account)
        except ConfigError as exc:
            print(f"vault: {exc.s_code} {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    if args.action != "serve":
        print(f"unknown vault action {args.action!r}", file=sys.stderr)
        return 2
    accounts_path = Path(args.accounts or (Path(args.root) / "accounts.json"))
    accounts = load_accounts(accounts_path)
    store = VaultStore(Path(args.root), accounts, accounts_path=accounts_path)
    tls = server_ssl_context(Path(args.cert), Path(args.key)) if args.cert and args.key else None
    admin_secret = args.admin_secret or os.environ.get("TRICK_ADMIN_SECRET") or ""
    server = VaultServer(
        store,
        host=args.host,
        port=args.port,
        tls=tls,
        allow_plaintext=args.allow_plaintext,
        expose_public=args.expose_public,
        admin_secret=admin_secret,
    )
    scheme = "https" if tls is not None else "http"
    print(
        f"vault: listening on {scheme}://{args.host}:{args.port} "
        f"root={args.root} accounts={len(accounts)}",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


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
    parser.add_argument("--local-floor", type=int,
                        default=int(os.environ.get("TRICK_LOCAL_FLOOR", DEFAULT_LOCAL_FLOOR)),
                        help="shortlist slots reserved for local catalogue entries")
    parser.add_argument("--live-cap", type=int,
                        default=int(os.environ.get("TRICK_LIVE_CAP", DEFAULT_LIVE_CAP)),
                        help="shortlist slots one live source may take")
    sub = parser.add_subparsers(dest="command", required=True)

    query = sub.add_parser("query", help="run one search")
    query.add_argument("text")
    query.add_argument("--limit", type=int, default=8)
    query.add_argument("--live", action="store_true", help="include live source APIs (default on)")
    query.add_argument("--offline", action="store_true", help="local store and matrix only")
    query.add_argument("--json", action="store_true")
    query.add_argument("--verbose", action="store_true", help="show score components")
    query.add_argument("--transcript", help="JSON conversation to search inside ({role, content} array)")
    query.set_defaults(func=cmd_query)

    tui = sub.add_parser("tui", help="interactive terminal session")
    tui.add_argument("--limit", type=int, default=8)
    tui.add_argument("--offline", action="store_true")
    tui.add_argument("--socket", help=f"core service socket (default {DEFAULT_SOCKET})")
    tui.add_argument("--history", help=f"history file (default {DEFAULT_HISTORY})")
    tui.add_argument("--transcript", help="JSON conversation to search inside ({role, content} array)")
    tui.set_defaults(func=cmd_tui)

    serve = sub.add_parser("serve", help="run the core service (unix socket + loopback HTTP)")
    serve.add_argument("--socket", help=f"socket path (default {DEFAULT_SOCKET})")
    serve.add_argument("--http-host", default="127.0.0.1")
    serve.add_argument("--expose-network", action="store_true",
                       help="allow --http-host to be a non-loopback local address (e.g. the docker bridge)")
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
    ingest.add_argument("--no-matrix", action="store_true", help="do not rebuild the score matrix")
    ingest.add_argument("--matrix", help=f"matrix path (default {DEFAULT_MATRIX})")
    ingest.add_argument("--no-popularity", action="store_true",
                        help="skip CRAN download counts (the ranking prior)")
    ingest.set_defaults(func=cmd_ingest)

    matrix = sub.add_parser("matrix", help="build or inspect the int8 score matrix")
    matrix.add_argument("action", choices=["build", "info"])
    matrix.add_argument("--limit", type=int, default=0, help="stop after N documents")
    matrix.add_argument("--db", help=f"store path (default {DEFAULT_DB})")
    matrix.add_argument("--matrix", help=f"matrix path (default {DEFAULT_MATRIX})")
    matrix.set_defaults(func=cmd_matrix)

    doctor = sub.add_parser("doctor", help="print environment, store and credential state")
    doctor.set_defaults(func=cmd_doctor)

    probe = sub.add_parser("probe", help="one real laya call")
    probe.add_argument("text", nargs="?", default="mixed effects model does not converge")
    probe.set_defaults(func=cmd_probe)

    backup = sub.add_parser("backup", help="encrypted memory: export, inspect, merge, push or pull")
    backup.add_argument(
        "action",
        choices=("export", "info", "import", "push", "pull", "list", "usage", "delete", "run"),
    )
    backup.add_argument("file", nargs="?", help="archive path (push), snapshot id (pull/delete)")
    backup.add_argument("--home", help=f"state directory (default {DEFAULT_HOME})")
    backup.add_argument("--out", help="where a pulled archive is written (default memory.trk)")
    backup.add_argument("--remote", help="store base url, e.g. https://vault.example.com:8790")
    backup.add_argument("--fingerprint", help="sha256 of the store's DER certificate (pinning)")
    backup.add_argument("--ca", help="CA bundle to verify the store instead of pinning")
    backup.add_argument("--timeout", type=float, default=30.0)
    backup.add_argument("--verify", action="store_true", help="after pull, decrypt and print a summary")
    backup.add_argument(
        "--allow-plaintext",
        action="store_true",
        help="allow http:// on loopback only, for tests",
    )
    backup.add_argument(
        "--no-confirm",
        action="store_true",
        help="do not ask for the passphrase twice when exporting",
    )
    backup.set_defaults(func=cmd_backup)

    vault = sub.add_parser("vault", help="run or initialise the remote memory store")
    vault.add_argument("action", choices=("init", "serve", "plans", "plan", "device"))
    vault.add_argument("--root", default=str(Path(DEFAULT_HOME) / "vault-store"))
    vault.add_argument("--accounts", help="accounts file (default <root>/accounts.json)")
    vault.add_argument("--account", default="owner", help="account name for init/plan/device")
    vault.add_argument("--set", help="plan to apply when action is 'plan' (see: vault plans)")
    vault.add_argument("--admin-secret", help="HMAC secret for /v1/admin/entitlement (or TRICK_ADMIN_SECRET)")
    vault.add_argument("--host", default="127.0.0.1")
    vault.add_argument("--port", type=int, default=8790)
    vault.add_argument("--cert", help="TLS certificate (fullchain)")
    vault.add_argument("--key", help="TLS private key")
    vault.add_argument("--home", help=f"client state directory (default {DEFAULT_HOME})")
    vault.add_argument("--allow-plaintext", action="store_true", help="loopback tests only")
    vault.add_argument(
        "--expose-public",
        action="store_true",
        help="permit a wildcard bind; requires TLS, meant for a store behind a firewall",
    )
    vault.set_defaults(func=cmd_vault)
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
