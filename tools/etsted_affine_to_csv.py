"""Write an etSTED transform coefficient file from a 2x3 affine map.

    python tools/etsted_affine_to_csv.py --matrix A00 A01 A02 A10 A11 A12 \\
        --out ~/ImSwitchConfig/imcontrol_etsted/transform_pipelines/my_rig.csv

``[scan_1, scan_2] = A @ [row, col, 1]``: the camera pixel (row = index along
axis 0, col = index along axis 1 of the frame array) to the centres, in um, of
the first and second active scan axis (X and Y on a standard setup). A bead
registration on image arrays gives exactly this map. ``--input-order col_row``
reads the matrix as ``A @ [col, row, 1]`` instead.

Choose the file as "Transform coefficients" with the ``poly3`` transform.
See the header of ``analysis_pipelines/dff_event.py`` for why the order is
what it is.
"""
import argparse
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _service():
    # By path: the model package pulls in more than this needs.
    spec = importlib.util.spec_from_file_location(
        'etsted_transform_service',
        REPO / 'imswitch' / 'imcontrol' / 'model' / 'EtSTEDTransformService.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EtSTEDTransformService


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--matrix', nargs=6, type=float, required=True,
                        metavar=('A00', 'A01'), help='the 2x3 matrix, row by row')
    parser.add_argument('--input-order', choices=('row_col', 'col_row'), default='row_col')
    parser.add_argument('--out', required=True, help='the .csv file to write')
    args = parser.parse_args(argv)
    service = _service()
    matrix = [args.matrix[:3], args.matrix[3:]]
    coefficients = service.coefficients_from_affine(matrix, args.input_order)
    service.save_coefficients(args.out, coefficients)
    print(f'Wrote {args.out}')


if __name__ == '__main__':
    main()
