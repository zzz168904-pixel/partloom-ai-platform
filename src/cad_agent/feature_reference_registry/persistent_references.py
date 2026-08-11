from __future__ import annotations

import base64
import copy
import hashlib
import os
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from .models import ResolvedReference


PERSISTENT_TOKEN_PREFIX = "swpr3:"
PERSISTENT_TOKEN_FORMAT = "solidworks.get_persist_reference3.v1"

SW_PERSIST_OK = 0
SW_PERSIST_INVALID = 1
SW_PERSIST_SUPPRESSED = 2
SW_PERSIST_DELETED = 4

_ERROR_FLAGS = (
    (SW_PERSIST_INVALID, "invalid"),
    (SW_PERSIST_SUPPRESSED, "suppressed"),
    (SW_PERSIST_DELETED, "deleted"),
)


class PersistentReferenceError(RuntimeError):
    pass


class PersistentReferenceBackend(Protocol):
    def capture(self, model_doc: Any, entity: Any) -> bytes:
        ...

    def resolve(self, model_doc: Any, token: bytes) -> tuple[Any | None, int]:
        ...


@dataclass(frozen=True)
class PersistentReferenceResolution:
    reference_id: str
    success: bool
    registry_state: str
    reason: str
    message: str = ""
    error_code: int = 0
    error_flags: tuple[str, ...] = ()
    fallback_required: bool = False
    entity: Any | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "success": self.success,
            "registry_state": self.registry_state,
            "reason": self.reason,
            "message": self.message,
            "error_code": self.error_code,
            "error_flags": list(self.error_flags),
            "fallback_required": self.fallback_required,
            "entity_resolved": self.entity is not None,
        }


def encode_persistent_token(value: Any) -> str:
    token = coerce_token_bytes(value)
    encoded = base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
    return f"{PERSISTENT_TOKEN_PREFIX}{encoded}"


def decode_persistent_token(value: str) -> bytes:
    token = str(value or "").strip()
    if not token.startswith(PERSISTENT_TOKEN_PREFIX):
        raise PersistentReferenceError(
            f"Persistent reference token must start with {PERSISTENT_TOKEN_PREFIX!r}."
        )
    payload = token[len(PERSISTENT_TOKEN_PREFIX) :]
    if not payload:
        raise PersistentReferenceError("Persistent reference token payload is empty.")
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.b64decode(payload + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise PersistentReferenceError("Persistent reference token is not valid base64url data.") from exc
    if not decoded:
        raise PersistentReferenceError("Persistent reference token decoded to an empty byte array.")
    return decoded


def coerce_token_bytes(value: Any) -> bytes:
    raw = getattr(value, "value", value)
    if isinstance(raw, bytes):
        token = raw
    elif isinstance(raw, bytearray):
        token = bytes(raw)
    elif isinstance(raw, memoryview):
        token = raw.tobytes()
    elif isinstance(raw, (tuple, list)):
        try:
            token = bytes(raw)
        except (TypeError, ValueError) as exc:
            raise PersistentReferenceError("Persistent reference contains non-byte values.") from exc
    else:
        raise PersistentReferenceError(
            f"Unsupported persistent reference value type: {type(raw).__name__}."
        )
    if not token:
        raise PersistentReferenceError("SolidWorks returned an empty persistent reference.")
    return token


def persist_error_flags(error_code: int) -> tuple[str, ...]:
    code = int(error_code or 0)
    return tuple(name for bit, name in _ERROR_FLAGS if code & bit)


class PyWin32PersistentReferenceBackend:
    """Thin adapter over IModelDocExtension persistent-reference methods."""

    def capture(self, model_doc: Any, entity: Any) -> bytes:
        if entity is None:
            raise PersistentReferenceError("Cannot capture a persistent reference for a null entity.")
        extension = _model_extension(model_doc)
        try:
            raw = extension.GetPersistReference3(entity)
        except Exception as exc:
            raise PersistentReferenceError("SolidWorks GetPersistReference3 failed.") from exc
        return coerce_token_bytes(raw)

    def resolve(self, model_doc: Any, token: bytes) -> tuple[Any | None, int]:
        extension = _model_extension(model_doc)
        try:
            import pythoncom
            from win32com.client import VARIANT
        except ImportError as exc:
            raise PersistentReferenceError(
                "pywin32 is required to resolve SolidWorks persistent references."
            ) from exc

        method = extension.GetObjectByPersistReference3
        attempts: list[Exception] = []
        token_arguments: list[Any] = []
        try:
            token_arguments.append(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, list(token)))
        except Exception as exc:
            attempts.append(exc)
        token_arguments.append(tuple(token))

        for token_argument in token_arguments:
            error = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
            try:
                result = method(token_argument, error)
                return _unpack_resolution_result(result, int(error.value or 0))
            except Exception as exc:
                attempts.append(exc)

        for token_argument in token_arguments:
            try:
                result = method(token_argument)
                return _unpack_resolution_result(result, 0)
            except Exception as exc:
                attempts.append(exc)

        detail = repr(attempts[-1]) if attempts else "unknown COM error"
        raise PersistentReferenceError(
            f"SolidWorks GetObjectByPersistReference3 failed: {detail}"
        ) from (attempts[-1] if attempts else None)


