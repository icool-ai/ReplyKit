"""Run regression cases against the customer service bot.

Usage:
  uv run python -m src.regression
  uv run python -m src.regression --cases data/regression_cases.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.chatbot import CustomerServiceBot
from src.config import PROJECT_ROOT, get_settings
from src.context import resolve_search_query
from src.knowledge import search_faq, vectorstore_exists
from src.sensitive_store import (
    get_sensitive_store,
    load_patterns_from_path,
)


def _ensure_sensitive_words_for_regression(settings) -> None:
    """Regression only: if DB empty, load data/sensitive.txt (app startup does not)."""
    store = get_sensitive_store(settings.sensitive_db_path)
    if store.count() > 0:
        return
    seed = PROJECT_ROOT / "data" / "sensitive.txt"
    if not seed.exists():
        print(f"[warn] 敏感词库为空且未找到 {seed}，sensitive_* 用例可能失败")
        return
    patterns = load_patterns_from_path(seed)
    result = store.import_patterns(patterns)
    print(
        f"[setup] 敏感词库为空，已从 {seed.name} 导入 "
        f"{result.imported} 条（跳过 {result.skipped}）"
    )


def _load_cases(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("regression_cases.json 须为数组")
    return raw


def _check_expect(actual: dict, expect: dict) -> list[str]:
    errors: list[str] = []
    if "route" in expect and actual.get("route") != expect["route"]:
        errors.append(f"route 期望={expect['route']} 实际={actual.get('route')}")
    if "strategy" in expect and actual.get("strategy") != expect["strategy"]:
        errors.append(
            f"strategy 期望={expect['strategy']} 实际={actual.get('strategy')}"
        )
    if "strategy_in" in expect:
        allowed = expect["strategy_in"]
        if actual.get("strategy") not in allowed:
            errors.append(
                f"strategy 期望属于 {allowed} 实际={actual.get('strategy')}"
            )
    if "faq_id" in expect and actual.get("faq_id") != expect["faq_id"]:
        errors.append(
            f"faq_id 期望={expect['faq_id']} 实际={actual.get('faq_id')}"
        )
    if "answer_contains" in expect:
        needle = str(expect["answer_contains"])
        if needle not in str(actual.get("answer") or ""):
            errors.append(f"answer 未包含：{needle}")
    return errors


def _run_one_query(
    bot: CustomerServiceBot,
    settings,
    *,
    query: str,
    expect: dict,
    check: str = "full",
    history=None,
    quiet_chat_logs: bool = True,
) -> tuple[dict, list[str]]:
    if check == "route_only":
        if history is not None or bot.last_topic:
            search_query, _method = resolve_search_query(
                bot.llm,
                query,
                history,
                last_topic=bot.last_topic,
            )
        else:
            search_query = query
        _docs, _cands, route = search_faq(
            settings, search_query, k=settings.top_k
        )
        actual = {
            "route": route,
            "strategy": None,
            "faq_id": None,
            "answer": "",
        }
    else:
        if quiet_chat_logs:
            import src.chatbot as chatbot_mod

            original_log = chatbot_mod._log
            chatbot_mod._log = lambda *args, **kwargs: None
            try:
                result = bot.chat_result(query, history=history)
            finally:
                chatbot_mod._log = original_log
        else:
            result = bot.chat_result(query, history=history)
        actual = {
            "route": result.route,
            "strategy": result.strategy,
            "faq_id": result.faq_id,
            "answer": result.answer,
        }
    return actual, _check_expect(actual, expect)


def run_cases(cases_path: Path, quiet_chat_logs: bool = True) -> int:
    settings = get_settings()
    _ensure_sensitive_words_for_regression(settings)
    try:
        if not vectorstore_exists(settings):
            print("向量库不存在，请先启动一次应用或重建知识库。")
            return 2
    except RuntimeError as exc:
        if "already accessed" in str(exc).lower():
            print(
                "无法打开本地向量库：可能 API 服务正在占用 Qdrant。\n"
                "请先在运行 `uv run python main.py` 的终端按 Ctrl+C 停掉服务，再跑回归。"
            )
            return 2
        raise

    cases = _load_cases(cases_path)
    try:
        bot = CustomerServiceBot(settings)
    except RuntimeError as exc:
        if "already accessed" in str(exc).lower():
            print(
                "无法打开本地向量库：请先停止正在运行的客服服务后再跑回归。"
            )
            return 2
        raise

    passed = 0
    failed = 0
    skipped = 0

    print(f"回归用例：{cases_path}  共 {len(cases)} 条\n")

    for case in cases:
        case_id = case.get("id", "?")
        category = case.get("category", "")
        turns = case.get("turns")

        # Multi-turn case: keep session across turns (for auto-handoff etc.).
        if isinstance(turns, list) and turns:
            bot.reset_session()
            turn_errors: list[str] = []
            last_actual: dict = {}
            try:
                for i, turn in enumerate(turns, 1):
                    query = str(turn.get("query") or "").strip()
                    if not query:
                        turn_errors.append(f"turn{i}: 空 query")
                        break
                    expect = turn.get("expect") or {}
                    check = turn.get("check", case.get("check", "full"))
                    history = turn.get("history")
                    actual, errors = _run_one_query(
                        bot,
                        settings,
                        query=query,
                        expect=expect,
                        check=check,
                        history=history,
                        quiet_chat_logs=quiet_chat_logs,
                    )
                    last_actual = actual
                    for err in errors:
                        turn_errors.append(f"turn{i} q={query!r}: {err}")
                if turn_errors:
                    failed += 1
                    print(f"[FAIL] {case_id} ({category})")
                    for err in turn_errors:
                        print(f"       - {err}")
                else:
                    passed += 1
                    detail = f"route={last_actual.get('route')}"
                    if last_actual.get("strategy"):
                        detail += f" strategy={last_actual['strategy']}"
                    print(f"[PASS] {case_id} ({category})  {detail}  turns={len(turns)}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"[FAIL] {case_id} ({category})")
                print(f"       - 异常: {exc}")
            continue

        query = str(case.get("query") or "").strip()
        expect = case.get("expect") or {}
        check = case.get("check", "full")

        if not query:
            print(f"[SKIP] {case_id} 空 query")
            skipped += 1
            continue

        try:
            bot.reset_session()
            if case.get("last_topic"):
                bot.last_topic = str(case["last_topic"]).strip()

            history = case.get("history")
            actual, errors = _run_one_query(
                bot,
                settings,
                query=query,
                expect=expect,
                check=check,
                history=history,
                quiet_chat_logs=quiet_chat_logs,
            )

            if errors:
                failed += 1
                print(f"[FAIL] {case_id} ({category})  q={query!r}")
                for err in errors:
                    print(f"       - {err}")
            else:
                passed += 1
                detail = f"route={actual['route']}"
                if actual.get("strategy"):
                    detail += f" strategy={actual['strategy']}"
                if actual.get("faq_id"):
                    detail += f" faq_id={actual['faq_id']}"
                print(f"[PASS] {case_id} ({category})  {detail}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"[FAIL] {case_id} ({category})  q={query!r}")
            print(f"       - 异常: {exc}")

    total = passed + failed + skipped
    rate = (passed / (passed + failed) * 100) if (passed + failed) else 0.0
    print(
        f"\n结果：通过 {passed} / 失败 {failed} / 跳过 {skipped} / 合计 {total}"
        f"  通过率 {rate:.1f}%"
    )
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReplyKit 回归用例")
    parser.add_argument(
        "--cases",
        default=str(PROJECT_ROOT / "data" / "regression_cases.json"),
        help="用例 JSON 路径",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印每次对话的终端调试日志",
    )
    args = parser.parse_args(argv)
    return run_cases(Path(args.cases), quiet_chat_logs=not args.verbose)


if __name__ == "__main__":
    sys.exit(main())
