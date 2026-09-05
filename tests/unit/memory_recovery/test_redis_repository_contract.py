import copy
import fnmatch

from mosaic_omega.memory_recovery.models import MemoryRecord, MemoryType, VerificationStatus
from mosaic_omega.memory_recovery.repository import RedisMemoryRepository


class FakeRedisStore:
    def __init__(self):
        self.json = {}
        self.sets = {}

    def set_json(self, key, value, ttl_seconds=None):
        self.json[key] = copy.deepcopy(dict(value))

    def get_json(self, key):
        value = self.json.get(key)
        return copy.deepcopy(value) if value is not None else None

    def mget_json(self, keys):
        return [self.get_json(k) for k in keys]

    def delete(self, *keys):
        count = 0
        for key in keys:
            if key in self.json:
                del self.json[key]
                count += 1
        return count

    def sadd(self, key, values):
        self.sets.setdefault(key, set()).update(values)

    def srem(self, key, values):
        self.sets.setdefault(key, set()).difference_update(values)

    def smembers(self, key):
        return list(self.sets.get(key, set()))

    def scan_keys(self, pattern):
        return [k for k in self.json if fnmatch.fnmatch(k, pattern)]


def test_redis_repository_indexes_update_delete_and_evidence_query():
    store = FakeRedisStore()
    repo = RedisMemoryRepository(store)
    record = MemoryRecord(
        run_id="r", task_id="t", node_id="n1", memory_type=MemoryType.SEMANTIC,
        content="fact", summary="fact", evidence_refs=["ev1"], tags=["fact"],
    )
    repo.save(record)
    assert repo.get(record.memory_id).content == "fact"
    assert repo.query_by_node("r", "n1")[0].memory_id == record.memory_id
    assert repo.query_by_evidence("ev1")[0].memory_id == record.memory_id

    record.node_id = "n2"
    record.verification_status = VerificationStatus.VERIFIED
    repo.update(record)
    assert repo.query_by_node("r", "n1") == []
    assert repo.query_by_node("r", "n2")[0].memory_id == record.memory_id
    assert repo.query(run_id="r", statuses=[VerificationStatus.VERIFIED])[0].memory_id == record.memory_id

    assert repo.delete(record.memory_id) is True
    assert repo.get(record.memory_id) is None
    assert repo.query_by_evidence("ev1") == []
