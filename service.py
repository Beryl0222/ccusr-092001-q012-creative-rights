"""文创设计权利转换服务入口。"""

import argparse
import json

from app.api import serve
from app.engine import RightsEngine, load_domain

SERVICE_ID = "creative-rights-conversion"


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


def check_config() -> list[str]:
    """校验领域词表与引擎空状态一致性，返回问题列表（为空即通过）。"""
    problems = []
    try:
        domain = load_domain()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"domain.json 无法读取：{exc}"]
    expected_tables = ["贡献类型", "用途范围", "授权状态"]
    for table in expected_tables:
        if table not in domain:
            problems.append(f"缺少词表：{table}")
    if "商品生产" not in domain.get("用途范围", []):
        problems.append("用途范围缺少 商品生产")
    if "已撤回" not in domain.get("授权状态", []):
        problems.append("授权状态缺少 已撤回")
    engine = RightsEngine(domain=domain)
    report = engine.consistency_report()
    if not report["ok"]:
        problems.append(f"空引擎一致性检查异常：{report['problems']}")
    return problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="文创设计权利转换")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="校验配置后退出")
    parser.add_argument("--data", default=None, help="JSON 持久化文件路径")
    args = parser.parse_args()
    if args.check:
        issues = check_config()
        if issues:
            for issue in issues:
                print(f"检查失败：{issue}")
            raise SystemExit(1)
        print("基础检查通过")
    else:
        serve(port=args.port, data_path=args.data)
