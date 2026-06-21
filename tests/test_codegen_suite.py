"""codegen: 批次套件按账号切换登录."""
from __future__ import annotations

from pathlib import Path

from core.codegen import _extract_login_username, generate_suite_script


def test_extract_login_username(tmp_path: Path):
    script = tmp_path / "playwright_x.py"
    script.write_text(
        "def login(page):\n"
        "    page.get_by_placeholder('请输入手机号').fill('18810812516')\n",
        encoding="utf-8",
    )
    assert _extract_login_username(script) == "18810812516"


def test_generate_suite_relogin_on_role_change(tmp_path: Path):
    root = tmp_path
    batch = root / "batch"
    for cid, user in (("case_a", "111"), ("case_b", "222")):
        d = batch / cid
        d.mkdir(parents=True)
        (d / f"playwright_{cid}.py").write_text(
            f"def login(page):\n"
            f"    page.get_by_placeholder('x').fill('{user}')\n"
            f"def run_steps(page):\n"
            f"    pass\n",
            encoding="utf-8",
        )
    out = generate_suite_script(batch, ["case_a", "case_b"], project_root=root)
    assert out is not None
    text = out.read_text(encoding="utf-8")
    assert "_run_one_case" in text
    assert "✅ 通过" in text
    assert "批次套件汇总" in text
