"""校验契约 JSON Schema 与其样例 fixture。

判据两条：
1. `docs/schemas/*.schema.json` 自身符合 JSON Schema 2020-12；
2. `docs/fixtures/<schema名>/*.json` 逐个通过校验。

契约的请求体定义普遍宽松（容忍未知扩展字段），根级 anyOf 没有判别力，所以 fixture 文件名
首段必须指定目标 def（如 `HealthResponse.health.json`），定向对 `#/$defs/<DefName>` 校验；
首段不是合法 def 名即报错——否则「fixture 通过」会退化成空断言。

用法：python scripts/validate_contracts.py     返回码：0 全过 / 1 有失败
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = _ROOT / "docs" / "schemas"
FIXTURES_DIR = _ROOT / "docs" / "fixtures"


def _validate(schema_path: Path, fixtures_dir: Path) -> list[str]:
    errors: list[str] = []
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"schema 不是合法 JSON [{schema_path.name}]: {exc}"]

    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:  # noqa: BLE001 - 汇总后统一上报
        return [f"schema 不符合 JSON Schema 2020-12 [{schema_path.name}]: {exc}"]

    defs = schema.get("$defs", {})
    if not fixtures_dir.is_dir():
        return errors
    for fixture in sorted(fixtures_dir.glob("*.json")):
        try:
            instance = json.loads(fixture.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"fixture 非法 JSON [{fixture.name}]: {exc}")
            continue
        def_name = fixture.name.split(".", 1)[0]
        if def_name not in defs:
            errors.append(
                f"fixture 文件名首段不是合法 def [{schema_path.name} ← {fixture.name}]:"
                f" '{def_name}' 不在 $defs（可选：{sorted(defs)}）"
            )
            continue
        sub = {**schema, "anyOf": [{"$ref": f"#/$defs/{def_name}"}]}
        sub.pop("oneOf", None)
        for err in Draft202012Validator(sub).iter_errors(instance):
            errors.append(f"fixture 校验失败 [{schema_path.name} ← {fixture.name}]: {err.message}")
    return errors


def main() -> int:
    schemas = sorted(SCHEMAS_DIR.glob("*.schema.json"))
    if not schemas:
        print(f"[schema] 未找到任何 schema：{SCHEMAS_DIR}")
        return 1
    errors: list[str] = []
    for schema_path in schemas:
        stem = schema_path.name.removesuffix(".schema.json")
        errors.extend(_validate(schema_path, FIXTURES_DIR / stem))
    if errors:
        print("[schema] 校验失败：")
        for msg in errors:
            print(f"  - {msg}")
        return 1
    for schema_path in schemas:
        print(f"[schema] OK：{schema_path.relative_to(_ROOT)} 校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
