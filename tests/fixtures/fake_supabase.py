"""테스트용 Supabase 메모리 대역 — table().select().eq().limit().single().upsert().execute() 만 흉내 낸다."""

from __future__ import annotations


class _Query:
    def __init__(self, store: "FakeSupabase", table: str):
        self.store, self.table_name, self.filters, self._upsert = store, table, [], None

    def select(self, *_a, **_k):
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def limit(self, _n):
        return self

    def single(self):
        return self

    def upsert(self, row, on_conflict=""):
        self._upsert = (row, [c.strip() for c in on_conflict.split(",") if c.strip()])
        return self

    def execute(self):
        rows = self.store.tables.setdefault(self.table_name, [])
        if self._upsert:
            row, keys = self._upsert
            for existing in rows:
                if all(existing.get(k) == row.get(k) for k in keys):
                    existing.update(row)
                    break
            else:
                rows.append(dict(row))
            return type("R", (), {"data": [row]})()
        found = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
        return type("R", (), {"data": found})()


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def table(self, name):
        return _Query(self, name)
