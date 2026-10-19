"""外置环境工程校核规则集；项目/课程规则不写死进 MCP。"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cad_mcp.core.contracts import (
    RuleDefinition,
    reject_excessive_nested_content,
    reject_non_finite,
)
from cad_mcp.dwt.contracts import read_utf8_json_object


class RuleSet(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )

    schema_version: Literal["1.0"] = "1.0"
    rule_set_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    source_documents: List[str] = Field(default_factory=list, max_length=256)
    rules: List[RuleDefinition] = Field(default_factory=list, max_length=2_000)
    metadata: Dict[str, Any] = Field(default_factory=dict, max_length=256)

    @model_validator(mode="after")
    def reject_nested_non_finite_values(self):
        payload = self.model_dump(mode="python")
        reject_non_finite(payload, "规则集")
        reject_excessive_nested_content(payload)
        return self


def load_rule_set(path: str | Path) -> tuple[RuleSet, Dict[str, Any]]:
    rule_path, raw, data = read_utf8_json_object(path, label="规则集")
    rule_set = RuleSet.model_validate(data)
    return rule_set, {
        "path": str(rule_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "rule_count": len(rule_set.rules),
    }
