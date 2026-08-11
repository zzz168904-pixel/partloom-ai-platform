from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cad_agent.feature_reference_registry import (
    PERSISTENT_TOKEN_FORMAT,
    PERSISTENT_TOKEN_PREFIX,
    FeatureReferenceRegistry,
    PersistentReferenceError,
    RegistryNotFoundError,
    RegistryValidationError,
    ResolvedReference,
    SolidWorksPersistentReferenceService,
    build_default_feature_registry,
    coerce_token_bytes,
    decode_persistent_token,
    encode_persistent_token,
    persist_error_flags,
)


class FakeModel:
    def __init__(self, path: str = r"C:\models\plate.SLDPRT") -> None:
        self.GetPathName = path


class FakeBackend:
    def __init__(self, *, token: bytes = b"\x00\x01\x7f\xff", entity=object(), error_code: int = 0) -> None:
        self.token = token
        self.entity = entity
        self.error_code = error_code
        self.capture_calls: list[tuple[object, object]] = []
        self.resolve_calls: list[tuple[object, bytes]] = []

    def capture(self, model_doc, entity) -> bytes:
        self.capture_calls.append((model_doc, entity))
        return self.token

    def resolve(self, model_doc, token: bytes):
        self.resolve_calls.append((model_doc, token))
        return self.entity, self.error_code


def _reference(*, token: str | None = None, state: str = "semantic") -> ResolvedReference:
    return ResolvedReference(
        reference_id="doc_01:feat_01:outer_planar_face",
        document_id="doc_01",
        feature_id="feat_01",
        role="outer_planar_face",
        entity_type="Face",
        body_id="body_01",
        persistent_token=token,
        geometry_signature={"normal": [0, 0, 1]},
        source_skill="base_plate",
        state=state,
    )


def _captured_reference(backend: FakeBackend | None = None) -> tuple[ResolvedReference, FakeBackend]:
    target = backend or FakeBackend()
    service = SolidWorksPersistentReferenceService(target)
    captured = service.capture(
        _reference(),
        FakeModel(),
        entity="face_object",
        document_id="doc_01",
    )
    return captured, target


def test_token_codec_is_binary_safe_and_self_describing() -> None:
    raw = bytes(range(256))

    encoded = encode_persistent_token(raw)

    assert encoded.startswith(PERSISTENT_TOKEN_PREFIX)
    assert decode_persistent_token(encoded) == raw


@pytest.mark.parametrize("value", [b"\x01\x02", bytearray([1, 2]), memoryview(b"\x01\x02"), (1, 2), [1, 2]])
def test_token_byte_coercion_supports_com_safe_array_shapes(value) -> None:
    assert coerce_token_bytes(value) == b"\x01\x02"


@pytest.mark.parametrize("value", [b"", [], [256], [-1], "not-bytes", None])
def test_invalid_token_values_are_rejected(value) -> None:
    with pytest.raises(PersistentReferenceError):
        coerce_token_bytes(value)


def test_capture_creates_resolved_serializable_reference() -> None:
    backend = FakeBackend(token=b"\x10\x20\x30")
    service = SolidWorksPersistentReferenceService(backend)
    model = FakeModel()

    captured = service.capture(
        _reference(),
        model,
        entity="face_object",
        document_id="doc_01",
        geometry_signature={"area_mm2": 6000.0},
        metadata={"semantic_selector": "largest_planar_face"},
    )

    descriptor = captured.metadata["persistent_reference"]
    assert captured.state == "resolved"
    assert decode_persistent_token(captured.persistent_token or "") == b"\x10\x20\x30"
    assert captured.geometry_signature == {"area_mm2": 6000.0}
    assert descriptor["format"] == PERSISTENT_TOKEN_FORMAT
    assert descriptor["byte_length"] == 3
    assert descriptor["model_path"] == model.GetPathName
    assert len(descriptor["sha256"]) == 64
    assert backend.capture_calls == [(model, "face_object")]


