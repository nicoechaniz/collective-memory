"""Contract, adversarial, crash-window and real-I/O tests for exchange v1."""

from __future__ import annotations

import base64
import datetime as dt
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mapa"))

from exchange import (
    EXPORT_CATALOG_SCHEMA,
    PUBLICATION_DRAFT_SCHEMA,
    PUBLICATION_EVIDENCE_SCHEMA,
    PUBLICATION_REQUEST_SCHEMA,
    ExchangeCapability,
    ExchangeConfig,
    ExchangeError,
    ExportBoundary,
    PublicationBoundary,
    TrustStore,
    canonical_bytes,
    load_json_bytes,
)

NOW = "2026-08-05T06:00:00.000000Z"
EARLY = "2026-08-01T00:00:00.000000Z"
LATE = "2026-08-10T00:00:00.000000Z"
FIXED_INSTANT = dt.datetime(2026, 8, 5, 6, tzinfo=dt.timezone.utc)


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class InjectedCrash(BaseException):
    pass


class FakeProjectionRunner:
    """Real filesystem/SQLite projections without the heavyweight model stack."""

    def __init__(self, root: Path, data: Path, hook=lambda _stage: None):
        self.root = root
        self.data = data
        self.hook = hook

    def snapshot(self, destination: Path):
        backup = destination / "projection-backup"
        backup.mkdir()
        present = []
        db = self.data / "index.db"
        if db.is_file():
            shutil.copy2(db, backup / "index.db")
            present.append("index.db")
        ui = self.data / "ui"
        if ui.is_symlink() or ui.is_dir():
            shutil.copytree(ui.resolve(), backup / "ui")
            present.append("ui")
        (backup / "snapshot.json").write_text(
            json.dumps({"present": present}, sort_keys=True), encoding="utf-8"
        )
        return {"present": present}

    def restore(self, destination: Path):
        backup = destination / "projection-backup"
        present = set(
            json.loads((backup / "snapshot.json").read_text(encoding="utf-8"))[
                "present"
            ]
        )
        db = self.data / "index.db"
        if "index.db" in present:
            shutil.copy2(backup / "index.db", db)
        elif db.exists():
            db.unlink()
        ui = self.data / "ui"
        if ui.is_symlink():
            ui.unlink()
        elif ui.is_dir():
            shutil.rmtree(ui)
        if "ui" in present:
            restored = self.data / f"ui.restored.{destination.name}"
            if restored.exists():
                shutil.rmtree(restored)
            shutil.copytree(backup / "ui", restored)
            ui.symlink_to(restored)

    def build(self):
        docs = []
        for path in sorted(self.root.rglob("*.md")):
            if ".mapa" in path.parts or path.is_symlink():
                continue
            rel = path.relative_to(self.root).as_posix()
            content = path.read_bytes()
            docs.append((rel, sha(content)))
        db_tmp = self.data / "index.db.tmp"
        if db_tmp.exists():
            db_tmp.unlink()
        con = sqlite3.connect(db_tmp)
        con.execute("CREATE TABLE docs(doc_id TEXT PRIMARY KEY, content_hash TEXT)")
        con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        con.executemany("INSERT INTO docs VALUES (?,?)", docs)
        con.executemany(
            "INSERT INTO meta VALUES (?,?)",
            [("index_generation", str(len(docs) + 1)), ("mode", "fts-only")],
        )
        con.commit()
        con.close()
        os.replace(db_tmp, self.data / "index.db")
        self.hook("index-published")

        generation = (
            f"fake-{len(docs)}-{sha(canonical_bytes([list(row) for row in docs]))[:12]}"
        )
        ui_generation = self.data / f"ui.{generation}"
        if ui_generation.exists():
            shutil.rmtree(ui_generation)
        ui_generation.mkdir()
        ucon = sqlite3.connect(ui_generation / "ui_v2.db")
        ucon.execute("CREATE TABLE docs(doc_id TEXT PRIMARY KEY)")
        ucon.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        ucon.executemany(
            "INSERT INTO docs VALUES (?)", [(doc_id,) for doc_id, _ in docs]
        )
        ucon.executemany(
            "INSERT INTO meta VALUES (?,?)",
            [
                ("generation", json.dumps(generation)),
                ("index_generation", json.dumps(str(len(docs) + 1))),
            ],
        )
        ucon.commit()
        ucon.close()
        link = self.data / "ui"
        tmp = self.data / ".ui.tmp"
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(ui_generation)
        os.replace(tmp, link)
        self.hook("ui-published")

    def verify(self, relative_path: str, content_hash: str):
        con = sqlite3.connect(f"file:{self.data / 'index.db'}?mode=ro", uri=True)
        row = con.execute(
            "SELECT content_hash FROM docs WHERE doc_id=?", (relative_path,)
        ).fetchone()
        generation = dict(con.execute("SELECT k,v FROM meta"))["index_generation"]
        con.close()
        ui = (self.data / "ui").resolve()
        ucon = sqlite3.connect(f"file:{ui / 'ui_v2.db'}?mode=ro", uri=True)
        ui_row = ucon.execute(
            "SELECT 1 FROM docs WHERE doc_id=?", (relative_path,)
        ).fetchone()
        ui_meta = {
            key: json.loads(value)
            for key, value in ucon.execute("SELECT k,v FROM meta")
        }
        ucon.close()
        if (
            row != (content_hash,)
            or ui_row != (1,)
            or ui_meta["index_generation"] != generation
        ):
            raise ExchangeError("effect_truth_discrepancy", "fake projections disagree")
        return {
            "index_generation": generation,
            "ui_generation": ui_meta["generation"],
            "index_content_hash": content_hash,
        }


