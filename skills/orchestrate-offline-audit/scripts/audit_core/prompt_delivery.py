"""Share exact Skill prose within one API request; never shorten evidence."""
from pathlib import Path
from collections import Counter
from copy import deepcopy
import json


def share_schema_definitions(schema):
    """Factor repeated schema nodes, retaining every original constraint.

    Existing reference scopes are deliberately left alone. Only JSON Schema
    child-schema positions are visited; enum/const/default business values are
    never interpreted as schemas. The original object is not mutated.
    """
    mappings = ("properties", "patternProperties", "dependentSchemas")
    singles = ("items", "additionalProperties", "unevaluatedProperties", "additionalItems",
               "unevaluatedItems", "propertyNames", "contains", "not", "if", "then", "else")
    arrays = ("allOf", "anyOf", "oneOf", "prefixItems")
    counts, nodes = Counter(), {}
    unsafe = False

    def encoded(node):
        return json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def children(node, transform):
        projected = deepcopy(node)
        for key in mappings:
            if isinstance(node.get(key), dict):
                projected[key] = {name: transform(child) for name, child in node[key].items()}
        for key in singles:
            if isinstance(node.get(key), (dict, bool)):
                projected[key] = transform(node[key])
            elif key == "items" and isinstance(node.get(key), list):
                projected[key] = [transform(child) for child in node[key]]
        for key in arrays:
            if isinstance(node.get(key), list):
                projected[key] = [transform(child) for child in node[key]]
        return projected

    def inspect(node):
        nonlocal unsafe
        if not isinstance(node, dict):
            return node
        if any(key in node for key in ("$ref", "$id", "id", "$anchor", "$dynamicRef",
                                       "$dynamicAnchor", "$defs", "definitions", "$recursiveRef", "$recursiveAnchor")):
            unsafe = True
        key = encoded(node)
        counts[key] += 1
        nodes.setdefault(key, node)
        children(node, inspect)
        return node

    if not isinstance(schema, dict):
        return schema
    inspect(schema)
    if unsafe:
        return schema
    shared = {key: f"s{index}" for index, (key, count) in enumerate(counts.items())
              if count > 1 and len(key.encode("utf-8")) >= 128}
    if not shared:
        return schema

    def render(node, inline=False):
        if not isinstance(node, dict):
            return node
        key = encoded(node)
        if key in shared and not inline:
            return {"$ref": "#/$defs/" + shared[key]}
        return children(node, render)

    projected = render(schema, inline=True)
    definitions = {name: render(nodes[key], inline=True) for key, name in shared.items()}

    def references(value):
        found = []
        def visit(node):
            if isinstance(node, dict):
                if set(node) == {"$ref"}:
                    found.append(node["$ref"].removeprefix("#/$defs/"))
                else:
                    children(node, visit)
            return node
        visit(value)
        return found

    pending, used = list(references(projected)), set()
    while pending:
        name = pending.pop()
        if name not in used:
            used.add(name)
            pending.extend(references(definitions[name]))
    projected["$defs"] = {name: value for name, value in definitions.items() if name in used}
    # Tiny differences do not justify introducing another representation.
    return projected if len(encoded(schema).encode("utf-8")) - len(encoded(projected).encode("utf-8")) >= 256 else schema


def share_system_business_guide(prompt, skill):
    """The API adapter sends this exact file in system on every attempt.

    Only named instruction bindings are changed. Source documents, dynamic
    clarifications, schemas and CLI prompts are never searched or rewritten.
    """
    path = Path(skill) / "references/business-guide.md"
    values = getattr(prompt, "partial_variables", {})
    if not path.is_file() or not hasattr(prompt, "partial"):
        return prompt, 0
    guide = path.read_text(encoding="utf-8")
    if not guide:
        return prompt, 0
    shared = "本类业务说明已完整逐字提供在system的 references/business-guide.md 中，仍须逐项执行；此处引用同一原文，不省略审核内容。"
    updates = {}
    for key in ("guide", "business_guide"):
        value = values.get(key)
        if isinstance(value, str) and value.startswith(guide) and len(shared) < len(guide):
            updates[key] = shared + value[len(guide):]
    if not updates:
        return prompt, 0
    projected = prompt.partial(**updates)
    saved = len(prompt.format().encode("utf-8")) - len(projected.format().encode("utf-8"))
    return (projected, saved) if saved > 0 else (prompt, 0)
