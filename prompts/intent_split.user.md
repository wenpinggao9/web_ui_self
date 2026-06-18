待拆分动作列表 (index 从 0 开始, 共 {{action_count}} 条):
{{action_list}}

请对每一条动作独立判断是否需要拆分.
- 标记 [跳过拆分] 的条目: split=false, steps 只保留与输入一致的一条动作.
- 其余条目按拆分规则处理.

严格只输出 {"items": [{"index": 0, "split": true/false, "steps": [...]}, ...]} JSON.
items 必须覆盖上表中的每一个 index, 且各 index 仅出现一次.