class Fixture:
    def __init__(self, *, fault_stage=None, self_review_key=False, real=False):
        self.temp = tempfile.TemporaryDirectory(prefix="collective-exchange-test.")
        base = Path(self.temp.name)
        self.root = base / "corpus"
        self.data = base / "data"
        self.root.mkdir()
        self.data.mkdir()
        (self.root / "mapa").mkdir()
        self.config = ExchangeConfig.from_object(
            {
                "schema": "collective-exchange-config/v1",
                "producer_instance": "collective:test",
                "producer_release": "collective:release:test",
                "policy_version": "policy:v1",
                "targets": [
                    {
                        "target_id": "collective:article:alpha",
                        "relative_path": "published/alpha.md",
                    },
                    {
                        "target_id": "collective:article:beta",
                        "relative_path": "published/beta.md",
                    },
                ],
                "index_scope": "total",
            }
        )
        self.reader = ExchangeCapability(
            "cap:reader", "export-reader", ("public",), b"r" * 32
        )
        self.publisher = ExchangeCapability(
            "cap:publisher",
            "reviewed-publisher",
            ("collective:article:alpha", "collective:article:beta"),
            b"p" * 32,
        )
        self.subject_key = Ed25519PrivateKey.generate()
        self.reviewer_key = (
            self.subject_key if self_review_key else Ed25519PrivateKey.generate()
        )
        reviewer_principal = "being:subject" if self_review_key else "human:reviewer"
        self.trust = TrustStore.from_object(
            {
                "schema": "collective-exchange-trust/v1",
                "keys": [
                    self._trust_key(
                        "key:subject",
                        "being:subject",
                        self.subject_key,
                        ["subject-consent"]
                        + (["independent-review"] if self_review_key else []),
                    ),
                    *(
                        []
                        if self_review_key
                        else [
                            self._trust_key(
                                "key:reviewer",
                                reviewer_principal,
                                self.reviewer_key,
                                ["independent-review"],
                            )
                        ]
                    ),
                ],
            }
        )

        def hook(stage):
            if stage == fault_stage:
                raise InjectedCrash(stage)

        runner = None if real else FakeProjectionRunner(self.root, self.data, hook)
        self.publication = PublicationBoundary(
            self.root,
            self.data,
            self.config,
            self.publisher,
            self.trust,
            fault_hook=hook,
            projection_runner=runner,
            clock=lambda: FIXED_INSTANT,
        )

    @staticmethod
    def _trust_key(kid, principal, private, roles):
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
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

    def close(self):
        self.temp.cleanup()

    def draft(
        self,
        *,
        action="publish",
        target="collective:article:alpha",
        predecessor=None,
        title="Alpha",
        body="Reviewed public text.",
        checkpoint=None,
    ):
        return {
            "schema": PUBLICATION_DRAFT_SCHEMA,
            "action": action,
            "requester_id": "operator:matrix",
            "subject_id": "being:subject",
            "target_id": target,
            "source_refs": [{"id": "dm:event:one", "hash": "1" * 64}],
            "source_checkpoint": checkpoint
            or {"id": "dm:checkpoint:one", "hash": "2" * 64},
            "classification": "public",
            "policy_version": "policy:v1",
            "media_type": "text/markdown; charset=utf-8",
            "title": "" if action == "tombstone" else title,
            "body": "" if action == "tombstone" else body,
            "predecessor_receipt_id": predecessor and predecessor["receipt_id"],
            "predecessor_receipt_hash": predecessor and predecessor["receipt_hash"],
        }

    def evidence(self, draft, preview, *, kind, expired=False):
        private = self.subject_key if kind == "consent" else self.reviewer_key
        issuer = (
            "being:subject"
            if kind == "consent" or private is self.subject_key
            else "human:reviewer"
        )
        kid = "key:subject" if private is self.subject_key else "key:reviewer"
        body = {
            "schema": PUBLICATION_EVIDENCE_SCHEMA,
            "kind": kind,
            "evidence_id": f"evidence:{kind}:{preview['preview_hash'][:16]}",
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
            "not_after": "2026-08-02T00:00:00.000000Z" if expired else LATE,
        }
        signature = private.sign(canonical_bytes(body))
        return {
            "schema": PUBLICATION_EVIDENCE_SCHEMA,
            "body": body,
            "signature": {"alg": "Ed25519", "kid": kid, "value": b64u(signature)},
        }

    def request(self, draft, *, key="idem:one", expired=False):
        preview = self.publication.preview(draft)
        return {
            "schema": PUBLICATION_REQUEST_SCHEMA,
            "draft": draft,
            "preview_hash": preview["preview_hash"],
            "idempotency_key": key,
            "consent": self.evidence(draft, preview, kind="consent", expired=expired),
            "review": self.evidence(draft, preview, kind="review", expired=expired),
        }

    def apply(self, request):
        plan = self.publication.plan(request)
        return self.publication.apply(request, plan)


class ExportContractTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.accepted_catalog = self.catalog()
        self.export = ExportBoundary(
            self.fx.root,
            self.fx.data,
            self.fx.config,
            self.fx.reader,
            lambda _scope: self.accepted_catalog,
            clock=lambda: FIXED_INSTANT,
        )
        (self.fx.root / "mapa" / "a.md").write_text("# A\n", encoding="utf-8")
        (self.fx.root / "mapa" / "b.md").write_text("# B\n", encoding="utf-8")

    def tearDown(self):
        self.fx.close()

    def create(self, catalog):
        self.accepted_catalog = catalog
        return self.export.create("public")

    def catalog(self, versions=(1, 1), states=("active", "active")):
        entries = []
        for name, version, state in zip(("a", "b"), versions, states):
            entries.append(
                {
                    "artifact_id": f"artifact:{name}:v{version}",
                    "logical_id": f"logical:{name}",
                    "relative_path": None
                    if state == "tombstone"
                    else f"mapa/{name}.md",
                    "media_type": "text/markdown; charset=utf-8",
                    "authors": [f"author:{name}"],
                    "source_refs": [{"id": f"source:{name}", "hash": (name * 64)[:64]}],
                    "license": "MIT",
                    "consent_scope": "public",
                    "classification": "public",
                    "predecessor_artifact_id": None
                    if version == 1
                    else f"artifact:{name}:v{version - 1}",
                    "state": state,
                }
            )
        return {
            "schema": EXPORT_CATALOG_SCHEMA,
            "policy_version": "policy:v1",
            "scope_id": "public",
            "entries": entries,
        }

    def test_immutable_generation_pagination_retry_successor_and_tombstone(self):
        first = self.create(self.catalog())
        self.assertEqual(self.export.manifest(), first)
        self.assertEqual(self.export.manifest(first["generation_id"]), first)
        self.assertEqual(first, self.create(self.catalog()))
        page1 = self.export.page(first["generation_id"], limit=1)
        page2 = self.export.page(
            first["generation_id"], cursor=page1["next_cursor"], limit=1
        )
        self.assertEqual(
            [a["logical_id"] for a in page1["artifacts"] + page2["artifacts"]],
            ["logical:a", "logical:b"],
        )
        ref = page1["artifacts"][0]["content_ref"]
        self.assertEqual(
            self.export.object_bytes(first["generation_id"], ref), b"# A\n"
        )

        (self.fx.root / "mapa" / "a.md").write_text("# A2\n", encoding="utf-8")
        second = self.create(self.catalog((2, 1)))
        self.assertEqual(self.export.manifest(first["generation_id"]), first)
        self.assertEqual(self.export.manifest(second["generation_id"]), second)
        self.assertEqual(
            second["body"]["predecessor_generation"], first["generation_id"]
        )
        third = self.create(self.catalog((2, 2), ("active", "tombstone")))
        tombstone = third["body"]["artifacts"][1]
        self.assertEqual(tombstone["state"], "tombstone")
        self.assertIsNone(tombstone["content_ref"])

    def test_export_rejects_generation_mix_implicit_removal_fork_tamper_and_symlink(
        self,
    ):
        first = self.create(self.catalog())
        cursor = self.export.page(first["generation_id"], limit=1)["next_cursor"]
        (self.fx.root / "mapa" / "a.md").write_text("# A2\n", encoding="utf-8")
        with self.assertRaisesRegex(ExchangeError, "artifact_collision"):
            self.create(self.catalog())
        second = self.create(self.catalog((2, 1)))
        with self.assertRaisesRegex(ExchangeError, "mixed_generation"):
            self.export.page(second["generation_id"], cursor=cursor)

        removed = self.catalog((2, 1))
        removed["entries"] = removed["entries"][:1]
        with self.assertRaisesRegex(ExchangeError, "implicit_removal"):
            self.create(removed)

        forked = self.catalog((3, 1))
        forked["entries"][0]["predecessor_artifact_id"] = "artifact:a:v1"
        with self.assertRaisesRegex(ExchangeError, "generation_fork"):
            self.create(forked)

        ref = first["body"]["artifacts"][0]["content_ref"]
        digest = ref.removeprefix("sha256:")
        obj = (
            self.export.generations
            / first["generation_id"].replace(":", "_")
            / "objects"
            / digest
        )
        external = Path(self.fx.temp.name) / "external-object"
        external.write_bytes(b"# A\n")
        obj.unlink()
        obj.symlink_to(external)
        with self.assertRaisesRegex(ExchangeError, "content_corrupt"):
            self.export.object_bytes(first["generation_id"], ref)
        obj.unlink()
        obj.write_bytes(b"tampered")
        with self.assertRaisesRegex(ExchangeError, "content_corrupt"):
            self.export.object_bytes(first["generation_id"], ref)

        with self.assertRaisesRegex(ExchangeError, "invalid_generation"):
            self.export.page("a/../../outside")

        link_catalog = self.catalog((2, 1))
        link_catalog["entries"][0]["relative_path"] = "mapa/link.md"
        (self.fx.root / "mapa" / "link.md").symlink_to(self.fx.root / "mapa" / "a.md")
        with self.assertRaisesRegex(ExchangeError, "unsafe_path"):
            self.create(link_catalog)

    def test_direction_capabilities_are_not_interchangeable(self):
        wrong = ExportBoundary(
            self.fx.root,
            self.fx.data,
            self.fx.config,
            self.fx.publisher,
            lambda _scope: self.catalog(),
            clock=lambda: FIXED_INSTANT,
        )
        with self.assertRaisesRegex(ExchangeError, "capability_denied"):
            wrong.create("public")

    def test_catalog_is_closed_and_requires_provenance_and_safe_paths(self):
        missing = self.catalog()
        missing["entries"][0]["authors"] = []
        with self.assertRaisesRegex(ExchangeError, "missing_provenance"):
            self.create(missing)

        unknown = self.catalog()
        unknown["entries"][0]["database"] = "index.db"
        with self.assertRaisesRegex(ExchangeError, "unknown_field"):
            self.create(unknown)

        traversal = self.catalog()
        traversal["entries"][0]["relative_path"] = "../outside.md"
        with self.assertRaisesRegex(ExchangeError, "unsafe_path"):
            self.create(traversal)

        duplicate = self.catalog()
        duplicate["entries"].append(dict(duplicate["entries"][0]))
        with self.assertRaisesRegex(ExchangeError, "duplicate_artifact"):
            self.create(duplicate)

        missing_license = self.catalog()
        missing_license["entries"][0]["license"] = ""
        with self.assertRaisesRegex(ExchangeError, "invalid_field"):
            self.create(missing_license)

        private = self.catalog()
        private["entries"][0]["classification"] = "private"
        with self.assertRaisesRegex(ExchangeError, "scope_violation"):
            self.create(private)

        mismatched_scope = self.catalog()
        mismatched_scope["scope_id"] = "private"
        for entry in mismatched_scope["entries"]:
            entry["consent_scope"] = "private"
            entry["classification"] = "private"
        self.accepted_catalog = mismatched_scope
        with self.assertRaisesRegex(ExchangeError, "scope_mismatch"):
            self.export.create("public")

    def test_export_rejects_oversized_content(self):
        (self.fx.root / "mapa" / "a.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ExchangeError, "artifact_too_large"):
            self.create(self.catalog())

    def test_export_rejects_a_non_symlink_current_pointer(self):
        self.create(self.catalog())
        current = self.export.state_root / "current"
        current.unlink()
        current.write_text("not a generation pointer\n", encoding="utf-8")
        with self.assertRaisesRegex(ExchangeError, "state_corrupt"):
            self.create(self.catalog())

    def test_export_cache_has_a_single_writer(self):
        descriptor = os.open(
            self.export.state_root / "lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ExchangeError, "writer_busy"):
                self.create(self.catalog())
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_export_binds_one_index_and_ui_projection_generation(self):
        FakeProjectionRunner(self.fx.root, self.fx.data).build()
        manifest = self.create(self.catalog())
        projection = manifest["body"]["projection"]
        self.assertIsNotNone(projection["index_generation"])
        self.assertIsNotNone(projection["ui_generation"])

        ui_db = (self.fx.data / "ui").resolve() / "ui_v2.db"
        connection = sqlite3.connect(ui_db)
        connection.execute(
            "UPDATE meta SET v=? WHERE k='index_generation'",
            (json.dumps("foreign-generation"),),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(ExchangeError, "projection_drift"):
            self.create(self.catalog())

    def test_export_cli_uses_operator_catalog_and_public_scope_only(self):
        config_path = Path(self.fx.temp.name) / "exchange-config.json"
        catalog_path = Path(self.fx.temp.name) / "export-catalog.json"
        capability_path = Path(self.fx.temp.name) / "reader-capability.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema": "collective-exchange-config/v1",
                    "producer_instance": "collective:test",
                    "producer_release": "collective:release:test",
                    "policy_version": "policy:v1",
                    "targets": [
                        {
                            "target_id": "collective:article:alpha",
                            "relative_path": "published/alpha.md",
                        }
                    ],
                    "index_scope": "total",
                }
            ),
            encoding="utf-8",
        )
        catalog_path.write_text(json.dumps(self.catalog()), encoding="utf-8")
        capability_path.write_text(
            json.dumps(
                {
                    "schema": "collective-exchange-capability/v1",
                    "capability_id": "cap:reader:cli",
                    "role": "export-reader",
                    "scopes": ["public"],
                    "secret": b64u(b"c" * 32),
                }
            ),
            encoding="utf-8",
        )
        os.chmod(capability_path, 0o600)
        env = dict(os.environ)
        env.update(
            {
                "MAPA_ROOT": str(self.fx.root),
                "MAPA_DATA": str(self.fx.data),
                "MAPA_EXCHANGE_CONFIG": str(config_path),
                "MAPA_EXCHANGE_CATALOG": str(catalog_path),
                "MAPA_EXCHANGE_READER_CAPABILITY": str(capability_path),
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "mapa" / "exchange_cli.py"),
                "export-create",
                "--scope",
                "public",
            ],
            env=env,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["body"]["scope_id"], "public")
        self.assertNotIn("relative_path", result.stdout.decode("utf-8"))


