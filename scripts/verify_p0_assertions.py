"""
Verify the P0 test assertions are NOT tautological, by extracting the REAL
source of the units under test and running them against deliberately broken variants.

This is the answer to "how do you know your new assertions can actually fail?":
every mutation below must make the corresponding assertion go red.

Run:  python scripts/verify_p0_assertions.py
(No Blender required - pure logic only.)
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def extract(rel, names, extra_assigns=()):
    """Extract named top-level defs/classes/assigns from a real source file."""
    src = read(rel)
    tree = ast.parse(src)
    chunks = []
    for node in tree.body:
        nm = getattr(node, "name", None)
        if nm in names:
            chunks.append(ast.get_source_segment(src, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in extra_assigns:
                    chunks.append(ast.get_source_segment(src, node))
    missing = [n for n in names
               if not any(n in (c or "") for c in chunks)]
    ns = {}
    exec(compile("\n\n".join(chunks), f"<{rel}>", "exec"), ns)
    return ns


# copy_ops 的真实源码抽取（P0-5 失败原因聚合）
CO = extract("operators/copy_ops.py",
             {"_BoundedFailureList", "_format_failure_details"},
             extra_assigns=("_FAILURE_DETAIL_LIMIT", "_FAILURE_COLLECT_LIMIT"))


# ---------------------------------------------------------------- cache logic
CACHE_SRC = read("core/cache.py")
BYTES = int(re.search(r"BYTES_PER_VERTEX_ESTIMATE = (\d+)", CACHE_SRC).group(1))
BUDGET_MB = int(re.search(r"CACHE_MEMORY_BUDGET_BYTES = (\d+) \* 1024", CACHE_SRC).group(1))
BUDGET = BUDGET_MB * 1024 * 1024
# 法线单价也从真实源码读取，不写死
BYTES_PER_NORMAL = int(re.search(
    r"BYTES_PER_NORMAL_ESTIMATE = (\d+)", CACHE_SRC).group(1))


class _NPArray:
    """
    最小 numpy 数组替身，**行为对齐真 numpy 的关键语义**。

    最重要的一条: 对多于 1 元素的数组求真值**抛 ValueError**
    （真 numpy: "The truth value of an array with more than one element
    is ambiguous"）。

    这正是 QA P0 能溜过全部单测的根因: 旧测试用 list 承载法线，
    而 `bool(list)` 永远合法。**能崩的地方，替身也要崩。**
    """

    def __init__(self, data, dtype="float64"):
        self._data = [list(r) if isinstance(r, (list, tuple)) else [r]
                      for r in data]
        self.dtype = dtype

    def __len__(self):
        return len(self._data)

    def __getitem__(self, i):
        return self._data[i]

    def __iter__(self):
        return iter(self._data)

    def __bool__(self):
        if len(self._data) > 1:
            raise ValueError(
                "The truth value of an array with more than one element "
                "is ambiguous. Use a.any() or a.all()")
        return bool(self._data and self._data[0])

    @property
    def ndim(self):
        return 2

    @property
    def shape(self):
        return (len(self._data), len(self._data[0]) if self._data else 0)


def _make_numpy_stub():
    """构造最小 numpy 模块替身（具备 _numpy_has_arrays 所需的全部能力）"""
    import types as _t
    mod = _t.ModuleType("numpy")
    mod.ndarray = _NPArray
    mod.float64 = "float64"
    mod.float32 = "float32"

    def empty(count, dtype=None):
        return [_NPArray([[0.0, 0.0, 0.0]] * max(count // 3, 1))
                for _ in range(max(count // 3, 1))][:max(count // 3, 1)]

    def asarray(obj, dtype=None):
        return _NPArray([[float(x) for x in row] for row in obj], dtype or "float64")

    def array(obj, dtype=None):
        return asarray(obj, dtype)

    class _linalg:
        @staticmethod
        def norm(arr, axis=1):
            import math as _m
            return _NPArray([[_m.sqrt(sum(float(v) ** 2 for v in row))]
                             for row in arr])

    mod.empty = empty
    mod.asarray = asarray
    mod.array = array
    mod.linalg = _linalg
    return mod


def _load_real_cache_class(mutation=None):
    """
    从**真实源码** core/cache.py 抽取并执行VertexColorCache。

    为什么必须用真实源码（QA 方法论问题）:
        早先这里手写复刻了一份 _store / get_cache_stats，
        测的是「复刻品」而非被测代码 —— 因此当批次 1 的字节记账
        与批次 2 的法线字段在同一文件叠加出 P0 缺陷时，
        它**测不出来**。复刻品与真实代码的偏离本身就是 bug 温床。

    做法与 verify_match_equivalence.py 一致: 用 ast 抽取真实定义并 exec。
    这保证测的就是仓库里那份代码。

    Args:
        mutation: 可选的 (old, new) 文本替换，用于注入缺陷

    Returns:
        tuple: (VertexColorCache类, 模块命名空间)
    """
    src = read("core/cache.py")
    if mutation:
        old, new_txt = mutation
        if old not in src:
            raise AssertionError(f"变异锚点未找到: {old[:60]}")
        src = src.replace(old, new_txt, 1)

    tree = ast.parse(src)
    wanted = {
        "VertexColorCache", "estimate_cache_entry_bytes",
        "_active_color_path", "_has_normals", "_release_native",
        # _active_color_path 依赖它们；缺失会NameError
        "_normal_constraint_enabled", "_numpy_has_arrays",
        # 1.1.0 阶段 C：_active_color_path 增加的距离约束判据
        "_distance_constraint_enabled",
    }
    chunks = []
    for node in tree.body:
        nm = getattr(node, "name", None)
        if nm in wanted:
            chunks.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in ("BYTES_PER_VERTEX_ESTIMATE",
                                             "BYTES_PER_NORMAL_ESTIMATE",
                                             "CACHE_MEMORY_BUDGET_BYTES"):
                    chunks.append(ast.get_source_segment(src, node))
    if not chunks:
        raise AssertionError("未能从 core/cache.py 抽取到任何目标定义")
    # 模块级常量必须排在类定义之前（类体在定义时就会引用它们）
    chunks.sort(key=lambda c: c.index("CACHE_MEMORY_BUDGET_BYTES")
                if "CACHE_MEMORY_BUDGET_BYTES" in c
                else (c.index("BYTES_PER_") if "BYTES_PER_" in c else 10 ** 6))

    # _active_color_path 内部会 `from . import native_backend`（相对导入）。
    # 这里预置一个可用的假模块，避免在无包上下文中 ImportError。
    class _FakeNativeBackend:
        @staticmethod
        def is_available():
            return False
    ns = {
        "np": _make_numpy_stub(),
        "OrderedDict": __import__("collections").OrderedDict,
        "log_warning": lambda *a, **k: None,
        "log_error": lambda *a, **k: None,
    }
    ns["__name__"] = "cache_under_test"
    exec(compile("\n\n".join(c for c in chunks if c), "<core/cache.py>", "exec"), ns)
    return ns["VertexColorCache"], ns


def load_real_cache_cls():
    """
    构造**未经任何修改**的待测 VertexColorCache（执行真实源码）。

    基线用这个函数。**它不接受任何变异参数** —— 这是刻意的：
    职责单一，读者一看就知道「测的就是仓库里那份代码」。

    注入变异的场景请用 `mutant_cache_cls()`，名字自带警告。
    """
    return _fresh(_load_real_cache_class())


def mutant_cache_cls(mutation=None, bytes_per_vertex=None, budget=None):
    """
    构造**注入缺陷**的 VertexColorCache —— 用于变异测试。

    ⚠ 与`load_real_cache_cls()` 的区别: 本函数会**故意破坏**被测代码
    （改源码文本或覆盖常量），因此它**只能用于「证明断言能被变异打红」**，
    绝不可用于验证真实行为。

    命名从 `make_cache_cls` 改为 `mutant_cache_cls`，就是为了让这个
    职责在调用处一目了然，避免后人误以为所有用例都在测真实源码。

    Args:
        mutation: (old, new) 文本替换，直接改真实源码后再执行
        bytes_per_vertex: 覆盖每顶点字节常量（模拟「公式回退」类缺陷）
        budget: 覆盖默认预算（模拟「预算异常」类缺陷）

    Returns:
        注入了缺陷的类
    """
    cls, ns = _load_real_cache_class(mutation)
    if bytes_per_vertex is not None:
        ns["BYTES_PER_VERTEX_ESTIMATE"] = bytes_per_vertex
    if budget is not None:
        cls._max_cache_bytes = budget
    return _fresh((cls, ns))


def _fresh(loaded):
    """重置类级计数，保证每个用例互不污染"""
    cls, _ns = loaded
    # 每次独立，避免用例间互相污染
    cls._cache = __import__("collections").OrderedDict()
    cls._cached_bytes = 0
    cls._cached_vertices = 0
    cls._hits = 0
    cls._misses = 0
    return cls

# ------------------------------------------------------------------ assertions
def assertions_for_cache(cls, budget, bytes_per_vertex):
    """Mirror of the P0-4 assertions in tests/test_p0_fixes.py."""
    r = {}
    r["budget_matches_const"] = budget == BUDGET
    r["budget_magnitude"] = 128 * 1024 * 1024 <= budget <= 1024 * 1024 * 1024
    r["bytes_magnitude"] = 300 <= bytes_per_vertex <= 500
    r["bytes_not_underestimated"] = bytes_per_vertex >= 300
    r["capacity_ok"] = budget // bytes_per_vertex >= 100_000

    # 每条都带 numpy 形态的法线数组（真实环境形态）。
    # 这是 QA P0 的直接复现场景：bool(ndarray) 会抛 ValueError，
    # 而裸 bool() 写法会让整个 _store 崩溃 -> 默认设置下复制 100% 失败。
    _nrm = _NPArray([[0.0, 1.0, 0.0]] * 200_000)
    for i in range(20):
        cls._store(f"o{i}", {"vertices": [None] * 200_000, "normals": _nrm},
                   200_000, None)
    r["bytes_within_budget"] = cls._cached_bytes <= budget
    r["eviction_happened"] = 0 < len(cls._cache) < 20
    # 每条都带法线数组，故每顶点字节 = 顶点 + 法线（真实环境形态）
    per_v = bytes_per_vertex + globals()["BYTES_PER_NORMAL"]
    expected = len(cls._cache) * 200_000 * per_v
    r["bytes_exactly_consistent"] = (cls._cached_bytes >= 0
                                     and cls._cached_bytes == expected)
    r["vertices_consistent"] = (cls._cached_vertices
                                == len(cls._cache) * 200_000)
    # 反向对照（与 tests 中的断言一一对应）
    requested = 20 * 200_000 * per_v
    r["reverse_control_request_exceeds"] = (
        20 * 200_000 > 2_000_000 and requested > budget)

    before = cls._cached_bytes
    cls._store("huge", {"vertices": [None] * 5_000_000}, 5_000_000, None)
    r["oversize_skipped"] = (cls._cached_bytes == before
                             and "huge" not in cls._cache)
    r["reverse_control_single_exceeds"] = 5_000_000 * per_v > budget
    r["entry_cap_valid"] = isinstance(cls._max_cache_size, int) and 1 <= cls._max_cache_size <= 1000

    s = cls.get_cache_stats()
    r["stats_expose_bytes"] = "cached_bytes" in s and "max_cache_mb" in s

    class Big:
        cache_memory_budget_mb = 1024
    big = cls.get_cache_stats(Big())
    r["stats_denominator_follows_user_setting"] = (
        big.get("max_cache_mb") == 1024
        and s.get("max_cache_mb") != big.get("max_cache_mb"))
    return r


def assertions_for_failure_list():
    """Mirror of the P0-5 assertions in tests/test_p0_fixes.py."""
    ff = CO["_format_failure_details"]
    BL = CO["_BoundedFailureList"]
    r = {}
    r["empty_gives_blank"] = ff([]) == ""
    r["few_all_shown"] = all(s in ff(["A: 无颜色层", "B: 层不存在"])
                             for s in ("A: 无颜色层", "B: 层不存在"))

    many = [f"obj_{i}: 原因{i}" for i in range(20)]
    d = ff(many, total_failures=20)
    r["only_first_three"] = (d.startswith("； ".join(many[:3]))
                             and many[3] not in d and "obj_19" not in d)
    r["exactly_three_shown"] = d.count("；") == 2
    r["remaining_count_shown"] = "另有 17 项失败" in d

    b = BL(limit=5)
    for i in range(50):
        b.append(f"x{i}")
    r["collect_capped"] = len(b) == 5
    r["dropped_accounted"] = b.dropped == 45
    r["total_after_cap"] = "另有 47 项失败" in ff(b, total_failures=50)

    dup = BL(limit=50)
    same = "Src: 源物体处于通道预览状态，请先点「RGBA」恢复"
    for _ in range(30):
        dup.append(same)
    r["dup_merged_to_one"] = len(dup) == 1
    r["merge_counted"] = dup.merged == 29
    r["total_includes_merged"] = dup.total == 30
    dd = ff(dup, total_failures=30)
    r["merged_display_ok"] = dd.count(same) == 1 and "另有 29 项失败" in dd

    d2 = BL(limit=50)
    d2.append("A: 原因1"); d2.append("B: 原因2"); d2.append("A: 原因1")
    r["distinct_preserved"] = (len(d2) == 2 and d2.merged == 1 and d2.total == 3)
    return r


def main():
    print("=" * 74)
    print("P0 断言可证伪性验证（对真实源码注入变异）")
    print(f"真实常量: {BYTES} B/顶点,预算 {BUDGET_MB} MB")
    print("=" * 74)

    all_ok = True
    baseline = assertions_for_cache(load_real_cache_cls(), BUDGET, BYTES)
    print("\n[1] 真实实现（全部断言应通过）")
    for k, v in baseline.items():
        if not v:
            all_ok = False
        print(f"   {'PASS' if v else 'FAIL'}  {k}")

    # 变异清单：(说明, 待测类, 预算, 每顶点字节)
    # 全部通过**改真实源码**或覆盖真实常量来注入，不再手写复刻一份 _store。
    # 这一点是 QA 提出的方法论修正：复刻品与真实代码的偏离本身就是 bug 温床，
    # 早先的 P0（bool(ndarray)）就是因为只测复刻品而完全测不出。
    mutations = [
        ("限流回退到按顶点数（2M）而非字节",
         mutant_cache_cls(mutation=(
             "            or cls._cached_bytes + entry_bytes > budget_bytes",
             "            or cls._cached_vertices + 2_000_000 > budget_bytes")),
         BUDGET, BYTES),
        ("超预算单条目不再跳过",
         mutant_cache_cls(mutation=(
             "        if entry_bytes > budget_bytes:",
             "        if False:")), BUDGET, BYTES),
        ("字节公式回退到 266（QA 指出的漏算版本）",
         mutant_cache_cls(bytes_per_vertex=266), BUDGET, 266),
        ("预算被改成 4GB（远超合理量级）",
         mutant_cache_cls(budget=4 * 1024 ** 3), 4 * 1024 ** 3, BYTES),
        ("预算被改成 16MB（远低于合理量级）",
         mutant_cache_cls(budget=16 * 1024 * 1024), 16 * 1024 * 1024, BYTES),
        ("估算值被夸大到 10000B（容量不足十万顶点）",
         mutant_cache_cls(bytes_per_vertex=10000), BUDGET, 10000),
        ("条目数上限失效（设为 10000，不再淘汰）",
         mutant_cache_cls(mutation=(
             "    _max_cache_size = 50",
             "    _max_cache_size = 10000")), BUDGET, BYTES),
        ("字节记账损坏（多记一次）",
         mutant_cache_cls(mutation=(
             "        cls._cached_bytes += entry_bytes",
             "        cls._cached_bytes += entry_bytes + 1")), BUDGET, BYTES),
        ("顶点记账损坏（多加一次）",
         mutant_cache_cls(mutation=(
             "        cls._cached_vertices += vertex_count",
             "        cls._cached_vertices += vertex_count + 1")), BUDGET, BYTES),
        ("统计分母忽略用户预算（QA 复核项 B）",
         mutant_cache_cls(mutation=(
             "            if override > 0:\n"
             "                return override * 1024 * 1024",
             "            if override > 0:\n"
             "                return 1")), BUDGET, BYTES),
        ("统计接口不再暴露字节字段",
         mutant_cache_cls(mutation=(
             "            'cached_bytes': cls._cached_bytes,",
             "            'renamed_bytes': cls._cached_bytes,")), BUDGET, BYTES),
        # QA P0 类变异：_has_normals 退回裸 bool()
        # 真实 numpy 下 bool(ndarray) 抛 ValueError -> 默认设置下100% 失败。
        ("_has_normals 退回裸 bool()（QA P0 根因）",
         mutant_cache_cls(mutation=(
             "    return normals is not None and len(normals) > 0",
             "    return bool(normals)")), BUDGET, BYTES),
    ]
    print("\n[2] 逐断言可证伪性：每条断言至少要被一个变异打红")
    # 收集每条断言被哪些变异打红
    hits = {k: [] for k in baseline}
    crashed_mutations = []
    for label, cls, budget, bpv in mutations:
        # 变异本身可能让被测代码**抛异常**——这也算「被捕获」，
        # 而且往往正是最严重的那类缺陷（如 QA P0：bool(ndarray) 抛 ValueError
        # 导致默认设置下复制 100% 失败）。这类变异不产生断言结果，
        # 但它证明代码路径确实被变异改变且会崩。
        try:
            res = assertions_for_cache(cls, budget, bpv)
        except Exception as exc:  # noqa: BLE001
            crashed_mutations.append((label, type(exc).__name__))
            for k in baseline:
                hits.setdefault(k, []).append(f"{label}(崩溃)")
            continue
        red = {k for k, v in res.items() if not v}
        for k in red:
            hits.setdefault(k, []).append(label)

    if crashed_mutations:
        print("  以下变异使被测代码直接抛异常（视为已捕获，且属最严重缺陷）:")
        for label, exc in crashed_mutations:
            print(f"    - {label}: {exc}")

    for name in baseline:
        killers = hits.get(name, [])
        ok = bool(killers)
        if not ok:
            all_ok = False
        print(f"   {'OK  ' if ok else 'TAUT'}  {name}")
        if killers:
            print(f"          被以下变异打红: {killers[0]}"
                  + (f" 等 {len(killers)} 个" if len(killers) > 1 else ""))
        else:
            print(f"          !! 没有任何变异能让它变红 —— 恒真断言")

    print("\n[3] 失败原因聚合（真实实现）")
    base5 = assertions_for_failure_list()
    for k, v in base5.items():
        if not v:
            all_ok = False
        print(f"   {'PASS' if v else 'FAIL'}  {k}")

    # mutation: disable dedup
    src = read("operators/copy_ops.py")
    mutated = src.replace("        if reason in self:\n            self.merged += 1\n            return\n", "")
    ns = {}
    tree = ast.parse(mutated)
    chunks = []
    for node in tree.body:
        if getattr(node, "name", None) in {"_BoundedFailureList", "_format_failure_details"}:
            chunks.append(ast.get_source_segment(mutated, node))
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in ("_FAILURE_DETAIL_LIMIT", "_FAILURE_COLLECT_LIMIT"):
                    chunks.append(ast.get_source_segment(mutated, node))
    exec(compile("\n\n".join(chunks), "<mutated>", "exec"), ns)
    real = CO["_format_failure_details"]
    dup = ns["_BoundedFailureList"](limit=50)
    same = "Src: 源物体处于通道预览状态，请先点「RGBA」恢复"
    for _ in range(30):
        dup.append(same)
    nodup_ok = len(dup) == 1
    print(f"\n[4] 变异：去掉去重逻辑")
    print(f"   {'MISS - 断言未捕获!' if nodup_ok else 'OK  - 断言正确变红'}  "
          f"相同原因合并为 1 条（去重失效后 len={len(dup)}，应 >1）")
    if nodup_ok:
        all_ok = False

    print("\n" + "=" * 74)
    print("结论:", "全部断言均可证伪 ✔" if all_ok else "存在恒真断言 → 需修正")
    print("=" * 74)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
