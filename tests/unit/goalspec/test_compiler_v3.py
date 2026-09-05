from mosaic_omega.goalspec.compiler import compile_goal

def test_enhanced_constraint():
    s=compile_goal('开发系统，不能上传用户数据，必须通过测试')
    assert s['hard_constraints'][0]['type']=='privacy'
    assert s['acceptance_conditions']
