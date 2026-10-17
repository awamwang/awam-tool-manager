"""把 tests/e2e/ 变成**包**——这是刻意的，不是随手加的空文件。

原因：tests/ 与 tests/e2e/ 各有一个 conftest.py。在没有 `__init__.py` 时，
pytest 的 prepend 导入模式会把**每个测试文件所在目录**都插到 sys.path 最前，
于是 `tests/e2e` 抢在 `tests` 前面，既有用例里的 `from conftest import
EXPECTED_TOOL_NAMES` 会命中这个目录下的 conftest（它没有那个常量）→
默认全量从 223 passed 掉到 4 failed。

有了 `__init__.py`，pytest 把包的父目录（tests）作为 basedir 插入、模块按
`e2e.test_e2e_startup` 这样的全名导入，`tests/e2e` 不再进 sys.path，两个
conftest 不再同名冲突。
"""
