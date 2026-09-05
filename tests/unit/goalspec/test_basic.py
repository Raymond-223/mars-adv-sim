import unittest

from mosaic_omega.goalspec import compile_goal
from mosaic_omega.goalspec.validator import REQUIRED_TOP_FIELDS, validate_goalspec


class TestGoalSpecCompiler(unittest.TestCase):
    def test_rule_compile_required_fields(self):
        spec = compile_goal("任务：生成 DAG 可读 JSON。必须包含 main_goal。禁止输出解释。", mode="rule")
        for field in REQUIRED_TOP_FIELDS:
            self.assertIn(field, spec)

    def test_rule_validation(self):
        spec = compile_goal("帮我写火星车阶段汇报，不要说已经上车。", mode="rule")
        valid, errors = validate_goalspec(spec)
        self.assertTrue(valid, errors)


if __name__ == "__main__":
    unittest.main()