class SolidWorksPersistentReferenceService:
    """Capture and resolve model-bound tokens without storing live COM objects."""

    def __init__(self, backend: PersistentReferenceBackend | None = None) -> None:
        self.backend = backend or PyWin32PersistentReferenceBackend()

    def capture(
        self,
        reference: ResolvedReference,
        model_doc: Any,
        entity: Any,
        *,
        document_id: str | None = None,
        geometry_signature: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ResolvedReference:
        self._check_document_id(reference, document_id)
        token_bytes = self.backend.capture(model_doc, entity)
        token = encode_persistent_token(token_bytes)
        model_path = _model_path(model_doc)
        merged_metadata = copy.deepcopy(reference.metadata)
        merged_metadata.update(copy.deepcopy(metadata or {}))
        merged_metadata["persistent_reference"] = {
            "format": PERSISTENT_TOKEN_FORMAT,
            "api": "IModelDocExtension.GetPersistReference3",
            "encoding": "base64url",
            "byte_length": len(token_bytes),
            "sha256": hashlib.sha256(token_bytes).hexdigest(),
            "model_path": model_path,
        }
        return replace(
            reference,
            persistent_token=token,
            geometry_signature=copy.deepcopy(
                reference.geometry_signature if geometry_signature is None else geometry_signature
            ),
            state="resolved",
            metadata=merged_metadata,
        )

    def resolve(
        self,
        reference: ResolvedReference,
        model_doc: Any,
        *,
        document_id: str | None = None,
        strict_model_path: bool = True,
    ) -> PersistentReferenceResolution:
        try:
            self._check_document_id(reference, document_id)
        except PersistentReferenceError as exc:
            return self._failure(reference, "document_id_mismatch", str(exc))
        if not reference.persistent_token:
            return self._failure(
                reference,
                "persistent_token_missing",
                "Reference is semantic only and has no SolidWorks persistent token.",
            )

        captured_path = str(
            reference.metadata.get("persistent_reference", {}).get("model_path") or ""
        )
        current_path = _model_path(model_doc)
        if strict_model_path and captured_path and current_path and not _same_path(captured_path, current_path):
            return self._failure(
                reference,
                "model_path_mismatch",
                f"Persistent token belongs to {captured_path!r}, not {current_path!r}.",
            )

        try:
            token = decode_persistent_token(reference.persistent_token)
        except PersistentReferenceError as exc:
            return self._failure(reference, "persistent_token_invalid", str(exc))
        expected_digest = str(
            reference.metadata.get("persistent_reference", {}).get("sha256") or ""
        )
        if expected_digest and hashlib.sha256(token).hexdigest() != expected_digest:
            return self._failure(
                reference,
                "persistent_token_digest_mismatch",
                "Persistent token checksum does not match the captured reference.",
            )

        try:
            entity, error_code = self.backend.resolve(model_doc, token)
        except Exception as exc:
            return self._failure(reference, "persistent_backend_error", str(exc))
        flags = persist_error_flags(error_code)
        if error_code != SW_PERSIST_OK or entity is None:
            reason = "persistent_object_not_found" if entity is None and not flags else "persistent_object_stale"
            return PersistentReferenceResolution(
                reference_id=reference.reference_id,
                success=False,
                registry_state="stale",
                reason=reason,
                message="SolidWorks could not resolve the stored persistent reference.",
                error_code=int(error_code or 0),
                error_flags=flags,
                fallback_required=True,
            )
        return PersistentReferenceResolution(
            reference_id=reference.reference_id,
            success=True,
            registry_state="resolved",
            reason="persistent_object_resolved",
            message="SolidWorks resolved the stored persistent reference.",
            entity=entity,
        )

    @staticmethod
    def apply_resolution_state(
        reference: ResolvedReference,
        resolution: PersistentReferenceResolution,
    ) -> ResolvedReference:
        metadata = copy.deepcopy(reference.metadata)
        metadata["last_resolution"] = resolution.as_dict()
        return replace(reference, state=resolution.registry_state, metadata=metadata)

    @staticmethod
    def _check_document_id(reference: ResolvedReference, document_id: str | None) -> None:
        if document_id and reference.document_id != document_id:
            raise PersistentReferenceError(
                f"Reference belongs to document {reference.document_id!r}, not {document_id!r}."
            )

    @staticmethod
    def _failure(
        reference: ResolvedReference,
        reason: str,
        message: str,
    ) -> PersistentReferenceResolution:
        return PersistentReferenceResolution(
            reference_id=reference.reference_id,
            success=False,
            registry_state="stale" if reference.persistent_token else "semantic",
            reason=reason,
            message=message,
            fallback_required=True,
        )


def _model_extension(model_doc: Any) -> Any:
    if model_doc is None:
        raise PersistentReferenceError("A SolidWorks ModelDoc2 object is required.")
    extension = getattr(model_doc, "Extension", None)
    if callable(extension):
        extension = extension()
    if extension is None:
        raise PersistentReferenceError("ModelDoc2.Extension is not available.")
    return extension


def _model_path(model_doc: Any) -> str:
    if model_doc is None:
        return ""
    value = getattr(model_doc, "GetPathName", "")
    if callable(value):
        value = value()
    return str(value or "")


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _unpack_resolution_result(result: Any, error_code: int) -> tuple[Any | None, int]:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        return result[0], int(result[1])
    return result, int(error_code or 0)
