from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, assess_admission_batch


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def upload_admission_batch(self, actor, data):
        payload = dict(data or {})
        self.rules.validate_batch_upload(actor, payload, self._lookup)
        batch_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(batch_id):
            raise ConflictError("entity already exists: " + batch_id)
        count = int(payload["count"])
        payload["count"] = count
        recorded_status = payload.get("zone_status")
        recorded_generation = payload.get("zone_generation")

        def decide(zone):
            return assess_admission_batch(zone, count, recorded_status, recorded_generation)

        batch, zone, deduplicated = self.repository.apply_admission_batch(
            batch_id, payload, decide, actor.user_id
        )
        if deduplicated:
            return {"batch": batch, "zone": zone, "deduplicated": True}
        self.audit.record(
            batch["id"], actor, "upload", None, batch["status"], {"kind": "admission_batch"}
        )
        if batch["status"] != "void":
            self.audit.record(
                zone["id"],
                actor,
                "reconcile",
                zone["status"],
                zone["status"],
                {
                    "batch_id": batch["id"],
                    "batch_no": batch["data"].get("batch_no"),
                    "gate_id": batch["data"].get("gate_id"),
                    "count": count,
                    "current_occupancy": zone["data"].get("current_occupancy"),
                    "over_capacity_by": batch["data"].get("over_capacity_by", 0),
                },
            )
        return {"batch": batch, "zone": zone, "deduplicated": False}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
