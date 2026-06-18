"""步骤① 解析器 —— 结构化用例数据结构.

字段对应设计文档输出结构 (英文标识符, 中文语义):
  case_id              用例编号
  priority             优先级
  preconditions        前置条件列表
  steps                操作步骤列表 (前置展开后会把前置步骤插到最前)
  expectations         预期结果列表
  dependencies         用例依赖 (其它用例的 case_id)
  module_path          模块路径 (逐级菜单)
  resources            资源定义 {名称: {source, filename}}
  notes                备注
  precondition_step_count  前置条件展开得到的步骤数 (步骤② 回填)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class CaseResource:
    """资源定义: 上传文件等. source ∈ 资产/上传/本地."""
    name: str
    source: str
    filename: str


@dataclass
class ExecutionBlock:
    """一组「操作 → 预期」: 先执行 operations, 后校验通过后再执行 expectations 对应的断言."""

    operations: list[str] = field(default_factory=list)
    expectations: list[str] = field(default_factory=list)


@dataclass
class ParsedCase:
    """Markdown 解析后的单条用例结构, 供后续所有阶段传递使用."""

    case_id: str
    module_path: list[str] = field(default_factory=list)
    priority: str = ""
    preconditions: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    expectations: list[str] = field(default_factory=list)
    # 解析阶段写入: 「操作步骤与预期结果」交错段落 (步骤->预期 或 步骤+子 bullet 预期)
    execution_blocks: list[ExecutionBlock] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    resources: dict[str, CaseResource] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    source_path: Optional[Path] = None
    # 步骤② 前置展开后回填: 前 N 条步骤来自前置条件解析, 必须保持顺序
    precondition_step_count: int = 0
    # 【多系统扩展】可选字段, 不填时走默认 target 段 (完全向后兼容)
    target_system: str = ""          # 对应 profiles 中的 key
    session_name: str = ""           # 对应 sessions 中的 key
    role: str = ""                   # 对应 session/profile.login.roles 中的 key

    @property
    def name(self) -> str:
        """展示名 = 用例编号 (兼容旧日志接口)."""
        return self.case_id

    @property
    def case_hash(self) -> str:
        """基于用例 ID 和步骤生成短 hash, 用于缓存/输出目录等稳定标识."""
        h = hashlib.md5()
        h.update(self.case_id.encode())
        for s in self.steps:
            h.update(s.encode())
        return h.hexdigest()[:10]
