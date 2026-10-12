import ast
import fnmatch
import hashlib
import io
import tokenize
from collections import defaultdict
from pathlib import Path

from maxwell.measurement_architecture import architecture_measurement
from maxwell.records import MeasurementFactor
from maxwell.repositories import git


class Branches(ast.NodeVisitor):
    def __init__(self):
        self.value = 1

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def generic_visit(self, node):
        if isinstance(
            node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler)
        ):
            self.value += 1
        elif isinstance(node, ast.BoolOp):
            self.value += len(node.values) - 1
        elif isinstance(node, ast.comprehension):
            self.value += 1 + len(node.ifs)
        elif isinstance(node, ast.Match):
            self.value += sum(
                not (isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None)
                for case in node.cases
            )
            self.value += sum(case.guard is not None for case in node.cases)
        super().generic_visit(node)


def cyclic_components(graph):
    seen, order = set(), []
    for root in sorted(graph):
        stack = [(root, False)]
        while stack:
            node, finished = stack.pop()
            if finished:
                order.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend(
                    (target, False)
                    for target in sorted(graph[node], reverse=True)
                    if target not in seen
                )
    reverse = {node: set() for node in graph}
    for source, targets in graph.items():
        for target in targets:
            reverse[target].add(source)
    seen, components = set(), []
    for root in reversed(order):
        if root in seen:
            continue
        component, stack = [], [root]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            stack.extend(reverse[node] - seen)
        if len(component) > 1 or root in graph[root]:
            components.append(sorted(component))
    return sorted(components)


def duplicated_tokens(files, settings):
    width = settings.clone_tokens
    index, covered, examples = defaultdict(list), defaultdict(set), []
    modulus, base = (1 << 64) - 1, 257
    power = pow(base, width - 1, 1 << 64)
    for path, tokens in sorted(files.items()):
        numbers = [
            int.from_bytes(hashlib.blake2b(value.encode(), digest_size=8).digest(), "big")
            for value, _ in tokens
        ]
        rolling = 0
        for position, number in enumerate(numbers):
            if position >= width:
                rolling = (rolling - numbers[position - width] * power) & modulus
            rolling = (rolling * base + number) & modulus
            start = position - width + 1
            if (
                start < 0
                or len({line for _, line in tokens[start : position + 1]}) < settings.clone_lines
            ):
                continue
            values = tuple(value for value, _ in tokens[start : position + 1])
            group = next((group for group in index[rolling] if group[0] == values), None)
            if group is None:
                index[rolling].append((values, path, start))
                continue
            _, first_path, first_start = group
            covered[first_path].update(range(first_start, first_start + width))
            covered[path].update(range(start, start + width))
            if len(examples) < 20:
                examples.append(
                    {
                        "first": first_path,
                        "first_line": files[first_path][first_start][1],
                        "second": path,
                        "second_line": tokens[start][1],
                    }
                )
    return sum(len(positions) for positions in covered.values()), examples


