"""Constraint semantic compiler."""

def classify_constraint(text: str) -> str:
    t=text.lower()
    rules={
        'privacy':['隐私','用户数据','上传','泄露','本地'],
        'security':['安全','权限','禁止访问'],
        'interface':['接口','api','协议'],
        'performance':['速度','延迟','性能','实时'],
        'resource':['预算','时间','token','资源'],
        'operation':['禁止','不得','不能','不要']
    }
    for k,vs in rules.items():
        if any(v in t for v in vs): return k
    return 'general'


def predicate_for(text: str):
    t=text.lower()
    if any(x in t for x in ['测试','通过','build','编译']): return 'test_pass'
    if any(x in t for x in ['文件','输出','报告']): return 'artifact_exists'
    if any(x in t for x in ['不能上传','不得上传']): return 'data_local_only'
    if any(x in t for x in ['接口','api']): return 'interface_unchanged'
    return 'condition_check'
