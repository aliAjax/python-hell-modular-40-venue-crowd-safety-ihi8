from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine


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
        if entity["kind"] == "zone" and next_status != entity["status"]:
            voided = self.repository.void_pending_batches(entity_id)
            if voided:
                self.audit.record(
                    entity_id,
                    actor,
                    "void_pending_batches",
                    next_status,
                    next_status,
                    {"voided": voided},
                )
        return updated

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

    def list_review_batches(self):
        batches = self.repository.list_entities(kind="admission_batch")
        return [
            batch
            for batch in batches
            if batch["status"] == "returned"
            and batch["data"].get("exceeded_capacity")
            and not batch["data"].get("reviewed")
        ]

    def return_batch(self, actor, data):
        self.rules._ensure_role(actor, ("operator", "supervisor", "admin"))
        payload = dict(data or {})
        batch_no = str(payload.get("batch_no", "")).strip()
        if not batch_no:
            raise ValidationError("batch_no is required")
        try:
            count = int(payload.get("count"))
        except (TypeError, ValueError):
            raise ValidationError("batch count must be an integer")
        if count <= 0:
            raise ValidationError("batch count must be positive")
        gate_id = payload.get("gate_id")
        zone_id = payload.get("zone_id")
        gate = self.repository.find_entities("gate", "id", gate_id)
        gate = gate[0] if gate else None
        if not gate:
            raise ValidationError("gate does not exist")
        zone = self.repository.find_entities("zone", "id", zone_id)
        zone = zone[0] if zone else None
        if not zone:
            raise ValidationError("zone does not exist")
        if zone_id not in (gate["data"].get("zone_ids") or []):
            raise ValidationError("gate does not serve this zone")
        result = self.repository.return_admission_batch(
            batch_no=batch_no,
            gate_id=gate_id,
            zone_id=zone_id,
            count=count,
            admitted_at=payload.get("admitted_at"),
            actor_id=actor.user_id,
            allowed_statuses=("open", "limited"),
        )
        batch = result["batch"]
        if not result["idempotent"]:
            self.audit.record(
                batch["id"],
                actor,
                "return",
                "pending" if not result["created"] else None,
                "returned",
                {
                    "zone_id": zone_id,
                    "count": count,
                    "excess": batch["data"].get("excess_count", 0),
                },
            )
        return batch

    def review_batch(self, actor, batch_id, data=None):
        self.rules._ensure_role(actor, ("coordinator", "supervisor", "admin"))
        batch = self.repository.get_entity(batch_id)
        if not batch or batch["kind"] != "admission_batch":
            raise NotFoundError("batch not found: " + batch_id)
        if not batch["data"].get("exceeded_capacity"):
            raise ValidationError("batch did not exceed capacity")
        if batch["data"].get("reviewed"):
            raise ConflictError("batch already reviewed")
        updated_data = dict(batch["data"])
        updated_data["reviewed"] = True
        updated_data["reviewed_by"] = actor.user_id
        updated_data["reviewed_at"] = utcnow()
        updated = self.repository.update_entity(
            batch_id, batch["version"], batch["status"], updated_data
        )
        zone_id = batch["data"].get("zone_id")
        if zone_id:
            remaining = [
                item
                for item in self.list_review_batches()
                if item["data"].get("zone_id") == zone_id
            ]
            if not remaining:
                zone = self.repository.get_entity(zone_id)
                if zone and zone["data"].get("over_capacity"):
                    zdata = dict(zone["data"])
                    zdata["over_capacity"] = False
                    self.repository.update_entity(zone_id, zone["version"], zone["status"], zdata)
        self.audit.record(
            batch_id,
            actor,
            "review",
            batch["status"],
            batch["status"],
            {"note": (data or {}).get("note")},
        )
        return updated

    def backfill_occupancy(self, actor):
        self.rules._ensure_role(actor, ("coordinator", "admin"))
        updated = self.repository.backfill_zone_occupancy()
        for item in updated:
            zone = self.repository.get_entity(item["zone_id"])
            status = zone["status"] if zone else "reconciled"
            self.audit.record(
                item["zone_id"],
                actor,
                "backfill_occupancy",
                status,
                status,
                {"occupancy": item["occupancy"]},
            )
        return updated
