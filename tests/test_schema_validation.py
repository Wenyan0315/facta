"""参数结构层校验验收：语法合法但缺必填/类型错 → 错误字符串（自纠反馈环）。"""

from agent.tools.registry import Tool, ToolRegistry


def _registry(**overrides) -> ToolRegistry:
    registry = ToolRegistry()
    params = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "count": {"type": "integer"},
            "flag": {"type": "boolean"},
        },
        "required": ["path"],
    }
    params.update(overrides)
    registry.register(
        Tool(
            name="demo",
            description="测试工具",
            parameters=params,
            func=lambda **args: f"ok:{args}",
        )
    )
    return registry


def test_missing_required_returns_error_string():
    result = _registry().execute("demo", "{}")
    assert result.startswith("错误：参数校验失败")
    assert "缺少必填参数 path" in result


def test_wrong_type_returns_error_string():
    result = _registry().execute("demo", '{"path": "x", "count": "不是数字"}')
    assert "count 应为整数" in result


def test_bool_not_accepted_as_integer():
    result = _registry().execute("demo", '{"path": "x", "count": true}')
    assert "count 应为整数" in result  # bool 是 int 子类，必须显式排除


def test_valid_args_pass_through():
    result = _registry().execute("demo", '{"path": "x", "count": 3}')
    assert result.startswith("ok:")


def test_unknown_extra_field_is_tolerated():
    # 菜单外多传的字段放行（宽松原则），由工具函数自行决定是否理会
    result = _registry().execute("demo", '{"path": "x", "surprise": [1,2]}')
    assert result.startswith("ok:")


def test_anyof_schema_skips_without_crash():
    # MCP 三方 schema 常见 anyOf 复合写法：没有单一 type → 跳过该字段
    registry = _registry(
        properties={
            "path": {"type": "string"},
            "filters": {"anyOf": [{"type": "object"}, {"type": "null"}]},
        },
    )
    result = registry.execute("demo", '{"path": "x", "filters": {"tag": "php"}}')
    assert result.startswith("ok:")