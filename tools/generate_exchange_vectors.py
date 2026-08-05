#!/usr/bin/env python3
"""Generate byte-stable public vectors for the exchange v1 contract."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "mapa"))

from exchange import (
    EXPORT_CATALOG_SCHEMA,
    PUBLICATION_DRAFT_SCHEMA,
    PUBLICATION_EVIDENCE_SCHEMA,
    PUBLICATION_REQUEST_SCHEMA,
    ExchangeCapability,
    ExchangeConfig,
    ExportBoundary,
    PublicationBoundary,
    TrustStore,
    canonical_bytes,
)

NOW = "2026-08-05T06:00:00.000000Z"
FIXED_INSTANT = dt.datetime(2026, 8, 5, 6, tzinfo=dt.timezone.utc)
EARLY = "2026-08-01T00:00:00.000000Z"
LATE = "2026-08-10T00:00:00.000000Z"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class VectorProjectionRunner:
    def __init__(self, root: Path):
        self.root = root

    def snapshot(self, destination: Path):
        backup = destination / "projection-backup"
        backup.mkdir()
        (backup / "snapshot.json").write_bytes(canonical_bytes({"present": []}) + b"\n")
        return {"present": []}

    def restore(self, _destination: Path):
        return None

    def build(self):
        return None

    def verify(self, relative_path: str, content_hash: str):
        content = (self.root / relative_path).read_bytes()
        if sha(content) != content_hash:
            raise RuntimeError("vector projection mismatch")
        return {
            "index_generation": "vector-index-1",
            "ui_generation": "vector-ui-1",
            "index_content_hash": content_hash,
        }


def trust_key(kid, principal, private, roles):
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return {
        "kid": kid,
        "principal": principal,
        "roles": roles,
        "public_key": b64u(public),
        "not_before": EARLY,
        "not_after": LATE,
        "revoked_at": None,
    }


def sign_evidence(private, kid, issuer, kind, draft, preview):
    body = {
        "schema": PUBLICATION_EVIDENCE_SCHEMA,
        "kind": kind,
        "evidence_id": f"evidence:{kind}:vector",
        "issuer": issuer,
        "subject_id": draft["subject_id"],
        "requester_id": draft["requester_id"],
        "action": draft["action"],
        "target_id": draft["target_id"],
        "source_checkpoint": draft["source_checkpoint"],
        "classification": draft["classification"],
        "policy_version": draft["policy_version"],
        "preview_hash": preview["preview_hash"],
        "content_hash": preview["body"]["rendered"]["content_hash"],
        "issued_at": EARLY,
        "not_before": EARLY,
        "not_after": LATE,
    }
    return {
        "schema": PUBLICATION_EVIDENCE_SCHEMA,
        "body": body,
        "signature": {
            "alg": "Ed25519",
            "kid": kid,
            "value": b64u(private.sign(canonical_bytes(body))),
        },
    }


def build_vectors() -> dict[str, bytes]:
    temp = tempfile.TemporaryDirectory(prefix="exchange-vectors.")
    try:
        base = Path(temp.name)
        root = base / "corpus"
        data = base / "data"
        (root / "mapa").mkdir(parents=True)
        data.mkdir()
        (root / "mapa" / "public.md").write_text(
            "# Public collective source\n", encoding="utf-8"
        )
        config = ExchangeConfig.from_object(
            {
                "schema": "collective-exchange-config/v1",
                "producer_instance": "collective:vector",
                "producer_release": "collective:release:vector",
                "policy_version": "policy:v1",
                "targets": [
                    {
                        "target_id": "collective:article:vector",
                        "relative_path": "published/vector.md",
                    }
                ],
                "index_scope": "total",
            }
        )
        reader = ExchangeCapability(
            "cap:vector:reader", "export-reader", ("public",), b"r" * 32
        )
        publisher = ExchangeCapability(
            "cap:vector:publisher",
            "reviewed-publisher",
            ("collective:article:vector",),
            b"p" * 32,
        )
        catalog = {
            "schema": EXPORT_CATALOG_SCHEMA,
            "policy_version": "policy:v1",
            "scope_id": "public",
            "entries": [
                {
                    "artifact_id": "artifact:vector:v1",
                    "logical_id": "logical:vector",
                    "relative_path": "mapa/public.md",
                    "media_type": "text/markdown; charset=utf-8",
                    "authors": ["author:vector"],
                    "source_refs": [{"id": "source:vector", "hash": "1" * 64}],
                    "license": "MIT",
                    "consent_scope": "public",
                    "classification": "public",
                    "predecessor_artifact_id": None,
                    "state": "active",
                }
            ],
        }
        export = ExportBoundary(
            root,
            data,
            config,
            reader,
            lambda _scope: catalog,
            clock=lambda: FIXED_INSTANT,
        )
        manifest = export.create("public")
        page = export.page(manifest["generation_id"], limit=1)

        subject = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        reviewer = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
        trust = TrustStore.from_object(
            {
                "schema": "collective-exchange-trust/v1",
                "keys": [
                    trust_key(
                        "key:vector:subject",
                        "being:vector",
                        subject,
                        ["subject-consent"],
                    ),
                    trust_key(
                        "key:vector:reviewer",
                        "human:vector-reviewer",
                        reviewer,
                        ["independent-review"],
                    ),
                ],
            }
        )
        publication = PublicationBoundary(
            root,
            data,
            config,
            publisher,
            trust,
            projection_runner=VectorProjectionRunner(root),
            clock=lambda: FIXED_INSTANT,
        )
        draft = {
            "schema": PUBLICATION_DRAFT_SCHEMA,
            "action": "publish",
            "requester_id": "operator:matrix-vector",
            "subject_id": "being:vector",
            "target_id": "collective:article:vector",
            "source_refs": [{"id": "dm:event:vector", "hash": "2" * 64}],
            "source_checkpoint": {"id": "dm:checkpoint:vector", "hash": "3" * 64},
            "classification": "public",
            "policy_version": "policy:v1",
            "media_type": "text/markdown; charset=utf-8",
            "title": "Reviewed vector",
            "body": "Deterministic inert publication bytes.",
            "predecessor_receipt_id": None,
            "predecessor_receipt_hash": None,
        }
        preview = publication.preview(draft)
        consent = sign_evidence(
            subject, "key:vector:subject", "being:vector", "consent", draft, preview
        )
        review = sign_evidence(
            reviewer,
            "key:vector:reviewer",
            "human:vector-reviewer",
            "review",
            draft,
            preview,
        )
        request = {
            "schema": PUBLICATION_REQUEST_SCHEMA,
            "draft": draft,
            "preview_hash": preview["preview_hash"],
            "idempotency_key": "idempotency:vector",
            "consent": consent,
            "review": review,
        }
        plan = publication.plan(request)
        receipt = publication.apply(request, plan)
        reconciliation = publication.reconcile(receipt["receipt_id"])
        negative = dict(draft)
        negative["host_path"] = "/tmp/forbidden"
        objects = {
            "export-catalog.json": catalog,
            "export-manifest.json": manifest,
            "export-page.json": page,
            "publication-draft.json": draft,
            "publication-preview.json": preview,
            "publication-consent.json": consent,
            "publication-review.json": review,
            "publication-request.json": request,
            "publication-plan.json": plan,
            "publication-receipt.json": receipt,
            "publication-reconciliation.json": reconciliation,
            "negative-host-path.json": negative,
        }
        rendered = {
            name: canonical_bytes(value) + b"\n" for name, value in objects.items()
        }
        index = {
            "schema": "collective-exchange-vector-index/v1",
            "generator": "tools/generate_exchange_vectors.py",
            "files": [
                {"name": name, "sha256": sha(content), "size": len(content)}
                for name, content in sorted(rendered.items())
            ],
        }
        rendered["index.json"] = canonical_bytes(index) + b"\n"
        return rendered
    finally:
        temp.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(REPO / "vectors" / "exchange" / "v1"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = Path(args.output)
    vectors = build_vectors()
    if args.check:
        for name, content in vectors.items():
            path = output / name
            if not path.is_file() or path.read_bytes() != content:
                print(f"vector drift: {name}", file=sys.stderr)
                return 1
        extras = sorted(
            path.name for path in output.glob("*.json") if path.name not in vectors
        )
        if extras:
            print("unexpected vectors: " + ", ".join(extras), file=sys.stderr)
            return 1
        return 0
    output.mkdir(parents=True, exist_ok=True)
    for existing in output.glob("*.json"):
        if existing.name not in vectors:
            existing.unlink()
    for name, content in vectors.items():
        (output / name).write_bytes(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
