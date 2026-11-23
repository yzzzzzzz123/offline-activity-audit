"""Structured advice from deterministic helpers; never a business verdict.

Keep schema diagnostics free of document contents. A repeated operand is an
ambiguous source identity to review, not permission to delete equal values.
Source authenticity and run ownership are checked separately by source_guard.
"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal

from jsonschema import Draft202012Validator


def _identity(value):
    """JSON equality, including 1 == 1.0 and true != 1, without deleting rows."""
    if isinstance(value, bool) or value is None:
        return type(value).__name__, value
    if isinstance(value, (int, float)):
        return 'number', Decimal(str(value))
    if isinstance(value, dict):
        return 'object', tuple(sorted((key, _identity(item)) for key, item in value.items()))
    if isinstance(value, list):
        return 'array', tuple(_identity(item) for item in value)
    return 'string', value


def validation_detail(error, value):
    path = list(error.absolute_path)
    detail = {'path': '/'.join(map(str, path)) or '$', 'constraint': error.validator,
              'actual_type': type(error.instance).__name__}
    if len(path) >= 2 and path[0] == 'checks' and isinstance(path[1], int):
        checks = value.get('checks', []) if isinstance(value, dict) else []
        if path[1] < len(checks) and isinstance(checks[path[1]], dict):
            detail['rule_id'] = checks[path[1]].get('rule_id')
    if isinstance(error.instance, list):
        detail['item_count'] = len(error.instance)
        if error.validator == 'uniqueItems':
            positions = defaultdict(list)
            for index, item in enumerate(error.instance):
                positions[_identity(item)].append(index)
            detail['duplicate_positions'] = [indexes for indexes in positions.values() if len(indexes) > 1]
            detail['guidance'] = '按原表真实行和单元格核实重复引用；相同数值可能属于不同明细，不能自动去重或删行。'
    scalar_constraints = {'type', 'minItems', 'maxItems', 'uniqueItems', 'minLength', 'maxLength',
                          'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'}
    if error.validator in scalar_constraints and isinstance(error.validator_value, (bool, int, float, str)):
        detail['expected'] = error.validator_value
    return detail


def schema_advice(value, schema):
    issues = [validation_detail(error, value)
              for error in Draft202012Validator(schema).iter_errors(value)]
    return {'status': 'model_takeover' if issues else 'available',
            'authoritative': False, 'issues': issues}


def error_advice(error):
    from .execution_diagnostics import _text
    detail = getattr(error, 'validation_detail', None)
    return {'status': 'model_takeover', 'authoritative': False,
            'error_type': type(error).__name__, 'error': _text(error),
            **({'issues': [deepcopy(detail)]} if isinstance(detail, dict) else {})}
