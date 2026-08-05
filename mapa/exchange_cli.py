#!/usr/bin/env python3
"""Closed CLI for the supported collective-memory exchange boundary."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exchange import (  # noqa: E402
    ExchangeCapability,
    ExchangeConfig,
    ExchangeError,
    ExportBoundary,
    PublicationBoundary,
    TrustStore,
    canonical_bytes,
    load_json_file,
)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ExchangeError("missing_configuration", f"{name} is required")
    return value


def _roots() -> tuple[Path, Path]:
    root = Path(_required_env("MAPA_ROOT"))
    data = Path(os.environ.get("MAPA_DATA") or root / ".mapa")
    return root, data


def _config() -> ExchangeConfig:
    return ExchangeConfig.load(_required_env("MAPA_EXCHANGE_CONFIG"))


def _export_boundary() -> ExportBoundary:
    root, data = _roots()
    capability = ExchangeCapability.load(
        _required_env("MAPA_EXCHANGE_READER_CAPABILITY")
    )
    catalog = load_json_file(_required_env("MAPA_EXCHANGE_CATALOG"))
    return ExportBoundary(root, data, _config(), capability, lambda _scope: catalog)


def _publication_boundary() -> PublicationBoundary:
    root, data = _roots()
    capability = ExchangeCapability.load(
        _required_env("MAPA_EXCHANGE_PUBLISHER_CAPABILITY")
    )
    trust = TrustStore.load(_required_env("MAPA_EXCHANGE_TRUST"))
    return PublicationBoundary(root, data, _config(), capability, trust)


def _emit(value: object) -> None:
    sys.stdout.buffer.write(canonical_bytes(value) + b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collective-exchange")
    sub = parser.add_subparsers(dest="command", required=True)

    export_create = sub.add_parser("export-create")
    export_create.add_argument("--scope", required=True)

    export_page = sub.add_parser("export-page")
    export_page.add_argument("--generation", required=True)
    export_page.add_argument("--cursor")
    export_page.add_argument("--limit", type=int, default=100)

    export_object = sub.add_parser("export-object")
    export_object.add_argument("--generation", required=True)
    export_object.add_argument("--content-ref", required=True)

    preview = sub.add_parser("publication-preview")
    preview.add_argument("--draft", required=True)

    plan = sub.add_parser("publication-plan")
    plan.add_argument("--request", required=True)

    apply = sub.add_parser("publication-apply")
    apply.add_argument("--request", required=True)
    apply.add_argument("--plan", required=True)

    reconcile = sub.add_parser("publication-reconcile")
    reconcile.add_argument("--receipt", required=True)

    sub.add_parser("publication-recover")
    args = parser.parse_args(argv)

    try:
        if args.command == "export-create":
            _emit(_export_boundary().create(args.scope))
        elif args.command == "export-page":
            _emit(
                _export_boundary().page(
                    args.generation,
                    cursor=args.cursor,
                    limit=args.limit,
                )
            )
        elif args.command == "export-object":
            sys.stdout.buffer.write(
                _export_boundary().object_bytes(args.generation, args.content_ref)
            )
        elif args.command == "publication-preview":
            _emit(_publication_boundary().preview(load_json_file(args.draft)))
        elif args.command == "publication-plan":
            _emit(_publication_boundary().plan(load_json_file(args.request)))
        elif args.command == "publication-apply":
            _emit(
                _publication_boundary().apply(
                    load_json_file(args.request),
                    load_json_file(args.plan),
                )
            )
        elif args.command == "publication-reconcile":
            _emit(_publication_boundary().reconcile(args.receipt))
        elif args.command == "publication-recover":
            _emit(
                {
                    "schema": "collective-publication-recovery/v1",
                    "outcomes": _publication_boundary().recover(),
                }
            )
        return 0
    except ExchangeError as error:
        print(
            json.dumps(error.as_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