def test_resolve_returns_live_entity_without_putting_it_in_registry_data() -> None:
    entity = object()
    captured, backend = _captured_reference(FakeBackend(entity=entity))
    service = SolidWorksPersistentReferenceService(backend)

    result = service.resolve(captured, FakeModel(), document_id="doc_01")

    assert result.success
    assert result.entity is entity
    assert result.registry_state == "resolved"
    assert result.as_dict()["entity_resolved"] is True
    assert "entity" not in result.as_dict()
    assert backend.resolve_calls[0][1] == b"\x00\x01\x7f\xff"


def test_semantic_reference_cannot_reach_backend_until_token_is_captured() -> None:
    backend = FakeBackend()
    result = SolidWorksPersistentReferenceService(backend).resolve(
        _reference(),
        FakeModel(),
        document_id="doc_01",
    )

    assert not result.success
    assert result.reason == "persistent_token_missing"
    assert result.registry_state == "semantic"
    assert result.fallback_required
    assert backend.resolve_calls == []


def test_suppressed_or_deleted_reference_is_marked_stale_without_guessing_geometry() -> None:
    backend = FakeBackend(entity=None, error_code=2 | 4)
    captured, _ = _captured_reference(backend)

    result = SolidWorksPersistentReferenceService(backend).resolve(
        captured,
        FakeModel(),
        document_id="doc_01",
    )

    assert not result.success
    assert result.registry_state == "stale"
    assert result.error_flags == ("suppressed", "deleted")
    assert result.fallback_required
    assert persist_error_flags(1 | 2 | 4) == ("invalid", "suppressed", "deleted")


def test_model_path_and_document_guards_block_cross_model_token_use() -> None:
    captured, backend = _captured_reference()
    service = SolidWorksPersistentReferenceService(backend)

    wrong_document = service.resolve(captured, FakeModel(), document_id="doc_02")
    wrong_path = service.resolve(
        captured,
        FakeModel(r"C:\models\other.SLDPRT"),
        document_id="doc_01",
    )

    assert wrong_document.reason == "document_id_mismatch"
    assert wrong_path.reason == "model_path_mismatch"
    assert backend.resolve_calls == []


def test_resolved_state_requires_a_real_token() -> None:
    with pytest.raises(RegistryValidationError, match="must contain a persistent_token"):
        _reference(state="resolved")


def test_registry_can_replace_semantic_reference_with_captured_token_and_reload(
    tmp_path: Path,
) -> None:
    registry = build_default_feature_registry(ROOT)
    trees = registry.find_feature_trees(part_type="mounting_plate")
    if not trees:
        pytest.skip("Clean public checkout does not bundle a verified proprietary CAD model.")
    document_id = trees[0].document_id
    semantic = registry.get_reference_for_role(
        document_id=document_id,
        feature_id="feat_01",
        role="outer_planar_face",
        entity_type="Face",
    )
    model_path = registry.get_feature_tree(semantic.document_id).model_path
    captured = SolidWorksPersistentReferenceService(FakeBackend()).capture(
        semantic,
        FakeModel(model_path),
        entity="face_object",
        document_id=semantic.document_id,
    )

    registry.register_reference(captured, replace=True)
    resolved = registry.get_reference_for_role(
        document_id=semantic.document_id,
        feature_id="feat_01",
        role="outer_planar_face",
        entity_type="Face",
        require_persistent=True,
    )
    restored = FeatureReferenceRegistry.load(registry.save(tmp_path / "registry.json"))

    assert resolved.state == "resolved"
    assert restored.get_reference(resolved.reference_id) == resolved


def test_registry_rejects_persistent_lookup_for_semantic_reference() -> None:
    registry = build_default_feature_registry(ROOT)
    trees = registry.find_feature_trees(part_type="mounting_plate")
    if not trees:
        pytest.skip("Clean public checkout does not bundle a verified proprietary CAD model.")
    document_id = trees[0].document_id

    with pytest.raises(RegistryNotFoundError, match="has no resolved persistent token"):
        registry.get_reference_for_role(
            document_id=document_id,
            feature_id="feat_01",
            role="outer_planar_face",
            require_persistent=True,
        )
