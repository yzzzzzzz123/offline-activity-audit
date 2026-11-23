"""Resolve material references to actual source units within the current case.

Inventory may refer to an original file while native spreadsheet readers emit
separate worksheet/chunk units. Only explicit file_id metadata creates that
relationship; similar names and unit-id prefixes never grant source access.
"""


def resolve_source_ids(case, references):
    documents = {document['unit_id'] for document in case.get('documents', [])}
    by_file = {}
    for unit in case.get('units', []):
        if unit['unit_id'] in documents and unit.get('file_id'):
            by_file.setdefault(unit['file_id'], []).append(unit['unit_id'])
    result = []
    for reference in references:
        if reference in documents and reference not in result:
            result.append(reference)
        for unit_id in by_file.get(reference, []):
            if unit_id not in result:
                result.append(unit_id)
    return result


def material_source_ids(case, material_id, *, states=('present', 'unclear')):
    """Read supplied sources independently of a material's field completeness.

    An unclear POS can still contain an exact name/barcode. This selects source
    bytes only: the original material status/reason and every business judgment
    remain untouched.
    """
    references = [source for material in case.get('materials', [])
                  if material.get('id') == material_id and material.get('state') in states
                  for source in material.get('source_ids', [])]
    return resolve_source_ids(case, references)
