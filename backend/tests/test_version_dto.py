"""API DTO regression tests for rule-generation review artifacts."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.api.suppliers import version_to_dto
from app.models import PriceVersion, VersionStatus


def test_version_dto_exposes_review_only_rule_generation_prompt() -> None:
    prompt = {
        "model": "ultra",
        "prompt_version": "unique-price-rule-v1",
        "prompt_id": "0123456789abcdef",
        "prompt": "bounded source evidence",
        "status": "prepared",
        "requires_manual_review": True,
    }
    version = PriceVersion(
        id=uuid.uuid4(),
        supplier_id=uuid.uuid4(),
        price_file_id=uuid.uuid4(),
        version_no=1,
        status=VersionStatus.complex.value,
        item_count=0,
        rows_accepted=0,
        warnings_count=0,
        errors_count=0,
        created_at=datetime.now(timezone.utc),
        rule_generation=prompt,
    )

    dto = version_to_dto(version, "complex.pdf")

    assert dto.rule_generation == prompt
