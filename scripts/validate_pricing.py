#!/usr/bin/env python3
"""校验 pricing/model_ratios.json 的结构、模型齐全性与数值合法性。

用法:
    python scripts/validate_pricing.py [--path pricing/model_ratios.json]

退出码:
    0  全部通过
    1  校验失败
    2  运行错误（文件不存在 / JSON 损坏 / 参数错误）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

# Windows 控制台可能是 GBK；强制 UTF-8 输出，避免中文乱码（文件写入已显式 UTF-8）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 业务上必须存在的视频（按次 model_price）与图像（按次 model_price）模型
REQUIRED_PER_CALL_MODELS: List[str] = [
    "doubao-seedance-1-0-pro-250528",
    "doubao-seedance-1-0-lite-t2v",
    "doubao-seedance-1-0-lite-i2v",
    "doubao-seedance-1-5-pro-251215",
    "doubao-seedream-4-0-250828",
]

# 必须存在的文本（token 计费）模型，缺失时也会报错，避免上线时按 unknown 默认倍率计费
REQUIRED_TEXT_MODELS: List[str] = [
    "gpt-4o-mini",
    "gpt-4o",
    "deepseek-chat",
    "deepseek-reasoner",
]


def _check_positive_float_map(name: str, data: Dict) -> List[str]:
    errors: List[str] = []
    if not isinstance(data, dict):
        return [f"{name} 必须是对象（map）"]
    if not data:
        return [f"{name} 不能为空"]
    for model, value in data.items():
        if not isinstance(model, str) or not model.strip():
            errors.append(f"{name}.{model!r} 模型名必须是非空字符串")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{name}.{model} 数值类型非法: {value!r}")
            continue
        if value <= 0:
            errors.append(f"{name}.{model} 必须为正数，当前为 {value!r}")
    return errors


def validate(data: Dict) -> List[str]:
    errors: List[str] = []

    for key in ("model_ratio", "completion_ratio", "model_price"):
        if key not in data:
            errors.append(f"缺少顶层字段 {key}")
            continue
        errors.extend(_check_positive_float_map(key, data[key]))

    if isinstance(data.get("model_price"), dict):
        for model in REQUIRED_PER_CALL_MODELS:
            if model not in data["model_price"]:
                errors.append(f"model_price 缺少必需模型 {model}")

    if isinstance(data.get("model_ratio"), dict):
        for model in REQUIRED_TEXT_MODELS:
            if model not in data["model_ratio"]:
                errors.append(f"model_ratio 缺少必需文本模型 {model}")

    # 视频 / 图像模型不能同时出现在按 token 计费的 model_ratio 中（避免双重计费语义混乱）
    if isinstance(data.get("model_ratio"), dict):
        for model in REQUIRED_PER_CALL_MODELS:
            if model in data["model_ratio"]:
                errors.append(f"{model} 已按次计费，不应再出现在 model_ratio 中")

    return errors


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--path",
        default=str(Path(__file__).resolve().parent.parent / "pricing" / "model_ratios.json"),
        help="model_ratios.json 路径",
    )
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"RUN ERROR: 文件不存在: {path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"RUN ERROR: JSON 解析失败: {path}: {exc}", file=sys.stderr)
        return 2

    errors = validate(data)
    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        print(f"共 {len(errors)} 个问题")
        return 1

    print(
        "OK: "
        f"model_ratio={len(data.get('model_ratio', {}))} "
        f"completion_ratio={len(data.get('completion_ratio', {}))} "
        f"model_price={len(data.get('model_price', {}))} "
        "（结构/模型/数值均通过）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
