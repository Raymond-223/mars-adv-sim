# Shared schemas

`GoalSpec` 的顶层接口冻结为六个字段：

- `main_goal`
- `hard_constraints`
- `soft_preferences`
- `acceptance_conditions`
- `budget`
- `prohibitions`

顶层字段禁止增删改名；嵌套对象允许通过 `schema_version`/代码升级继续扩展。
