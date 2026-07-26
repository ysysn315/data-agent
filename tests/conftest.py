"""pytest 公共 fixtures"""
import sqlite3
from pathlib import Path

import pytest

from app.skills.repository import InMemorySkillRepository
from app.skills.service import SkillService

BUILTIN_DIR = Path(__file__).parent.parent / "app" / "skills" / "buildin"


@pytest.fixture
def loguru_capture():
    """捕获 loguru 输出（caplog 只能抓 stdlib logging）"""
    from loguru import logger

    records: list[str] = []
    sink_id = logger.add(lambda msg: records.append(str(msg)), level="DEBUG")
    yield records
    logger.remove(sink_id)


@pytest.fixture
async def skill_service(tmp_path) -> SkillService:
    """加载了内置 skills 的 SkillService（save_dir 指向临时目录）"""
    service = SkillService(
        repository=InMemorySkillRepository(),
        save_dir=tmp_path / "saves",
    )
    await service.load_builtin_skills(BUILTIN_DIR)
    return service


@pytest.fixture
def demo_db(tmp_path) -> str:
    """最小演示 SQLite 库"""
    db_path = tmp_path / "demo.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_state TEXT,
            price REAL
        );
        INSERT INTO orders VALUES (1, 'SP', 100.0), (2, 'RJ', 50.5), (3, 'SP', 30.0);
    """)
    conn.commit()
    conn.close()
    return str(db_path)


def make_skill_md(slug: str, deps: str = "") -> str:
    """构造一个合法的 SKILL.md 内容"""
    return f"""---
name: {slug}
slug: {slug}
description: "测试技能 {slug}"
{deps}---

# {slug}

测试正文。
"""
