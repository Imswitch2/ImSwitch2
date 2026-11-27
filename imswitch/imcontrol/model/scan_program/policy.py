"""Timing policies: how a program's requested times may become what runs
(docs/design/plans/scan-engines-and-expert-scan.md §2.6, §2.8).

A policy is a named, versioned document with a schema, not a constant:

- published versions live beside this module (``policies/<id>.json``) and
  are immutable -- each is pinned by its content hash, so a change is a new
  version, never an edit;
- a preset may bundle its own policy in the same schema;
- an acquisition records the policy's identifier and resolved contents;
- an unknown identifier or version is refused, never substituted.

Qt-free.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

SCHEMA = 'imswitch.scan-timing-policy/1'

#: How a requested time may land on the grid. 'exact' refuses a time that is
#: not a whole number of grid steps; 'ceil' rounds up; 'nearest-even' rounds to
#: the nearest step, ties to even (Python's ``round``).
ROUNDINGS = ('exact', 'ceil', 'nearest-even')
QUANTITIES = ('step', 'return', 'move', 'settle', 'wait', 'preparation', 'event_edges')
VANISHED = ('refuse', 'drop')
#: 'preserve' refuses a realization that changes the order of two events on a
#: device, closes a gap or opens an overlap; 'as-designers' reports it.
RELATIONS = ('preserve', 'as-designers')
SCHEDULING = ('sequential', 'deadlines')

#: Published policies and the SHA-256 of their documents. Editing a published
#: document breaks its pin: publish a new version instead.
PUBLISHED = {
    'strict-v1': 'a289bcd182b12da24aa6dbdb9828d5b0aea1a5d6d8986b3ffa820b3d82d14361',
    'advanced-compatible-v1': '57aed1fa8a9f4e9c0cf6c7fb0b387442532dc364f3f630cbbbedc02bdec54dc9',
}

POLICY_DIR = Path(__file__).parent / 'policies'


class PolicyError(ValueError):
    """An unknown, changed or malformed timing policy."""


@dataclass(frozen=True)
class TimingPolicy:
    id: str
    title: str
    rationale: str
    rounding: Mapping[str, str]
    clip_to_step: bool
    vanished: str
    relations: str
    scheduling: str
    tolerances: Mapping[str, Any] = field(default_factory=dict)
    document: Mapping[str, Any] = field(default_factory=dict)
    published: bool = False

    @property
    def exact(self) -> bool:
        return all(mode == 'exact' for mode in self.rounding.values())


def policy_from_document(document: Mapping[str, Any], *, published=False) -> TimingPolicy:
    """A policy from its document, or :class:`PolicyError` naming every problem."""
    problems = []
    if document.get('schema') != SCHEMA:
        problems.append(f'schema must be {SCHEMA!r}, not {document.get("schema")!r}')
    for key in ('id', 'title', 'rationale'):
        if not isinstance(document.get(key), str) or not document.get(key).strip():
            problems.append(f'{key} must be a non-empty text')
    rounding = document.get('rounding')
    if not isinstance(rounding, dict):
        problems.append('rounding must name a mode for every quantity')
        rounding = {}
    missing = [q for q in QUANTITIES if q not in rounding]
    extra = [q for q in rounding if q not in QUANTITIES]
    if missing:
        problems.append(f'rounding is missing {missing}')
    if extra:
        problems.append(f'rounding names unknown quantities {extra}')
    for quantity, mode in rounding.items():
        if mode not in ROUNDINGS:
            problems.append(f'rounding.{quantity}: {mode!r} is not one of {ROUNDINGS}')
    events = document.get('events') or {}
    if not isinstance(events.get('clip_to_step'), bool):
        problems.append('events.clip_to_step must be true or false')
    if events.get('vanished') not in VANISHED:
        problems.append(f'events.vanished must be one of {VANISHED}')
    if events.get('relations') not in RELATIONS:
        problems.append(f'events.relations must be one of {RELATIONS}')
    if document.get('scheduling') not in SCHEDULING:
        problems.append(f'scheduling must be one of {SCHEDULING}')
    tolerances = document.get('tolerances', {})
    if not isinstance(tolerances, dict):
        problems.append('tolerances must be a mapping')
        tolerances = {}
    if tolerances:
        # Nothing applies a policy's tolerances yet (§7.9): a policy that
        # declared one would claim a tolerance no realization honours.
        problems.append(f'tolerances are not applied by this version, so a policy cannot '
                        f'declare them ({", ".join(sorted(tolerances))}); a separation '
                        'carries its own tolerance_s')
    if problems:
        raise PolicyError(f'Timing policy {document.get("id")!r} is malformed:\n- '
                          + '\n- '.join(problems))
    return TimingPolicy(
        id=document['id'], title=document['title'], rationale=document['rationale'],
        rounding=dict(rounding), clip_to_step=events['clip_to_step'],
        vanished=events['vanished'], relations=events['relations'],
        scheduling=document['scheduling'],
        tolerances=dict(tolerances), document=json.loads(json.dumps(document)),
        published=published,
    )


def document_hash(path: Path) -> str:
    """The SHA-256 of a policy document's canonical JSON: its contents, not
    its bytes, so line endings and whitespace on a checkout do not matter."""
    document = json.loads(path.read_text(encoding='utf-8'))
    canonical = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def load_policy(policy_id: str) -> TimingPolicy:
    """A published policy, checked against its pin; unknown ones are refused."""
    if policy_id not in PUBLISHED:
        raise PolicyError(
            f'Unknown timing policy {policy_id!r}. Published policies: '
            f'{", ".join(sorted(PUBLISHED))}. No other policy is substituted.'
        )
    path = POLICY_DIR / f'{policy_id}.json'
    if not path.exists():
        raise PolicyError(f'The published timing policy {policy_id!r} is missing ({path}).')
    pinned = PUBLISHED[policy_id]
    if pinned is not None and document_hash(path) != pinned:
        raise PolicyError(
            f'The published timing policy {policy_id!r} was changed. Published versions are '
            'immutable: publish the change as a new version.'
        )
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('id') != policy_id:
        raise PolicyError(f'{path.name} declares id {document.get("id")!r}.')
    return policy_from_document(document, published=True)


def resolve_policy(policy) -> TimingPolicy:
    """A ``TimingPolicy`` for an identifier, a bundled document or a policy."""
    if isinstance(policy, TimingPolicy):
        return policy
    if isinstance(policy, str):
        return load_policy(policy)
    if isinstance(policy, Mapping):
        if policy.get('id') in PUBLISHED:
            published = load_policy(policy['id'])
            if dict(published.document) != json.loads(json.dumps(dict(policy))):
                raise PolicyError(
                    f'A bundled policy reuses the published id {policy["id"]!r} with other '
                    'contents. A bundled policy needs its own id.'
                )
            return published
        return policy_from_document(policy)
    raise PolicyError(f'Not a timing policy: {policy!r}')


__all__ = [
    'POLICY_DIR', 'PUBLISHED', 'PolicyError', 'SCHEMA', 'TimingPolicy',
    'document_hash', 'load_policy', 'policy_from_document', 'resolve_policy',
]


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
