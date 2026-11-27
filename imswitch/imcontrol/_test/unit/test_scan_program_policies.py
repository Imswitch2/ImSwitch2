"""Timing policies are versioned, validated, immutable documents
(docs/design/plans/scan-engines-and-expert-scan.md §2.6, §2.8).

Each published version is pinned here by what it accepts, rounds and
refuses, and by its content hash; an unknown version is refused, never
substituted; a bundled policy uses the same schema.
"""

import json
import shutil

import pytest

from imswitch.imcontrol.model.scan_program import policy as policies
from imswitch.imcontrol.model.scan_program.policy import (
    PolicyError, document_hash, load_policy, policy_from_document, resolve_policy,
)


def test_every_published_policy_loads_and_matches_its_pin():
    for policy_id, pinned in policies.PUBLISHED.items():
        policy = load_policy(policy_id)
        assert policy.id == policy_id and policy.published
        assert pinned == document_hash(policies.POLICY_DIR / f'{policy_id}.json')
        assert policy.rationale.strip()


def test_strict_v1_is_exact_everywhere():
    strict = load_policy('strict-v1')
    assert strict.exact
    assert set(strict.rounding.values()) == {'exact'}
    assert (strict.clip_to_step, strict.vanished, strict.relations, strict.scheduling) == \
        (False, 'refuse', 'preserve', 'sequential')
    assert strict.tolerances == {}


def test_advanced_compatible_v1_is_what_the_designers_do():
    compatible = load_policy('advanced-compatible-v1')
    assert {q: compatible.rounding[q] for q in ('step', 'return', 'move', 'settle')} == \
        dict.fromkeys(('step', 'return', 'move', 'settle'), 'ceil')
    assert compatible.rounding['event_edges'] == 'nearest-even'
    assert (compatible.clip_to_step, compatible.vanished, compatible.relations) == \
        (True, 'drop', 'as-designers')


def test_an_unknown_policy_is_refused_not_substituted():
    with pytest.raises(PolicyError, match="Unknown timing policy 'strict-v2'"):
        load_policy('strict-v2')


def test_a_published_policy_cannot_be_edited_in_place(tmp_path, monkeypatch):
    for name in policies.PUBLISHED:
        shutil.copy(policies.POLICY_DIR / f'{name}.json', tmp_path / f'{name}.json')
    document = json.loads((tmp_path / 'strict-v1.json').read_text())
    document['rounding']['step'] = 'ceil'
    (tmp_path / 'strict-v1.json').write_text(json.dumps(document))
    monkeypatch.setattr(policies, 'POLICY_DIR', tmp_path)
    with pytest.raises(PolicyError, match='was changed'):
        load_policy('strict-v1')
    # Whitespace and line endings are not contents.
    text = (policies.POLICY_DIR / 'advanced-compatible-v1.json').read_text()
    (tmp_path / 'advanced-compatible-v1.json').write_text(text.replace('\n', '\r\n'))
    assert load_policy('advanced-compatible-v1').id == 'advanced-compatible-v1'


def _bundled(**changes):
    document = json.loads((policies.POLICY_DIR / 'strict-v1.json').read_text())
    document.update(id='preset-nearest', title='A preset', rationale='For a test.')
    for key, value in changes.items():
        document[key] = value
    return document


def test_a_preset_bundles_a_policy_in_the_same_schema():
    policy = resolve_policy(_bundled(rounding=dict.fromkeys(policies.QUANTITIES, 'nearest-even')))
    assert policy.id == 'preset-nearest' and not policy.published and not policy.exact


def test_a_bundled_policy_may_not_reuse_a_published_id_with_other_contents():
    document = _bundled(id='strict-v1', rounding=dict.fromkeys(policies.QUANTITIES, 'ceil'))
    with pytest.raises(PolicyError, match='needs its own id'):
        resolve_policy(document)


def test_a_malformed_policy_names_every_problem():
    with pytest.raises(PolicyError) as refused:
        policy_from_document({'schema': 'nope', 'id': '', 'rounding': {'step': 'round'},
                              'events': {}, 'scheduling': 'later',
                              'tolerances': {'duration': {'value': 0.01}}})
    text = str(refused.value)
    for problem in ("schema must be 'imswitch.scan-timing-policy/1'", 'id must be a non-empty',
                    'rounding is missing', "rounding.step: 'round'", 'clip_to_step',
                    'events.vanished', 'events.relations', 'scheduling must be',
                    # nothing applies tolerances yet, so none may be declared (§7.9)
                    'tolerances are not applied by this version, so a policy cannot '
                    'declare them (duration)'):
        assert problem in text, problem


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
