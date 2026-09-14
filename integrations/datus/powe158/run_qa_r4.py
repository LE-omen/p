"""Lean POWE-3 experiment runner (user-authorized test environment).

Runs the pinned native Datus GenSQL -> ExecuteSQL -> Output graph, reusing the
reviewed bridge components (powercontext_datus.capture / workflow.build_graph /
native.skill_manager_for) outside the bubblewrap/admission harness, per the
user's 2026-09-11 authorization (POWE-3 comment 01a0915f).

Must run inside the datus runtime:
  cd <repo>/integrations/datus/runtime
  uv run --frozen python <workdir>/experiment/run_qa.py ...

Secrets come from environment: OPENAI_API_KEY, OPENAI_BASE_URL, DB_PASSWORD.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")  # keep SDK telemetry off the HTTP gate

import functools  # noqa: E402
import re  # noqa: E402

# deepseek-v4-flash-0731 occasionally emits empty unquoted optional-parameter
# values in tool arguments (e.g. '"min_rows": , "max_rows": }'). Repair that
# single pattern before the native/trace JSON parsers see it.
_EMPTY_JSON_VALUE = re.compile(r'("\s*:\s*)(\s*[,\}\]])')


def tolerant_arguments(arguments: str) -> str:
    return _EMPTY_JSON_VALUE.sub(lambda m: m.group(1) + "null" + m.group(2), arguments)


def install_argument_sanitizer(nodes) -> None:
    for tool in list(nodes[0].tools):
        original_invoke = tool.on_invoke_tool

        @functools.wraps(original_invoke)
        async def invoke(context, arguments, _orig=original_invoke):
            return await _orig(context, tolerant_arguments(arguments))

        tool.on_invoke_tool = invoke


def inject_required_skills(nodes, names: list[str]) -> None:
    """Deliver the learned skills as host-injected required-skill content.

    Uses the native `_inject_required_skills` prompt path: full skill bodies
    land in the system prompt at render time, so the agent does not need a
    load_skill retrieval step before producing SQL.
    """
    if names:
        nodes[0]._get_required_skills = lambda: list(names)
        # Skills are already injected as required content; drop the
        # <available_skills> advertisement so the model does not spend a
        # load_skill retrieval step on content it already has.
        nodes[0]._get_available_skills_context = lambda: ""

REPO_SRC = Path(__file__).resolve().parent.parent / "powercontext" / "integrations" / "datus" / "src"
sys.path.insert(0, str(REPO_SRC))

from powercontext_datus.capture import ExecutionTrace, agent_steps, reconcile  # noqa: E402
from powercontext_datus.freeze import IntegrityError, digest_json, snapshot  # noqa: E402
from powercontext_datus.workflow import build_graph, tool_manifest  # noqa: E402

ANSWER_PROTOCOL = (
    "Return the native GenSQL JSON envelope with sql and output. "
    'The entire output must be a JSON string encoding {"columns": [...], "rows": [[...]]}, '
    "containing the complete answer table. Preserve NULL, duplicate rows and column order. "
    "Do not include prose or unsupported claims in output."
)


def configuration(model: dict, api_key: str, qdir: Path, max_turns: int):
    cls = importlib.import_module("datus.configuration.agent_config").AgentConfig
    return cls(
        nodes={},
        home=str(qdir / "home" / ".datus"),
        project_root=str(qdir),
        session_dir=str(qdir / "sessions"),
        plugins_enabled=False,
        active_plugins={},
        config_mutable=False,
        sql_read_only=True,
        bash={"enabled": False},
        filesystem={"strict": True},
        target="evaluation",
        models={
            "evaluation": {
                "type": "openai",
                "model": model["model"],
                "base_url": model["base_url"],
                "temperature": 0,
                "api_key": api_key,
                "save_llm_trace": False,
                "max_retry": 1,
            }
        },
        agentic_nodes={
            "gen_sql": {"model": "evaluation", "skills": "*", "subagents": "", "max_turns": max_turns, "mcp": ""}
        },
    )


def connector_for(db: dict, password: str):
    cls = importlib.import_module("datus_mysql").MySQLConnector
    return cls({"host": db["host"], "port": db["port"], "username": db["username"],
                "password": password, "database": db["name"]})


async def run_graph(workflow, nodes, task, trace, *, common, output_dir):
    """Adapted copy of powercontext_datus.workflow.run_graph with a per-question output_dir."""
    models = importlib.import_module("datus.schemas.node_models")
    skills_before = snapshot(Path(nodes[0].skill_manager.config.directories[0]))
    input_cls = importlib.import_module("datus.schemas.gen_sql_agentic_node_models").GenSQLNodeInput
    nodes[0].input = input_cls(user_message="", reference_date=task["current_date"])
    prompt = nodes[0]._get_system_prompt()
    tools = tool_manifest(nodes[0].tools)
    tools_digest = digest_json(tools)
    effective = {"prompt": prompt, "tools": tools, "skills": skills_before, "common": common}
    trace.emit(
        "effective_config",
        workflow=["gen_sql", "execute_sql", "output"],
        prompt=prompt,
        tools=tools,
        tools_sha256=tools_digest,
        session_id=nodes[0].session_id,
        skills=skills_before,
        embedding={"enabled": False},
        common_sha256=digest_json(common),
        effective_sha256=digest_json(effective),
    )
    model = nodes[0].model
    original_stream = model.generate_with_tools_stream

    async def guarded_stream(*args, **kwargs):
        if (
            tool_manifest(kwargs.get("tools", [])) != tools
            or kwargs.get("mcp_servers")
            or any((kwargs.get("builtin_web_tools") or {}).values())
            or kwargs.get("instruction") != prompt
        ):
            trace.emit("coverage_failure", reason="model_dispatch_drift")
            raise IntegrityError("model dispatch differs from frozen effective configuration")
        async for action in original_stream(*args, **kwargs):
            yield action

    model.generate_with_tools_stream = guarded_stream
    workflow.task = models.SqlTask(
        id=task["task_id"],
        task=task["question"],
        database_name=task["database"],
        output_dir=str(output_dir),
        external_knowledge=common,
        current_date=task["current_date"],
        artifact_profile="benchmark_v1",
    )
    trace.start_question(task["question"])
    # inline execute_nodes
    history = importlib.import_module("datus.schemas.action_history").ActionHistoryManager()
    for node in nodes:
        configured = node.setup_input(workflow)
        if not configured["success"]:
            raise IntegrityError("native workflow input rejected")
        async for action in node.execute_stream(history):
            trace.emit("workflow_action", node=node.type, action=action.model_dump(mode="json"))
        trace.emit("node_result", node=node.type, result=node.result.model_dump(mode="json") if node.result else None)
        if node.result is None or not node.result.success:
            raise IntegrityError("native node failed")
        if not node.update_context(workflow)["success"]:
            raise IntegrityError("native workflow context update failed")
    verify = importlib.import_module("powercontext_datus.freeze").verify_snapshot
    verify(Path(nodes[0].skill_manager.config.directories[0]), skills_before)
    if tool_manifest(nodes[0].tools) != tools:
        raise IntegrityError("tools changed during execution")
    response = nodes[0].result.response
    trace.emit("answer_submitted", gen_sql_response=response, answer=response,
               output=nodes[2].result.model_dump(mode="json"))
    return {"answer": response}


def run_single(payload: dict) -> int:
    from powercontext_datus.native import skill_manager_for  # noqa: F401  (used via build_graph)

    qdir = Path(payload["qdir"])
    qdir.mkdir(parents=True, exist_ok=True)
    records_path = qdir / "trace.jsonl"
    result_path = qdir / "result.json"
    result = {"qid": payload["qid"], "question": payload["question"], "arm": payload["arm"],
              "task_id": f"q{payload['qid']}", "started_at": time.time(), "returncode": 0, "error": None}
    db = payload["db"]
    model = payload["model"]
    api_key = os.environ["OPENAI_API_KEY"]
    password = os.environ["DB_PASSWORD"]
    common = Path(payload["common_path"]).read_text(encoding="utf-8")
    if payload.get("contract_path"):
        # POWE-158 unified numeric-contract delivery: the frozen G1-G5 solving
        # contract is appended to every arm's common content before the answer
        # protocol, so all conditions receive identical normative information.
        common += "\n\n" + Path(payload["contract_path"]).read_text(encoding="utf-8")
    common += "\n\n" + ANSWER_PROTOCOL
    skills_dir = Path(payload["skills_dir"])
    skills_dir.mkdir(parents=True, exist_ok=True)
    with records_path.open("w", encoding="utf-8") as stream:
        try:
            with ExecutionTrace(stream=stream, run_id=payload["run_id"], task_id=f"q{payload['qid']}",
                                attempt_id=str(uuid.uuid4())) as trace:
                try:
                    trace.observe_model_http(model["base_url"])
                except Exception:
                    pass
                config = configuration(model, api_key, qdir, payload["max_turns"])
                connector = connector_for(db, password)
                try:
                    with connector._conn():
                        pass
                    graph, nodes = build_graph(config, connector, skills_dir, payload["skill_names"], trace,
                                               session_id=str(uuid.uuid4()))
                    install_argument_sanitizer(nodes)
                    if payload.get("inject_skills"):
                        inject_required_skills(nodes, payload["skill_names"])
                    task = {
                        "task_id": f"q{payload['qid']}",
                        "question": payload["question"],
                        "database": db["name"],
                        "current_date": payload["current_date"],
                    }
                    coro = run_graph(graph, nodes, task, trace, common=common, output_dir=qdir / "output")
                    answer = asyncio.run(asyncio.wait_for(coro, timeout=payload["timeout_s"]))
                    result["answer"] = answer.get("answer")
                finally:
                    connector.close()
        except SystemExit:
            result["returncode"] = 1
            result["error"] = "SystemExit"
        except BaseException as error:  # noqa: BLE001 - record every failure mode
            result["returncode"] = 1
            result["error"] = f"{type(error).__name__}: {error}"[:500]
    records = []
    with records_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    try:
        summary = reconcile(records)
    except Exception as error:  # noqa: BLE001
        summary = {"steps": None, "trace_complete": False, "trace_issues": [f"reconcile_error:{error}"],
                   "operations": [], "sql_results": []}
    answers = [r for r in records if r["kind"] == "answer_submitted"]
    step_ledger = agent_steps(records)
    result.update({
        "steps": summary["steps"],
        "s_agent": step_ledger["s_agent"],
        "s_agent_issues": step_ledger["s_agent_issues"],
        "step_ledger": step_ledger,
        "trace_complete": summary["trace_complete"],
        "trace_issues": summary["trace_issues"],
        "operations": [{"name": op["name"], "failed": op["failed"]} for op in summary["operations"]],
        "sql_results": summary["sql_results"],
        "output": answers[0]["output"] if answers else None,
        "finished_at": time.time(),
    })
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"q{payload['qid']} done rc={result['returncode']} steps={result['steps']} "
          f"issues={result['trace_issues'][:3]} err={result['error']}", flush=True)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", help="single-question payload JSON file")
    parser.add_argument("--batch", help="batch plan JSON file")
    args = parser.parse_args()
    if args.payload:
        run_single(json.loads(Path(args.payload).read_text(encoding="utf-8")))
        return
    if not args.batch:
        parser.error("need --payload or --batch")
    plan = json.loads(Path(args.batch).read_text(encoding="utf-8"))
    out_root = Path(plan["out_root"])
    out_root.mkdir(parents=True, exist_ok=True)
    concurrency = plan.get("concurrency", 3)
    pending: list[tuple[dict, Path]] = []
    for q in plan["questions"]:
        qdir = out_root / f"q{q['qid']}"
        if (qdir / "result.json").exists():
            print(f"q{q['qid']} already done, skip", flush=True)
            continue
        qdir.mkdir(parents=True, exist_ok=True)
        payload = {**q, "qdir": str(qdir), "run_id": plan["run_id"],
                   "db": plan["db"], "model": plan["model"], "common_path": plan["common_path"],
                   "skills_dir": plan["skills_dir"], "max_turns": plan["max_turns"],
                   "timeout_s": plan["timeout_s"], "current_date": plan["current_date"]}
        pf = qdir / "payload.json"
        pf.write_text(json.dumps(payload), encoding="utf-8")
        pending.append((q, pf))
    running: list[tuple[dict, subprocess.Popen, object]] = []
    failed = 0
    while pending or running:
        while pending and len(running) < concurrency:
            q, pf = pending.pop(0)
            err = open(pf.parent / "stderr.log", "w")
            proc = subprocess.Popen([sys.executable, __file__, "--payload", str(pf)],
                                    stdout=subprocess.DEVNULL, stderr=err)
            running.append((q, proc, err))
            print(f"launched q{q['qid']}", flush=True)
        time.sleep(3)
        still = []
        for q, proc, err in running:
            rc = proc.poll()
            if rc is None:
                still.append((q, proc, err))
            else:
                err.close()
                if rc != 0:
                    failed += 1
                    print(f"q{q['qid']} process rc={rc}", flush=True)
        running = still
    print(f"batch done, failed_processes={failed}", flush=True)


if __name__ == "__main__":
    main()
