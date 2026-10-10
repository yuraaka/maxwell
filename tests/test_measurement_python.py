from maxwell.config import MeasurementSettings
from maxwell.measurement_python import cyclic_components, python_measurement
from maxwell.repositories import git


def tracked(source, files):
    for name, text in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git("add", ".", cwd=source)


def test_complexity_nested_functions_and_clone_coverage(source):
    code = "def example(x):\n    if x:\n        return 1\n    return 0\n"
    tracked(
        source,
        {
            "a.py": code,
            "b.py": code,
            "nested.py": "def outer():\n    def inner(x):\n        if x: return 1\n    return inner\n",
        },
    )
    settings = MeasurementSettings(complexity_threshold=1, clone_tokens=5, clone_lines=1)
    factors, status, details = python_measurement(source, settings)
    assert not status
    functions = {
        (item["path"], item["symbol"]): item["complexity"] for item in details["complex_functions"]
    }
    assert functions["nested.py", "outer"] == 1 and functions["nested.py", "inner"] == 2
    assert factors["complexity"].numerator == 3
    assert 0 < factors["duplication"].numerator <= factors["duplication"].denominator
    assert python_measurement(source, settings) == (factors, status, details)


def test_relative_imports_cycles_and_package_exports(source):
    tracked(
        source,
        {
            "src/p/__init__.py": "from .a import f\n",
            "src/p/a.py": "from .b import g\ndef f(): return g()\n",
            "src/p/b.py": "from .a import f\ndef g(): return f()\n",
        },
    )
    factors, status, details = python_measurement(source, MeasurementSettings())
    assert not status
    assert details["cycles"] == [["p.a", "p.b"]]
    assert factors["coupling"].numerator == 2
    assert cyclic_components({"a": {"a"}, "b": set()}) == [["a"]]


def test_parse_failures_limits_and_dynamic_imports_remain_unknown(source):
    tracked(source, {"broken.py": "def (\n"})
    factors, status, _ = python_measurement(source, MeasurementSettings())
    assert not factors and set(status) == {"complexity", "duplication", "coupling"}
    (source / "broken.py").write_text("def f(): return __import__('dynamic')\n")
    factors, status, _ = python_measurement(source, MeasurementSettings(max_tokens=1))
    assert "complexity" in factors and "coupling" in status and "duplication" in status


def test_exclusions_symlinks_and_untracked_source(source):
    tracked(source, {"vendor/broken.py": "def (\n"})
    (source / "untracked.py").write_text("def (\n")
    factors, status, details = python_measurement(source, MeasurementSettings())
    assert factors and not status and details["excluded_files"] == ["vendor/broken.py"]
    (source / "link.py").symlink_to(source.parent / "outside.py")
    git("add", "link.py", cwd=source)
    factors, status, details = python_measurement(source, MeasurementSettings())
    assert not factors and "link.py" in details["incomplete_files"]


def test_clone_line_threshold_and_token_resource_limit(source):
    tracked(source, {"one.py": "x = 1; y = 2; z = 3\n", "two.py": "x = 1; y = 2; z = 3\n"})
    factors, _, _ = python_measurement(source, MeasurementSettings(clone_tokens=5, clone_lines=5))
    assert factors["duplication"].numerator == 0
    factors, status, _ = python_measurement(source, MeasurementSettings(max_file_bytes=1))
    assert not factors and len(status) == 3
