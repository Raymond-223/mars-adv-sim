def detect_conflicts(spec):
    conflicts=[]
    hard=' '.join(x.get('constraint','') for x in spec.get('hard_constraints',[]))
    budget=str(spec.get('budget',{}))
    if ('快速' in hard or '短时间' in budget) and ('高质量' in hard or '质量' in hard):
        conflicts.append('time_quality_conflict')
    return conflicts
