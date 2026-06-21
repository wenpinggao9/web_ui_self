"""通用 API 运行器 —— 把自然语言前置条件转成 API 调用.

职责:
  1. 从描述文本匹配 API 模板 (通过 keywords)
  2. 从描述中扫描关键词, 对照配置文件中的 enum 规则提取参数
  3. 从描述中提取目标数量 (如"2题"→2, "获取 orderId1 和 orderId2"→2)
  4. 调用 API, 提取返回值存入上下文
  5. 按配置执行重试策略 (如投放失败自动换下一个 TID, 直到达到目标数量)

所有业务特有信息 (枚举值、重试规则、TID 查询等) 都在业务知识配置文件中定义.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .api_client import APIClient
from .profile import ApiTemplate, SystemProfile


def _print_api_preview(preview: dict[str, Any]) -> None:
    """打印即将发出的 API 请求 (api_call / bind_session resolve 共用)."""
    import sys

    name = preview.get("api_name", "?")
    if preview.get("type") == "db":
        sys.stdout.write(f"  ├─ API: {name} [DB查询]\n")
        sys.stdout.flush()
        return
    if preview.get("error"):
        sys.stdout.write(f"  ├─ API: {preview['error']}\n")
        sys.stdout.flush()
        return
    method = preview.get("method", "?")
    url = preview.get("url", "?")
    sys.stdout.write(f"  ├─ API: {name} | {method} {url}\n")
    params = preview.get("params")
    body = preview.get("body")
    if params:
        sys.stdout.write(f"  ├─ params: {params}\n")
    if body:
        sys.stdout.write(f"  ├─ body: {body}\n")
    if not params and not body:
        sys.stdout.write("  ├─ (无 params/body)\n")
    sys.stdout.flush()


class ApiRunner:
    def __init__(self, client: APIClient, profile: SystemProfile) -> None:
        self.client = client
        self.profile = profile
        self.context: dict[str, Any] = {}

    def run_preconditions(self, preconditions: list[str]) -> dict[str, Any]:
        """执行所有前置环节, 返回变量上下文."""
        for line in preconditions:
            line = line.strip()
            if not line:
                continue
            api_name, api_tpl = self._match_api(line)
            if not api_name:
                continue

            # 从描述中提取目标数量 (如"2题"→2, "获取 orderId1 和 orderId2"→2)
            target_count = self._extract_target_count(line)

            # 从描述中提取参数 (对照配置中的 param_rules)
            params = self._extract_params(line, api_tpl)

            # 调用 API (智能重试: 查一批 TID 逐个试, 直到成功 target_count 个)
            results = self._call_until_success(api_name, api_tpl, params, target_count)

            # 提取返回值存入上下文
            var_suffixes = re.findall(r'[a-zA-Z]+(\d+)', line)
            base_key = api_tpl.returns[0].split(".")[-1] if api_tpl.returns else "orderId"
            store_as = self._extract_store_as_name(line)
            api_values = []
            for result in results:
                val = result.get(base_key)
                if val is not None:
                    api_values.append(val)
            for i, val in enumerate(api_values):
                if store_as and len(api_values) == 1:
                    self.context[store_as] = str(val)
                    self.context.setdefault(base_key, str(val))
                elif i < len(var_suffixes):
                    self.context[f"{base_key}{var_suffixes[i]}"] = str(val)
                elif len(api_values) == 1:
                    self.context[base_key] = str(val)
        return self.context

    @staticmethod
    def _extract_store_as_name(line: str) -> Optional[str]:
        """从「记录为变量名」提取目标会话变量 (通用, 不依赖业务 YAML)."""
        m = re.search(r"记录为\s*(\w+)", line)
        return m.group(1) if m else None

    def _extract_target_count(self, line: str) -> int:
        """从描述中提取目标数量."""
        # 只从"获取 orderId1 和 orderId2"模式提取变量名（变量名是字母开头+数字结尾）
        m = re.findall(r'(?<=[a-zA-Z])\d+(?=[^\d]|$)', line)
        # 只取"获取"之后的变量名
        get_pos = line.find('获取')
        if get_pos >= 0:
            after_get = line[get_pos:]
            m = re.findall(r'[a-zA-Z]+(\d+)', after_get)
            if m:
                return len(m)
        # 兜底：从"N题"模式提取
        m = re.search(r'(\d+)\s*题', line)
        if m:
            return int(m.group(1))
        return 1

    def _extract_var_names(self, line: str) -> list[str]:
        """从描述中提取变量名后缀, 只取'获取'后面的字母+数字变量."""
        get_pos = line.find('获取')
        if get_pos < 0:
            return []
        after_get = line[get_pos:]
        # 匹配 "获取 xxx1 和 yyy2" 中的 (xxx, 1), (yyy, 2)
        return re.findall(r'([a-zA-Z]+)(\d+)', after_get)

    def _match_api(self, line: str) -> tuple[Optional[str], Optional[ApiTemplate]]:
        """通过关键词匹配 API 模板."""
        for name, tpl in self.profile.apis.items():
            for keyword in tpl.keywords:
                if keyword in line:
                    return name, tpl
        return None, None

    def _extract_params(self, line: str, api_tpl: ApiTemplate) -> dict[str, Any]:
        """从描述文本中提取参数, 对照配置中的 param_rules."""
        params: dict[str, Any] = {}
        rules = getattr(api_tpl, "param_rules", []) or []

        for rule in rules:
            field = rule.get("field", "")
            enum_map = rule.get("enum", {})
            # 枚举 key 按长度倒序, 先匹配长的 (如"大学化学"优先于"化学")
            sorted_keys = sorted(enum_map.keys(), key=len, reverse=True)
            for key in sorted_keys:
                if key in line:
                    params[field] = enum_map[key]
                    break

        # 从上下文中引用已存在的变量 (如 "orderId1")
        for var_name, var_value in self.context.items():
            if var_name in line and var_value is not None:
                params[var_name] = var_value

        return params

    def _call_until_success(
        self, api_name: str, api_tpl: ApiTemplate, params: dict, target_count: int
    ) -> list[dict[str, Any]]:
        """调用 API, 直到成功 target_count 个.

        next_tid 模式: 查一批 TID 逐个试, 用完后继续查下一批, 直到成功或 DB 无更多数据.
        """
        success_cfg = api_tpl.retry
        on_error = success_cfg.get("on_error", "")
        max_attempts = success_cfg.get("max_attempts", 200)  # 防止无限循环

        success_list: list[dict[str, Any]] = []
        attempt = 0
        all_tids: list[str] = []
        tid_batch_start = 0  # 当前批次的起始索引
        total_tried = 0      # 总尝试次数

        while len(success_list) < target_count and total_tried < max_attempts:
            if on_error == "next_tid":
                # 当前批次用完时, 查下一批
                if not all_tids or tid_batch_start >= len(all_tids):
                    new_tids = self.client._query_db(
                        type('T', (), {'type': 'db'})(), {}, {}
                    ).get("tids", [])
                    if not new_tids:
                        # DB 彻底没有数据了
                        break
                    all_tids = new_tids
                    tid_batch_start = 0

                tid = all_tids[tid_batch_start % len(all_tids)]
                tid_batch_start += 1
                tid_field = success_cfg.get("tid_field", "tid")
                new_params = dict(params)
                if "body" in new_params:
                    new_body = dict(new_params["body"])
                    new_body[tid_field] = tid
                    new_params["body"] = new_body
                else:
                    new_params[tid_field] = tid
                # 打印每次尝试的 TID 和参数
                attempt_num = total_tried + 1
                if attempt_num <= 5 or attempt_num % 10 == 0:
                    body_info = new_params.get("body", new_params)
                    print(f"    尝试 {attempt_num}: tid={tid}, params={body_info}")
            else:
                new_params = params

            if total_tried == 0:
                preview = self.client.preview_request(api_name, new_params, self.context)
                _print_api_preview(preview)

            raw_result = self.client.call(api_name, new_params, self.context)
            err_no = self._get_err_no(raw_result)

            if err_no == 0:
                extracted = self._extract_returns(api_tpl, raw_result)
                success_list.append(extracted)
            total_tried += 1

        return success_list

    @staticmethod
    def _extract_returns(api_tpl: ApiTemplate, result: dict) -> dict[str, Any]:
        """从原始响应中提取返回值."""
        extracted = {}
        for var_name in api_tpl.returns:
            parts = var_name.split(".")
            cur = result
            for p in parts:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    cur = None
                    break
            extracted[var_name.split(".")[-1]] = cur
        return extracted

    @staticmethod
    def _get_err_no(result: dict) -> Optional[int]:
        """提取错误码, 兼容多种字段名."""
        for key in ("errNo", "errno", "err_no", "code", "status", "errorCode"):
            val = result.get(key)
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    continue
        return None