class CapabilityContractTests(unittest.TestCase):
    def test_capability_file_requires_owner_only_regular_file(self):
        with tempfile.TemporaryDirectory(prefix="exchange-capability.") as temp:
            root = Path(temp)
            capability = root / "reader.json"
            capability.write_text(
                json.dumps(
                    {
                        "schema": "collective-exchange-capability/v1",
                        "capability_id": "cap:reader:test",
                        "role": "export-reader",
                        "scopes": ["public"],
                        "secret": b64u(b"x" * 32),
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(capability, 0o644)
            with self.assertRaisesRegex(ExchangeError, "unsafe_capability"):
                ExchangeCapability.load(capability)
            os.chmod(capability, 0o600)
            self.assertEqual(ExchangeCapability.load(capability).role, "export-reader")
            link = root / "reader-link.json"
            link.symlink_to(capability)
            with self.assertRaisesRegex(ExchangeError, "unsafe_capability"):
                ExchangeCapability.load(link)

    def test_json_numbers_and_base64url_are_canonical(self):
        with self.assertRaisesRegex(ExchangeError, "non_canonical_number"):
            load_json_bytes(b'{"n":9007199254740992}')

        fx = Fixture()
        try:
            catalog = {
                "schema": EXPORT_CATALOG_SCHEMA,
                "policy_version": "policy:v1",
                "scope_id": "public",
                "entries": [],
            }
            export = ExportBoundary(
                fx.root,
                fx.data,
                fx.config,
                fx.reader,
                lambda _scope: catalog,
                clock=lambda: FIXED_INSTANT,
            )
            manifest = export.create("public")
            with self.assertRaisesRegex(ExchangeError, "invalid_cursor"):
                export.page(manifest["generation_id"], cursor="!!!!")
        finally:
            fx.close()


class PublicationContractTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def test_preview_is_non_mutating_and_publish_replay_reconcile(self):
        draft = self.fx.draft()
        request = self.fx.request(draft)
        self.assertFalse((self.fx.root / "published" / "alpha.md").exists())
        plan = self.fx.publication.plan(request)
        self.assertFalse((self.fx.root / "published" / "alpha.md").exists())
        receipt = self.fx.publication.apply(request, plan)
        self.assertEqual(receipt, self.fx.publication.apply(request, plan))
        reconciled = self.fx.publication.reconcile(receipt["receipt_id"])
        self.assertEqual(reconciled["effect"], "verified")
        self.assertNotIn("relative_path", canonical_bytes(receipt).decode("utf-8"))

    def test_successor_and_tombstone_are_monotonic(self):
        first = self.fx.apply(self.fx.request(self.fx.draft(), key="idem:first"))
        predecessor = {
            "receipt_id": first["receipt_id"],
            "receipt_hash": first["receipt_hash"],
        }
        successor_draft = self.fx.draft(
            action="successor",
            predecessor=predecessor,
            title="Alpha 2",
            body="Reviewed successor.",
        )
        second = self.fx.apply(self.fx.request(successor_draft, key="idem:second"))
        tombstone_draft = self.fx.draft(
            action="tombstone",
            predecessor={
                "receipt_id": second["receipt_id"],
                "receipt_hash": second["receipt_hash"],
            },
        )
        tombstone = self.fx.apply(
            self.fx.request(tombstone_draft, key="idem:tombstone")
        )
        self.assertEqual(tombstone["body"]["after"]["state"], "tombstone")
        with self.assertRaisesRegex(ExchangeError, "invalid_predecessor"):
            self.fx.publication.preview(successor_draft)

    def test_evidence_mismatch_expiry_self_review_and_final_secret_fail_closed(self):
        draft = self.fx.draft()
        request = self.fx.request(draft)
        request["review"]["body"]["content_hash"] = "f" * 64
        with self.assertRaisesRegex(
            ExchangeError, "evidence_mismatch|invalid_signature"
        ):
            self.fx.publication.plan(request)

        for different_draft in (
            self.fx.draft(body="Different reviewed bytes."),
            self.fx.draft(target="collective:article:beta"),
            self.fx.draft(checkpoint={"id": "checkpoint:other", "hash": "e" * 64}),
        ):
            original = self.fx.request(draft, key="idem:exact-binding")
            different_preview = self.fx.publication.preview(different_draft)
            original["review"] = self.fx.evidence(
                different_draft, different_preview, kind="review"
            )
            with (
                self.subTest(different_draft=different_draft),
                self.assertRaisesRegex(ExchangeError, "evidence_mismatch"),
            ):
                self.fx.publication.plan(original)

        expired = self.fx.request(draft, key="idem:expired", expired=True)
        with self.assertRaisesRegex(ExchangeError, "stale_evidence"):
            self.fx.publication.plan(expired)

        revoked_keys = [
            self.fx._trust_key(
                "key:subject",
                "being:subject",
                self.fx.subject_key,
                ["subject-consent"],
            ),
            self.fx._trust_key(
                "key:reviewer",
                "human:reviewer",
                self.fx.reviewer_key,
                ["independent-review"],
            ),
        ]
        revoked_keys[1]["revoked_at"] = "2026-08-04T00:00:00.000000Z"
        revoked_boundary = PublicationBoundary(
            self.fx.root,
            self.fx.data,
            self.fx.config,
            self.fx.publisher,
            TrustStore.from_object(
                {"schema": "collective-exchange-trust/v1", "keys": revoked_keys}
            ),
            projection_runner=FakeProjectionRunner(self.fx.root, self.fx.data),
            clock=lambda: FIXED_INSTANT,
        )
        valid_request = self.fx.request(draft, key="idem:revoked")
        with self.assertRaisesRegex(ExchangeError, "revoked_key"):
            revoked_boundary.plan(valid_request)

        self_review = Fixture(self_review_key=True)
        try:
            with self.assertRaisesRegex(ExchangeError, "self_review"):
                request = self_review.request(self_review.draft())
                self_review.publication.plan(request)
        finally:
            self_review.close()

        with self.assertRaisesRegex(ExchangeError, "secret_detected"):
            self.fx.publication.preview(self.fx.draft(body="api_key=supersecretvalue"))

        secret_cases = (
            self.fx.draft(title="ghp_" + "a" * 24),
            self.fx.draft(body="[link](https://user:verysecret@example.invalid)"),
        )
        frontmatter = self.fx.draft()
        frontmatter["source_refs"] = [{"id": "sk-" + "a" * 24, "hash": "1" * 64}]
        for secret_draft in (*secret_cases, frontmatter):
            with (
                self.subTest(secret_draft=secret_draft),
                self.assertRaisesRegex(ExchangeError, "secret_detected"),
            ):
                self.fx.publication.preview(secret_draft)

        with self.assertRaisesRegex(ExchangeError, "publication_too_large"):
            self.fx.publication.preview(self.fx.draft(body="x" * (1024 * 1024)))

    def test_idempotency_conflict_target_drift_and_direction_confusion(self):
        request = self.fx.request(self.fx.draft())
        receipt = self.fx.apply(request)
        conflicting_draft = self.fx.draft(
            action="successor", predecessor=receipt, title="Other", body="Different"
        )
        conflicting = self.fx.request(conflicting_draft, key="idem:one")
        with self.assertRaisesRegex(ExchangeError, "idempotency_conflict"):
            self.fx.publication.apply(conflicting, {})

        target = self.fx.root / "published" / "alpha.md"
        target.write_text("manual drift", encoding="utf-8")
        with self.assertRaisesRegex(ExchangeError, "effect_truth_discrepancy"):
            self.fx.publication.reconcile(receipt["receipt_id"])

        wrong = PublicationBoundary(
            self.fx.root,
            self.fx.data,
            self.fx.config,
            self.fx.reader,
            self.fx.trust,
            projection_runner=FakeProjectionRunner(self.fx.root, self.fx.data),
            clock=lambda: FIXED_INSTANT,
        )
        with self.assertRaisesRegex(ExchangeError, "capability_denied"):
            wrong.preview(self.fx.draft(target="collective:article:beta"))
        with self.assertRaisesRegex(ExchangeError, "capability_denied"):
            wrong.recover()
        with self.assertRaisesRegex(ExchangeError, "capability_denied"):
            wrong.reconcile(receipt["receipt_id"])

    def test_untrusted_clock_and_symlink_lock_fail_closed(self):
        request = self.fx.request(self.fx.draft())
        bad_clock = PublicationBoundary(
            self.fx.root,
            self.fx.data,
            self.fx.config,
            self.fx.publisher,
            self.fx.trust,
            projection_runner=FakeProjectionRunner(self.fx.root, self.fx.data),
            clock=lambda: dt.datetime(2026, 8, 5, 6),  # noqa: DTZ001 - negative fixture
        )
        with self.assertRaisesRegex(ExchangeError, "invalid_clock"):
            bad_clock.plan(request)

        transaction_link = self.fx.publication.transactions / "foreign"
        transaction_link.symlink_to(Path(self.fx.temp.name))
        with self.assertRaisesRegex(ExchangeError, "state_corrupt"):
            self.fx.publication.recover()
        transaction_link.unlink()

        lock = self.fx.data / "lock"
        external = Path(self.fx.temp.name) / "foreign-lock"
        external.touch()
        lock.unlink()
        lock.symlink_to(external)
        with self.assertRaisesRegex(ExchangeError, "unsafe_lock"):
            self.fx.publication.preview(self.fx.draft())

    def test_publication_rejects_a_non_symlink_current_pointer(self):
        current = self.fx.publication.state_root / "current"
        current.write_text("not a generation pointer\n", encoding="utf-8")
        with self.assertRaisesRegex(ExchangeError, "state_corrupt"):
            self.fx.publication.preview(self.fx.draft())

    def test_untracked_target_and_unknown_fields_fail(self):
        target = self.fx.root / "published" / "alpha.md"
        target.parent.mkdir()
        target.write_text("untracked", encoding="utf-8")
        with self.assertRaisesRegex(ExchangeError, "target_drift"):
            self.fx.publication.preview(self.fx.draft())
        bad = self.fx.draft(target="collective:article:beta")
        bad["host_path"] = "/tmp/escape"
        with self.assertRaisesRegex(ExchangeError, "unknown_field"):
            self.fx.publication.preview(bad)

    def test_writer_contention_and_projection_failure_leave_all_old(self):
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import fcntl,os,sys; "
                    "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); "
                    "fcntl.flock(fd,fcntl.LOCK_EX); print('locked',flush=True); "
                    "sys.stdin.readline()"
                ),
                str(self.fx.data / "lock"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            with self.assertRaisesRegex(ExchangeError, "writer_busy"):
                self.fx.publication.preview(self.fx.draft())
        finally:
            holder.communicate("\n", timeout=5)
        # The lock file is intentionally durable; a dead holder does not leave
        # authority behind and the next process can proceed.
        self.fx.publication.preview(self.fx.draft())

        class SnapshotFailingRunner(FakeProjectionRunner):
            def snapshot(self, _destination):
                raise OSError("simulated disk full")

        snapshot_boundary = PublicationBoundary(
            self.fx.root,
            self.fx.data,
            self.fx.config,
            self.fx.publisher,
            self.fx.trust,
            projection_runner=SnapshotFailingRunner(self.fx.root, self.fx.data),
            clock=lambda: FIXED_INSTANT,
        )
        snapshot_draft = self.fx.draft()
        snapshot_preview = snapshot_boundary.preview(snapshot_draft)
        snapshot_request = {
            "schema": PUBLICATION_REQUEST_SCHEMA,
            "draft": snapshot_draft,
            "preview_hash": snapshot_preview["preview_hash"],
            "idempotency_key": "idem:snapshot-failure",
            "consent": self.fx.evidence(
                snapshot_draft, snapshot_preview, kind="consent"
            ),
            "review": self.fx.evidence(snapshot_draft, snapshot_preview, kind="review"),
        }
        snapshot_plan = snapshot_boundary.plan(snapshot_request)
        with self.assertRaisesRegex(ExchangeError, "snapshot_failed"):
            snapshot_boundary.apply(snapshot_request, snapshot_plan)
        self.assertFalse(any(snapshot_boundary.transactions.iterdir()))
        self.assertFalse(snapshot_boundary.fence.exists())

        class FailingRunner(FakeProjectionRunner):
            def build(self):
                raise ExchangeError("projection_failed", "injected")

        boundary = PublicationBoundary(
            self.fx.root,
            self.fx.data,
            self.fx.config,
            self.fx.publisher,
            self.fx.trust,
            projection_runner=FailingRunner(self.fx.root, self.fx.data),
            clock=lambda: FIXED_INSTANT,
        )
        draft = self.fx.draft()
        request = boundary.preview(draft)
        signed = {
            "schema": PUBLICATION_REQUEST_SCHEMA,
            "draft": draft,
            "preview_hash": request["preview_hash"],
            "idempotency_key": "idem:projection-failure",
            "consent": self.fx.evidence(draft, request, kind="consent"),
            "review": self.fx.evidence(draft, request, kind="review"),
        }
        plan = boundary.plan(signed)
        with self.assertRaisesRegex(ExchangeError, "projection_failed"):
            boundary.apply(signed, plan)
        self.assertFalse((self.fx.root / "published" / "alpha.md").exists())
        self.assertFalse((self.fx.root / "published").exists())
        self.assertFalse(boundary.fence.exists())


class PublicationCrashTests(unittest.TestCase):
    STAGES = (
        "snapshot-staged",
        "prepared",
        "target-published",
        "index-published",
        "ui-published",
        "projections-published",
        "receipt-staged",
        "state-published",
        "journal-committed",
    )

    def test_every_commit_window_recovers_old_or_new(self):
        for stage in self.STAGES:
            with self.subTest(stage=stage):
                fx = Fixture(fault_stage=stage)
                try:
                    draft = fx.draft()
                    request = fx.request(draft)
                    plan = fx.publication.plan(request)
                    with self.assertRaises(InjectedCrash):
                        fx.publication.apply(request, plan)

                    runner = FakeProjectionRunner(fx.root, fx.data)
                    recovered_boundary = PublicationBoundary(
                        fx.root,
                        fx.data,
                        fx.config,
                        fx.publisher,
                        fx.trust,
                        projection_runner=runner,
                        clock=lambda: FIXED_INSTANT,
                    )
                    outcomes = recovered_boundary.recover()
                    committed = stage in ("state-published", "journal-committed")
                    target = fx.root / "published" / "alpha.md"
                    self.assertEqual(target.exists(), committed)
                    if committed:
                        receipt = recovered_boundary.apply(request, plan)
                        self.assertEqual(receipt["body"]["status"], "committed")
                    else:
                        self.assertTrue(
                            any(item["outcome"] == "rolled-back" for item in outcomes)
                        )
                        fresh_request = fx.request(draft)
                        fresh_plan = recovered_boundary.plan(fresh_request)
                        receipt = recovered_boundary.apply(fresh_request, fresh_plan)
                        self.assertEqual(receipt["body"]["status"], "committed")
                finally:
                    fx.close()


class RealProjectionIntegrationTests(unittest.TestCase):
    def test_real_fts_index_and_atlas_publish_in_isolated_root(self):
        fx = Fixture(real=True)
        try:
            (fx.root / "mapa" / "index.md").write_text(
                "# Existing\n\nUnrelated corpus.\n", encoding="utf-8"
            )
            request = fx.request(
                fx.draft(body="Visible through real search and Atlas.")
            )
            receipt = fx.apply(request)
            con = sqlite3.connect(f"file:{fx.data / 'index.db'}?mode=ro", uri=True)
            row = con.execute(
                "SELECT content_hash FROM docs WHERE doc_id='published/alpha.md'"
            ).fetchone()
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            con.close()
            self.assertEqual(row[0], receipt["body"]["after"]["content_hash"])
            self.assertEqual(integrity, "ok")
            ui = (fx.data / "ui" / "ui_v2.db").resolve()
            ucon = sqlite3.connect(f"file:{ui}?mode=ro", uri=True)
            self.assertEqual(
                ucon.execute(
                    "SELECT count(*) FROM docs WHERE doc_id='published/alpha.md'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(ucon.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            ucon.close()
            self.assertTrue((fx.root / "mapa" / "index.md").exists())

            fence = fx.data / "exchange" / "v1" / "publisher" / "publication.fence.json"
            fence.write_text("{}\n", encoding="utf-8")
            env = dict(os.environ)
            env.update({"MAPA_ROOT": str(fx.root), "MAPA_DATA": str(fx.data)})
            blocked = subprocess.run(
                [sys.executable, str(ROOT / "mapa" / "tier1.py"), "stats"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            fence.unlink()

            index_db = fx.data / "index.db"
            external_index = Path(fx.temp.name) / "external-index.db"
            index_db.replace(external_index)
            index_db.symlink_to(external_index)
            with self.assertRaisesRegex(ExchangeError, "unsafe_projection"):
                fx.publication.reconcile(receipt["receipt_id"])
        finally:
            fx.close()


class PublishedVectorTests(unittest.TestCase):
    def test_schema_accepts_positive_vectors_and_rejects_host_path(self):
        schema = json.loads(
            (ROOT / "schemas" / "exchange" / "v1" / "contracts.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        vector_root = ROOT / "vectors" / "exchange" / "v1"
        positives = (
            "export-catalog.json",
            "export-manifest.json",
            "export-page.json",
            "publication-draft.json",
            "publication-preview.json",
            "publication-consent.json",
            "publication-review.json",
            "publication-request.json",
            "publication-plan.json",
            "publication-receipt.json",
        )
        for name in positives:
            with self.subTest(name=name):
                validator.validate(
                    json.loads((vector_root / name).read_text(encoding="utf-8"))
                )
        with self.assertRaises(ValidationError):
            validator.validate(
                json.loads(
                    (vector_root / "negative-host-path.json").read_text(
                        encoding="utf-8"
                    )
                )
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
