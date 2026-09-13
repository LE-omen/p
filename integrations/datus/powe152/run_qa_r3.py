"""POWE-152 round-3 lean runner: frozen run_qa.py + S2 native template tools.

Identical to the frozen lean runner (run_qa.py @ eval HEAD d88f2a4b) when the
plan carries no ``template_store`` block or ``template_store.enabled`` is
false: same four-tool frozen profile, same prompts, same trace contract.

When ``template_store.enabled`` is true the frozen C4 templates (already
projected into this question's home store by the driver) are exposed through
datus 0.4.0's NATIVE reference-template tools — a config/tool-surface
adaptation only; the GenSQL->ExecuteSQL->Output graph, loops, completion and
final-output contract are untouched:

  mode=render : expose search/get/render_reference_template (sandbox render,
                then the model executes the rendered SQL with native
                execute_sql)
  mode=direct : additionally expose execute_reference_template (render +
                single read-only execution inside the tool; the model still
                submits the final SQL through the native envelope)

Every extra tool call counts toward S_agent honestly. The effective tool
manifest for S2 arms is a superset (frozen 4 + reference tools) and is
recorded in effective_config so the delivery ledger can account for it.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import importlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

REPO_SRC = Path(__file__).resolve().parent.parent / "powercontext" / "integrations" / "datus" / "src"
sys.path.insert(0, str(REPO_SRC))

from powercontext_datus.capture import ExecutionTrace, agent_steps, reconcile  # noqa: E402
from powercontext_datus.freeze import IntegrityError, digest_json, snapshot  # noqa: E402
from powercontext_datus import workflow as frozen_workflow  # noqa: E402
from powercontext_datus.workflow import tool_manifest as frozen_tool_manifest  # noqa: E402

TOOL_NAMES_FROZEN = list(frozen_workflow.TOOL_NAMES)
RENDER_TOOLS = ["get_reference_template", "render_reference_template", "search_reference_template"]
DIRECT_TOOLS = RENDER_TOOLS + ["execute_reference_template"]

ANSWER_PROTOCOL = (
    "Return the native GenSQL JSON envelope with sql and output. "
    'The entire output must be a JSON string encoding {"columns": [...], "rows": [[...]]}, '
    "containing the complete answer table. Preserve NULL, duplicate rows and column order. "
    "Do not include prose or unsupported claims in output."
)


def tool_manifest_r3(tools: list, *, extended: set[str] | None = None):
    """Frozen profile when unextended; frozen+reference subset when extended."""
    names = sorted(t.name for t in tools)
    allowed = TOOL_NAMES_FROZEN + sorted(extended or set())
    if names != sorted(allowed):
        raise IntegrityError(f"effective native tool inventory differs from allowed profile: {names}")
    return sorted(({"name": t.name, "description": t.description, "schema": t.params_json_schema}
                   for t in tools), key=lambda t: t["name"])


def tolerant_arguments(arguments: str) -> str:
    _EMPTY_JSON_VALUE = re.compile(r'("\s*:\s*)(\s*[,\}\]])')
    return _EMPTY_JSON_VALUE.sub(lambda m: m.group(1) + "null" + m.group(2), arguments)


def install_argument_sanitizer(nodes) -> None:
    for tool in list(nodes[0].tools):
        original_invoke = tool.on_invoke_tool

        @functools.wraps(original_invoke)
        async def invoke(context, arguments, _orig=original_invoke):
            return await _orig(context, tolerant_arguments(arguments))

        tool.on_invoke_tool = invoke


def inject_required_skills(nodes, names: list[str]) -> None:
    if names:
        nodes[0]._get_required_skills = lambda: list(names)
        nodes[0]._get_available_skills_context = lambda: ""


def configuration(model: dict, api_key: str, qdir: Path, max_turns: int, *, datasource: str | None):
    cls = importlib.import_module("datus.configuration.agent_config").AgentConfig
    cfg = cls(
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
    if datasource:
        # S2 arms only: register the datasource so the native reference-template
        # store (already projected under this question's home) resolves.
        cfg.services.datasources[datasource] = importlib.import_module(
            "datus.configuration.agent_config").DbConfig(
            type="mysql", host="unused", port="3306", username="unused", database=datasource)
        cfg.current_datasource = datasource
    return cfg


def connector_for(db: dict, password: str):
    cls = importlib.import_module("datus_mysql").MySQLConnector
    return cls({"host": db["host"], "port": db["port"], "username": db["username"],
                "password": password, "database": db["name"]})


def build_graph_r3(config, connector, skill_root, names, trace, *, session_id, mode: str | None):
    """Frozen build_graph with the native reference-template tools mounted.

    Everything else — node graph, frozen DB tools, skill manager, no-KB
    profile — is the frozen wiring from powercontext_datus.workflow.
    """
    importlib.import_module("powercontext_datus.native").verify_runtime()
    gen_cls = importlib.import_module("datus.agent.node.gen_sql_agentic_node").GenSQLAgenticNode
    execute_cls = importlib.import_module("datus.agent.node.execute_sql_node").ExecuteSQLNode
    output_cls = importlib.import_module("datus.agent.node.output_node").OutputNode
    workflow_cls = importlib.import_module("datus.agent.workflow").Workflow
    manager = importlib.import_module("powercontext_datus.native").skill_manager_for(skill_root, names)
    trace.observe_skill_loads(manager)

    extended = set(DIRECT_TOOLS if mode == "direct" else RENDER_TOOLS) if mode else set()

    class R3GenSQL(gen_cls):
        def _setup_skill_manager(self):
            self.skill_manager = manager

        def setup_tools(self):
            db_cls = importlib.import_module("datus.tools.func_tool.database").DBFuncTool
            self.db_func_tool = db_cls(connector, read_only=True)
            self.tools = [
                self.db_func_tool.to_function_tool(method)
                for method in (
                    self.db_func_tool.describe_table,
                    self.db_func_tool.execute_sql,
                    self.db_func_tool.list_tables,
                )
            ]
            self._ensure_skill_tools_in_tools()
            if mode:
                rt_cls = importlib.import_module(
                    "datus.tools.func_tool.reference_template_tools").ReferenceTemplateTools
                rt = rt_cls(self.agent_config, sub_agent_name=None, db_func_tool=self.db_func_tool)
                if not rt.has_reference_templates:
                    raise IntegrityError("S2 arm requires a projected reference template store")
                self.tools.extend(rt.available_tools())
            tool_manifest_r3(self.tools, extended=extended)
            self.tools = [trace.wrap_tool(t) for t in self.tools]

        def _ensure_lazy_tools_mounted(self):
            tool_manifest_r3(self.tools, extended=extended)

        def _setup_bash_tool(self):
            self.bash_tool = None

        def _inject_memory_context(self, base_prompt, **kwargs):
            return base_prompt

        def _build_context_rewriter(self, ctx):
            return None

        async def _auto_compact(self):
            return None

        def _compose_run_hooks(self, ctx):
            base_hooks = self._compose_hooks()
            return frozen_workflow.combine_hooks(base_hooks, trace)

        def _ensure_tool_transformers(self):
            if self.agent_config._active_plugins or self.mcp_servers:
                raise IntegrityError("dynamic plugin/MCP configuration is forbidden")
            tool_manifest_r3(self.tools, extended=extended)

    class BoundExecute(execute_cls):
        def _sql_connector(self, database_name=""):
            if database_name and database_name != connector.database_name:
                raise IntegrityError("database routing changed")
            return connector

    class BoundOutput(output_cls):
        def _sql_connector(self, database_name=""):
            return connector

    class R3Workflow(workflow_cls):
        def _init_tools(self):
            self.tools = []

    gen = R3GenSQL("gen_sql", "Generate SQL", "gen_sql", agent_config=config,
                   node_name="gen_sql", execution_mode="workflow", session_id=session_id)
    execute = BoundExecute("execute_sql", "Execute SQL", "execute_sql", agent_config=config)
    output = BoundOutput("output", "Submit native result", "output", agent_config=config)
    wf = R3Workflow("powercontext-development", agent_config=config)
    for node in (gen, execute, output):
        wf.add_node(node)
    return wf, [gen, execute, output]


async def run_graph(workflow, nodes, task, trace, *, common, output_dir, mode):
    models = importlib.import_module("datus.schemas.node_models")
    skills_before = snapshot(Path(nodes[0].skill_manager.config.directories[0]))
    input_cls = importlib.import_module("datus.schemas.gen_sql_agentic_node_models").GenSQLNodeInput
    nodes[0].input = input_cls(user_message="", reference_date=task["current_date"])
    prompt = nodes[0]._get_system_prompt()
    extended = set(DIRECT_TOOLS if mode == "direct" else RENDER_TOOLS) if mode else set()
    tools = tool_manifest_r3(nodes[0].tools, extended=extended)
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
        r3_template_mode=mode or "frozen",
    )
    model = nodes[0].model
    original_stream = model.generate_with_tools_stream

    async def guarded_stream(*args, **kwargs):
        if (
            tool_manifest_r3(kwargs.get("tools", []), extended=extended) != tools
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
    if tool_manifest_r3(nodes[0].tools, extended=extended) != tools:
        raise IntegrityError("tools changed during execution")
    response = nodes[0].result.response
    trace.emit("answer_submitted", gen_sql_response=response, answer=response,
               output=nodes[2].result.model_dump(mode="json"))
    return {"answer": response}


def run_single(payload: dict) -> int:
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
    common = Path(payload["common_path"]).read_text(encoding="utf-8") + "\n\n" + ANSWER_PROTOCOL
    skills_dir = Path(payload["skills_dir"])
    skills_dir.mkdir(parents=True, exist_ok=True)
    store_cfg = payload.get("template_store") or {}
    mode = store_cfg.get("mode") if store_cfg.get("enabled") else None
    with records_path.open("w", encoding="utf-8") as stream:
        try:
            with ExecutionTrace(stream=stream, run_id=payload["run_id"], task_id=f"q{payload['qid']}",
                                attempt_id=str(uuid.uuid4())) as trace:
                try:
                    trace.observe_model_http(model["base_url"])
                except Exception:
                    pass
                config = configuration(model, api_key, qdir, payload["max_turns"],
                                       datasource="birdbench" if mode else None)
                connector = connector_for(db, password)
                try:
                    with connector._conn():
                        pass
                    graph, nodes = build_graph_r3(config, connector, skills_dir, payload["skill_names"], trace,
                                                  session_id=str(uuid.uuid4()), mode=mode)
                    install_argument_sanitizer(nodes)
                    if payload.get("inject_skills"):
                        inject_required_skills(nodes, payload["skill_names"])
                    task = {
                        "task_id": f"q{payload['qid']}",
                        "question": payload["question"],
                        "database": db["name"],
                        "current_date": payload["current_date"],
                    }
                    coro = run_graph(graph, nodes, task, trace, common=common,
                                     output_dir=qdir / "output", mode=mode)
                    answer = asyncio.run(asyncio.wait_for(coro, timeout=payload["timeout_s"]))
                    result["answer"] = answer.get("answer")
                finally:
                    connector.close()
        except SystemExit:
            result["returncode"] = 1
            result["error"] = "SystemExit"
        except BaseException as error:  # noqa: BLE001
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
