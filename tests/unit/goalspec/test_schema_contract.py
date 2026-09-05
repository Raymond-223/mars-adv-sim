from mosaic_omega.goalspec import compile_goal
from mosaic_omega.goalspec.validator import validate_goalspec, validate_goalspec_schema


def test_compiler_output_matches_packaged_schema() -> None:
    spec = compile_goal("任务：生成合法JSON；必须通过测试；禁止上传数据", mode="rule")
    valid, errors = validate_goalspec_schema(spec)
    assert valid, errors


def test_extra_root_field_is_rejected() -> None:
    spec = compile_goal("生成报告", mode="rule")
    spec["extra"] = True
    valid, errors = validate_goalspec(spec)
    assert not valid
    assert any("unexpected top-level" in item for item in errors)
