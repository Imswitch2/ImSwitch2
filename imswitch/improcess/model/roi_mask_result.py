"""The ROI set as an image (P-6.4, C-01).

ImageJ's *Create Mask* makes a new image; here it makes a
:class:`ProcessingResult`, which is the same idea in this application's terms —
it lands in the reconstruction list, so it can be saved, processed, measured
and compared by the machinery every other result already uses. A viewer layer
would have been fewer lines and a dead end: nothing else in ImProcess consumes
a bare layer.
"""

from __future__ import annotations


import numpy as np

from imswitch.improcess.model.result import ProcessingResult


class ROIMaskResult(ProcessingResult):
    """A label image in which value ``n`` is the nth ROI.

    ``kind = "labels"`` rather than ``"image"``: the values are identities, not
    intensities, so a processor that would happily filter or rescale an image
    must not be offered for this one.
    """

    kind = "labels"

    def __init__(
        self,
        name: str,
        data: np.ndarray,
        axis_labels: list[str] | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
        roi_names: list[str] | None = None,
        **kwargs,
    ):
        #: Which ROI each label value came from, so a reader can map a label
        #: back to the region that produced it without guessing at the order.
        self.roi_names = list(roi_names or [])
        array = np.asarray(data)
        super().__init__(
            name=name,
            data=array,
            axis_labels=list(axis_labels or ["Y", "X"]),
            axis_scales=list(axis_scales or [1.0] * array.ndim),
            scale_unit=scale_unit,
            display_levels=(0.0, float(max(1, int(array.max()) if array.size else 1))),
            **kwargs,
        )

    def table_records(self) -> list[dict]:
        """One row per ROI, so the label values are legible in the table."""
        return [
            {"label": index, "roi": name}
            for index, name in enumerate(self.roi_names, start=1)
        ]


    supported_formats = ("tiff", "hdf5")

    def write_files(self, plan, document) -> None:
        """Write the label image, in the format the plan asks for."""
        if plan.fmt == "tiff":
            from imswitch.improcess.model.result_io import save_image_result

            save_image_result(
                self, plan.primary, "tiff",
                extra={"labels": True, "roi_names": [str(n) for n in self.roi_names]},
                document=document,
            )
            return
        if plan.fmt == "hdf5":
            import h5py

            from imswitch.improcess.model.save_protocol import embed_hdf5

            with h5py.File(str(plan.primary), "w") as handle:
                dataset = handle.create_dataset("labels", data=np.asarray(self.data))
                dataset.attrs["roi_names"] = [str(n) for n in self.roi_names]
                embed_hdf5(handle, document)
            return
        raise ValueError(f"unsupported format {plan.fmt!r} for an ROI label image")



__all__ = ["ROIMaskResult"]
