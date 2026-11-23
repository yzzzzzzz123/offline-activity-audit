"""Human-confirmed OCR examples, supplied before reading the current image.

This is in-context reference material, never evidence for the current case.
The catalogue is source controlled; customer originals live beside it in an
ignored originals/ directory. Reading these examples makes no model requests.
"""
from copy import deepcopy
import hashlib
import json

from .paths import SKILL_ROOT


CATALOG = SKILL_ROOT / 'references/recognition-badcases/catalog.json'


def load_examples():
    raw = CATALOG.read_bytes()
    value = json.loads(raw.decode('utf-8'))
    examples = [deepcopy(item) for item in value['examples']
                if item.get('approval') in {'user_confirmed', 'visually_verified'}]
    return {'revision': value['revision'], 'catalog_sha256': hashlib.sha256(raw).hexdigest(),
            'method': 'human_confirmed_text_examples', 'examples': examples}


def study_prompt(manifest, catalogue=None):
    catalogue = catalogue if catalogue is not None else load_examples()
    readers = {name for item in manifest for name in item.get('reader_skills', [])}
    examples = [item for item in catalogue['examples']
                if not readers or readers.intersection(item['reader_skills'])]
    reference = {'revision': catalogue['revision'], 'catalog_sha256': catalogue['catalog_sha256'],
                 'examples': [{key: item[key] for key in
                    ('id', 'field', 'incorrect_reading', 'confirmed_reading', 'reading_lesson')}
                    for item in examples]}
    return ('读图前先参考下列人工核验的纠错示例，学习字形、排版和叠印的读取方式。'
            '这些是文字示例，原件另存案例库；不表示本次已发送案例原图或训练模型权重。'
            '示例不是本单证据，不能把示例公司、时间、金额或审核结论复制到当前facts。'
            '当前事实仍须从当前所附原图读取，模糊处保留可见片段和不确定字形。\n'
            + json.dumps(reference, ensure_ascii=False, separators=(',', ':')))
