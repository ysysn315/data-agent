"""Skills 工具测试：read_skill / run_skill_script / execute_sql"""
import json

from app.agents.tools.sql_tool import create_execute_sql_tool
from app.skills.tools import create_skill_tools


async def test_read_skill_returns_body(skill_service):
    read_skill, _ = create_skill_tools(skill_service)
    body = await read_skill.ainvoke({"slug": "sqlite-query"})
    assert "execute_sql" in body

    missing = await read_skill.ainvoke({"slug": "ghost"})
    assert missing.startswith("技能不存在")


async def test_run_skill_script_executes(skill_service, demo_db):
    _, run_skill_script = create_skill_tools(skill_service)
    output = await run_skill_script.ainvoke({
        "slug": "sqlite-query",
        "script": "query.py",
        "script_args": ["--db", demo_db, "--sql", "SELECT COUNT(*) AS n FROM orders"],
    })
    data = json.loads(output)
    assert data["rows"][0][0] == 3


async def test_run_skill_script_path_traversal_blocked(skill_service):
    _, run_skill_script = create_skill_tools(skill_service)
    output = await run_skill_script.ainvoke({
        "slug": "sqlite-query",
        "script": "../../../etc/passwd",
        "script_args": [],
    })
    assert "非法脚本路径" in output or "脚本不存在" in output


async def test_execute_sql_select_only(demo_db):
    execute_sql = create_execute_sql_tool(demo_db)

    ok = execute_sql.invoke({"sql": "SELECT SUM(price) FROM orders"})
    assert json.loads(ok)["rows"][0][0] == 180.5

    for bad in [
        "DROP TABLE orders",
        "DELETE FROM orders",
        "SELECT 1; DROP TABLE orders",
        "UPDATE orders SET price=0",
    ]:
        result = execute_sql.invoke({"sql": bad})
        assert "拒绝执行" in result, f"未拦截: {bad}"

    # WITH ... INSERT：首词是 WITH 骗过关键词校验，但 AST 校验（第 2 层）会识破
    # 最外层是 Insert 并拦下；引擎级只读（第 3 层）仍是最终兜底。三种拦截文案均可接受。
    result = execute_sql.invoke({"sql": "WITH x AS (SELECT 1) INSERT INTO orders SELECT 4,'MG',1 FROM x"})
    assert "拒绝执行" in result or "SQL 执行失败" in result or "SQL 校验失败" in result
