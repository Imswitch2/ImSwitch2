"""Mirror command and matrix files.

A *command* is one value per actuator; a *matrix* has a row per mode (or
per anything) and a column per actuator. Read from:

- ``.csv`` / ``.txt``: numbers separated by commas, semicolons or white
  space. A first line that is not numbers is a header; a column headed
  ``Push`` (what Microscope_GUI wrote) or ``Command`` is the command,
  otherwise the last column is.
- ``.npy``: a NumPy array.
- ``.xlsx``: the first sheet, its ``Push`` column (or the last one) --
  Microscope_GUI's flat files. Read without openpyxl.
- ``.tif`` / ``.tiff``: a 2-D image (Microscope_GUI's control matrices).

Commands are written as ``.csv`` (``Actuator,Push`` with one row per
actuator) or ``.npy``.
"""

import re
import zipfile
from pathlib import Path
from typing import List, Optional
from xml.etree import ElementTree

import numpy as np

_SPLIT = re.compile(r'[,;\s]+')
_COMMAND_HEADERS = ('push', 'command', 'value', 'values')


def _numbers(tokens: List[str]) -> Optional[List[float]]:
    try:
        return [float(token) for token in tokens]
    except ValueError:
        return None


def _rows_of_text(path: Path):
    header, rows = None, []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        tokens = [t for t in _SPLIT.split(line.strip()) if t]
        if not tokens:
            continue
        values = _numbers(tokens)
        if values is None:
            if header is None and not rows:
                header = [t.strip().strip('"').lower() for t in tokens]
                continue
            raise ValueError(f'{path.name}: "{line.strip()}" is not numbers')
        rows.append(values)
    if not rows:
        raise ValueError(f'{path.name} holds no numbers')
    if len({len(row) for row in rows}) != 1:
        raise ValueError(f'{path.name}: its rows have different lengths')
    return header, np.array(rows, dtype=float)


def _column(header, table: np.ndarray) -> np.ndarray:
    if header is not None and len(header) == table.shape[1]:
        for name in _COMMAND_HEADERS:
            if name in header:
                return table[:, header.index(name)]
    return table[:, -1]


def _xlsx_table(path: Path):
    """The first sheet of an .xlsx file as (header, table), from its XML."""
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shared = []
        if 'xl/sharedStrings.xml' in names:
            root = ElementTree.fromstring(archive.read('xl/sharedStrings.xml'))
            for item in root.findall('m:si', ns):
                shared.append(''.join(t.text or '' for t in item.iter(f'{{{ns["m"]}}}t')))
        sheets = sorted(n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))
        if not sheets:
            raise ValueError(f'{path.name} has no worksheet')
        sheet = 'xl/worksheets/sheet1.xml' if 'xl/worksheets/sheet1.xml' in sheets else sheets[0]
        root = ElementTree.fromstring(archive.read(sheet))
    rows = []
    for row in root.iter(f'{{{ns["m"]}}}row'):
        cells = {}
        for cell in row.findall('m:c', ns):
            column = re.match(r'[A-Z]+', cell.get('r', 'A')).group(0)
            kind = cell.get('t')
            if kind == 'inlineStr':
                text = ''.join(t.text or '' for t in cell.iter(f'{{{ns["m"]}}}t'))
            else:
                value = cell.find('m:v', ns)
                text = None if value is None else value.text
                if kind == 's' and text is not None:
                    text = shared[int(text)]
            if text is not None:
                cells[column] = text
        if cells:
            rows.append([cells[c] for c in sorted(cells, key=lambda c: (len(c), c))])
    if not rows:
        raise ValueError(f'{path.name}: its first sheet is empty')
    header = None
    if _numbers(rows[0]) is None:
        header = [t.strip().lower() for t in rows[0]]
        rows = rows[1:]
    table = [_numbers(row) for row in rows]
    if not table or any(row is None for row in table) or len({len(r) for r in table}) != 1:
        raise ValueError(f'{path.name}: its first sheet is not a table of numbers')
    return header, np.array(table, dtype=float)


def read_command(path, actuators: Optional[int] = None) -> np.ndarray:
    """One value per actuator from ``path``; ``actuators`` checks the count."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == '.npy':
        command = np.asarray(np.load(path), dtype=float)
        if command.ndim == 2 and 1 in command.shape:
            command = command.ravel()
        if command.ndim != 1:
            raise ValueError(f'{path.name}: a command is one row of numbers, not {command.shape}')
    elif suffix == '.xlsx':
        header, table = _xlsx_table(path)
        command = _column(header, table)
    elif suffix in ('.csv', '.txt', ''):
        header, table = _rows_of_text(path)
        command = table.ravel() if table.shape[0] == 1 else _column(header, table)
    else:
        raise ValueError(f'{path.name}: a command file is .csv, .txt, .npy or .xlsx')
    if actuators is not None and command.size != actuators:
        raise ValueError(f'{path.name} holds {command.size} values for {actuators} actuators')
    if not np.all(np.isfinite(command)):
        raise ValueError(f'{path.name} holds values that are not finite')
    return command


def read_matrix(path) -> np.ndarray:
    """A 2-D table of numbers from ``path`` (no header)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == '.npy':
        matrix = np.asarray(np.load(path), dtype=float)
    elif suffix in ('.tif', '.tiff'):
        import tifffile
        matrix = np.asarray(tifffile.imread(str(path)), dtype=float)
    elif suffix in ('.csv', '.txt', ''):
        _, matrix = _rows_of_text(path)
    else:
        raise ValueError(f'{path.name}: a matrix file is .csv, .txt, .npy or .tif')
    if matrix.ndim != 2:
        raise ValueError(f'{path.name}: a matrix has two dimensions, not {matrix.shape}')
    if not np.all(np.isfinite(matrix)):
        raise ValueError(f'{path.name} holds values that are not finite')
    return matrix


def write_command(path, command) -> Path:
    """Write ``command`` as .csv (``Actuator,Push``) or .npy; returns the path."""
    path = Path(path)
    command = np.asarray(command, dtype=float).ravel()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == '.npy':
        np.save(path, command)
    elif path.suffix.lower() in ('.csv', '.txt'):
        lines = ['Actuator,Push'] + [f'{i},{value!r}' for i, value in enumerate(command.tolist())]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    else:
        raise ValueError(f'{path.name}: write a command as .csv or .npy')
    return path


def write_matrix(path, matrix) -> Path:
    """Write a 2-D ``matrix`` as .csv (no header) or .npy; returns the path."""
    path = Path(path)
    matrix = np.asarray(matrix, dtype=float)
    if matrix.ndim != 2:
        raise ValueError(f'a matrix has two dimensions, not {matrix.shape}')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == '.npy':
        np.save(path, matrix)
    elif path.suffix.lower() in ('.csv', '.txt'):
        path.write_text('\n'.join(','.join(repr(v) for v in row) for row in matrix.tolist()) + '\n',
                        encoding='utf-8')
    else:
        raise ValueError(f'{path.name}: write a matrix as .csv or .npy')
    return path


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
