import ast
from itertools import combinations

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def production_path(path, patterns):
    import fnmatch

    return not any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def components(graph):
    seen, result = set(), []
    for root in sorted(graph):
        if root in seen:
            continue
        group, stack = [], [root]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            group.append(node)
            stack.extend(graph[node] - seen)
        result.append(sorted(group))
    return result


def references(node, globals_, receiver=None):
    local = set()
    if isinstance(node, FUNCTIONS):
        local.update(
            arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        )
        local.update(arg.arg for arg in (node.args.vararg, node.args.kwarg) if arg)
        local.update(
            item.id
            for item in ast.walk(node)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        )
    used = {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name)
        and isinstance(item.ctx, ast.Load)
        and item.id in globals_
        and item.id not in local
    }
    if receiver:
        used.update(
            f"self.{item.attr}"
            for item in ast.walk(node)
            if isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == receiver
        )
    return used


def cohesion_unit(path, symbol, members, globals_, methods=False):
    if len(members) < 2:
        return None
    refs = {}
    for member in members:
        args = (
            [*member.args.posonlyargs, *member.args.args] if isinstance(member, FUNCTIONS) else []
        )
        receiver = args[0].arg if methods and args and args[0].arg in ("self", "cls") else None
        refs[member.name] = references(member, globals_, receiver)
    graph = {name: set() for name in refs}
    for first, second in combinations(refs, 2):
        prefix = "self." if methods else ""
        if (
            refs[first] & refs[second]
            or prefix + first in refs[second]
            or prefix + second in refs[first]
        ):
            graph[first].add(second)
            graph[second].add(first)
    groups = components(graph)
    pairs = len(refs) * (len(refs) - 1) // 2
    disconnected = pairs - sum(len(group) * (len(group) - 1) // 2 for group in groups)
    return {
        "path": path,
        "symbol": symbol,
        "line": members[0].lineno,
        "members": len(refs),
        "components": groups,
        "disconnected_pairs": disconnected,
        "possible_pairs": pairs,
        "lack_of_cohesion": disconnected / pairs if pairs else 0,
    }


def architecture_measurement(graph, modules, trees, settings):
    selected = {
        name: path
        for name, path in modules.items()
        if production_path(path, settings.architecture_exclude)
    }
    if not selected:
        return None, "No production modules for architecture analysis", {}
    if len(selected) > settings.max_architecture_modules:
        return None, "Architecture module limit exceeded", {"modules": len(selected)}
    graph = {name: (graph[name] & selected.keys()) - {name} for name in selected}
    incoming = {name: set() for name in graph}
    for name, targets in graph.items():
        for target in targets:
            incoming[target].add(name)
    names = sorted(graph)
    indices = {name: index for index, name in enumerate(names)}
    reachable = [sum(1 << indices[target] for target in graph[name]) for name in names]
    for pivot in range(len(names)):
        mask = 1 << pivot
        for index in range(len(names)):
            if reachable[index] & mask:
                reachable[index] |= reachable[pivot]
    reach_counts = {
        name: (reachable[index] & ~(1 << index)).bit_count() for index, name in enumerate(names)
    }
    hotspots, units = [], []
    reached = sum(reach_counts.values())
    for name, targets in graph.items():
        hotspots.append(
            {
                "module": name,
                "path": selected[name],
                "fanout": len(targets),
                "fanin": len(incoming[name]),
                "reachable_dependencies": reach_counts[name],
            }
        )
        tree = trees[selected[name]]
        globals_ = {item.name for item in tree.body if isinstance(item, (*FUNCTIONS, ast.ClassDef))}
        for item in tree.body:
            if isinstance(item, (ast.Assign, ast.AnnAssign)):
                globals_.update(
                    child.id
                    for child in ast.walk(item)
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
                )
        members = [item for item in tree.body if isinstance(item, (*FUNCTIONS, ast.ClassDef))]
        if len(members) > settings.max_cohesion_members:
            return None, "Architecture cohesion member limit exceeded", {"path": selected[name]}
        unit = cohesion_unit(selected[name], name, members, globals_)
        if unit:
            units.append(unit)
        for cls in (item for item in ast.walk(tree) if isinstance(item, ast.ClassDef)):
            methods = [
                item
                for item in cls.body
                if isinstance(item, FUNCTIONS) and item.name not in ("__init__", "__new__")
            ]
            if len(methods) > settings.max_cohesion_members:
                return (
                    None,
                    "Architecture cohesion member limit exceeded",
                    {"path": selected[name], "symbol": cls.name},
                )
            unit = cohesion_unit(selected[name], cls.name, methods, set(), methods=True)
            if unit:
                units.append(unit)
    possible = len(graph) * (len(graph) - 1)
    propagation = reached / possible if possible else 0
    disconnected = sum(unit["disconnected_pairs"] for unit in units)
    cohesion_pairs = sum(unit["possible_pairs"] for unit in units)
    lack = disconnected / cohesion_pairs if cohesion_pairs else 0
    details = {
        "modules": len(graph),
        "excluded_modules": len(modules) - len(graph),
        "reachable_pairs": reached,
        "possible_dependency_pairs": possible,
        "propagation_cost": propagation,
        "disconnected_pairs": disconnected,
        "possible_cohesion_pairs": cohesion_pairs,
        "lack_of_cohesion": lack,
        "coupling_hotspots": sorted(
            hotspots,
            key=lambda item: (-item["reachable_dependencies"], -item["fanout"], item["module"]),
        )[:20],
        "cohesion_hotspots": sorted(
            units,
            key=lambda item: (
                -item["lack_of_cohesion"],
                -item["members"],
                item["path"],
                item["symbol"],
            ),
        )[:20],
    }
    return (propagation + lack) / 2, None, details
