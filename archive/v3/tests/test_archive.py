import pytest
from src.mutsumi_sync.memory.archive import SlidingArchive


class TestSlidingArchive:
    def test_add_and_total(self):
        arch = SlidingArchive[int](max_size=100.0, min_size=50.0, size_of=lambda x: float(x))
        arch.add(30)
        arch.add(40)
        assert arch.total == 70.0
        assert not arch.needs_recycle

    def test_needs_recycle_triggers(self):
        arch = SlidingArchive[int](max_size=100.0, min_size=50.0, size_of=lambda x: float(x))
        arch.add(60)
        arch.add(60)
        assert arch.total == 120.0
        assert arch.needs_recycle

    def test_find_cutoff(self):
        arch = SlidingArchive[int](max_size=100.0, min_size=50.0, size_of=lambda x: float(x))
        arch.add(30)
        arch.add(30)
        arch.add(30)
        arch.add(30)
        cutoff = arch.find_cutoff()
        assert cutoff == 3

    def test_pop_recyclable(self):
        arch = SlidingArchive[int](max_size=100.0, min_size=50.0, size_of=lambda x: float(x))
        arch.add(40)
        arch.add(40)
        arch.add(40)
        to_recycle, kept = arch.pop_recyclable()
        assert len(to_recycle) >= 1
        assert len(kept) >= 1
        kept_total = sum(arch.size_of(x) for x in kept)
        assert kept_total <= arch.min_size or len(kept) <= 1

    def test_commit(self):
        arch = SlidingArchive[int](max_size=100.0, min_size=50.0, size_of=lambda x: float(x))
        arch.add(50)
        arch.add(50)
        arch.add(50)
        to_recycle, kept = arch.pop_recyclable()
        arch.commit(kept)
        assert len(arch) == len(kept)

    def test_default_size_of_counts_entries(self):
        arch = SlidingArchive[str](max_size=5, min_size=2)
        arch.add("a")
        arch.add("b")
        arch.add("c")
        arch.add("d")
        arch.add("e")
        arch.add("f")
        assert arch.needs_recycle
        to_recycle, kept = arch.pop_recyclable()
        assert len(kept) <= 3

    def test_no_recycle_when_below_max(self):
        arch = SlidingArchive[int](max_size=100.0, min_size=50.0, size_of=lambda x: float(x))
        arch.add(30)
        to_recycle, kept = arch.pop_recyclable()
        assert to_recycle == []
        assert kept == [30]

    def test_extend(self):
        arch = SlidingArchive[int](max_size=100.0, min_size=50.0, size_of=lambda x: float(x))
        arch.extend([30, 40, 50])
        assert arch.needs_recycle


class TestMessageWindowReplace:
    def test_replace_clears_and_sets(self):
        from src.mutsumi_sync.memory.window import MessageWindow
        w = MessageWindow(max_size=100)
        w.add("u1", "hello")
        w.add("u1", "world", is_bot=True)
        assert len(w) == 2

        new_items = [
            {"role": "user", "content": "replaced", "user_id": "u1"}
        ]
        w.replace(new_items)
        assert len(w) == 1
        ctx = w.get_context()
        assert ctx[0]["content"] == "replaced"

    def test_replace_empty(self):
        from src.mutsumi_sync.memory.window import MessageWindow
        w = MessageWindow()
        w.add("u1", "test")
        w.replace([])
        assert len(w) == 0
        assert w.get_context() == []
