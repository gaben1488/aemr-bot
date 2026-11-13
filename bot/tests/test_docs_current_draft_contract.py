"""Keep current operator instructions aligned with persistent draft delivery."""
from pathlib import Path

import pytest

KB = Path(__file__).resolve().parents[2] / 'docs/site/_kb2'


@pytest.mark.parametrize(('fragment', 'obsolete'), [
    ('operator', 'следующее ваше сообщение в служебной группе уйдёт жителю'),
    ('operator', 'без неё бот не сохранит новый текст'),
    ('operator', 'до конца 5-минутного окна'),
    ('codemap', 'Намерение ответить живёт в памяти и одноразово'),
    ('codemap', 'как суть и зовёт финализацию'),
    ('voprosy', 'можно смело отправить ещё раз'),
    ('voprosy', 'до конца пятиминутного окна'),
    ('voprosy', 'намерение живёт в оперативной памяти, не в базе'),
    ('voprosy', 'теряется только незавершённое'),
])
def test_current_instructions_do_not_restore_pre_draft_contract(fragment, obsolete):
    assert obsolete not in (KB / f'{fragment}.html').read_text()