def python_measurement(worktree, settings):
    paths = sorted(git("ls-files", "-z", cwd=worktree, strip=False).split("\0"))
    eligible = [path for path in paths if path.endswith((".py", ".pyi"))]
    excluded = [
        path
        for path in eligible
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in settings.exclude)
    ]
    eligible = [path for path in eligible if path not in excluded]
    trees, tokens, errors, functions = {}, {}, [], []
    total_tokens = 0
    for path in eligible:
        source = worktree / path
        try:
            if (
                source.is_symlink()
                or not source.resolve().is_relative_to(worktree.resolve())
                or source.stat().st_size > settings.max_file_bytes
            ):
                errors.append(path)
                continue
            with tokenize.open(source) as stream:
                text = stream.read()
            tree = ast.parse(text, filename=path)
            sequence = [
                (item.string, item.start[0])
                for item in tokenize.generate_tokens(io.StringIO(text).readline)
                if item.type
                not in (
                    tokenize.COMMENT,
                    tokenize.NL,
                    tokenize.NEWLINE,
                    tokenize.INDENT,
                    tokenize.DEDENT,
                    tokenize.ENDMARKER,
                    tokenize.ENCODING,
                )
            ]
        except (SyntaxError, UnicodeError, OSError, tokenize.TokenError, RecursionError):
            errors.append(path)
            continue
        total_tokens += len(sequence)
        trees[path], tokens[path] = tree, sequence
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                branches = Branches()
                for statement in node.body:
                    branches.visit(statement)
                functions.append(
                    {
                        "path": path,
                        "line": node.lineno,
                        "symbol": node.name,
                        "complexity": branches.value,
                    }
                )
    factors, status = {}, {}
    complete = bool(eligible) and not errors
    reason = (
        "No eligible Python source" if not eligible else "Source parsing or file limit incomplete"
    )
    complexity = sum(item["complexity"] for item in functions)
    if complete and complexity:
        factors["complexity"] = MeasurementFactor(
            numerator=sum(
                max(item["complexity"] - settings.complexity_threshold, 0) for item in functions
            ),
            denominator=complexity,
            unit="excess_complexity/complexity",
        )
    else:
        status["complexity"] = reason if not complete else "No functions"
    if complete and total_tokens and total_tokens <= settings.max_tokens:
        duplicate, examples = duplicated_tokens(tokens, settings)
        factors["duplication"] = MeasurementFactor(
            numerator=duplicate, denominator=total_tokens, unit="duplicated_tokens/tokens"
        )
    else:
        examples = []
        status["duplication"] = (
            reason if not complete else "Token population empty or token limit exceeded"
        )
    modules, collisions = {}, False
    for path in trees:
        relative = Path(path)
        for root in sorted(settings.source_roots, key=len, reverse=True):
            if relative.is_relative_to(root):
                relative = relative.relative_to(root)
                break
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        if module in modules:
            collisions = True
        modules[module] = path
    graph = {module: set() for module in modules}
    dynamic = 0
    for module, path in modules.items():
        package = module if Path(path).stem == "__init__" else module.rpartition(".")[0]
        for node in ast.walk(trees[path]):
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
            ):
                dynamic += 1
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    prefix = ".".join(
                        parts[: len(parts) - node.level + 1] + ([prefix] if prefix else [])
                    )
                names = [f"{prefix}.{item.name}" if prefix else item.name for item in node.names]
            for name in names:
                parts = name.split(".")
                for length in range(len(parts), 0, -1):
                    target = ".".join(parts[:length])
                    if target in graph:
                        graph[module].add(target)
                        break
    cycles = cyclic_components(graph)
    architecture = {}
    if complete and modules and not collisions and not dynamic:
        value, reason, architecture = architecture_measurement(graph, modules, trees, settings)
        if value is not None:
            factors["coupling"] = MeasurementFactor(
                numerator=value, denominator=1, unit="mean(propagation_cost,lack_of_cohesion)"
            )
        else:
            status["coupling"] = reason
    else:
        status["coupling"] = reason if not complete else "Ambiguous modules or dynamic imports"
    details = {
        "scope": "tracked Python source",
        "other_source_files": sum(
            path.endswith(
                (
                    ".js",
                    ".jsx",
                    ".ts",
                    ".tsx",
                    ".java",
                    ".kt",
                    ".go",
                    ".rs",
                    ".c",
                    ".cpp",
                    ".rb",
                    ".sh",
                )
            )
            for path in paths
        ),
        "eligible_files": len(eligible),
        "analyzed_files": len(trees),
        "excluded_files": excluded[:100],
        "excluded_file_count": len(excluded),
        "incomplete_files": errors[:100],
        "incomplete_file_count": len(errors),
        "functions": len(functions),
        "total_complexity": complexity,
        "tokens": total_tokens,
        "dynamic_imports": dynamic,
        "complex_functions": sorted(
            functions, key=lambda item: (-item["complexity"], item["path"], item["line"])
        )[:20],
        "clone_examples": examples,
        "cycles": cycles[:20],
        "architecture": architecture,
        "module_fanout": sorted(
            ({"module": module, "fanout": len(targets)} for module, targets in graph.items()),
            key=lambda item: (-item["fanout"], item["module"]),
        )[:20],
    }
    return factors, status, details
