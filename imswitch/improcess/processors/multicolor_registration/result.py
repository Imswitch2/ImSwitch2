"""Result wrapper for multicolor registration calibration."""


import h5py
import numpy as np

from imswitch.improcess.analysis.multicolor import alignment_summary, save_alignment
from imswitch.improcess.model.result import ProcessingResult, ViewMode


class MulticolorRegistrationResult(ProcessingResult):
    """Aligned bead preview plus the extracted registration transform."""

    def __init__(
        self,
        name: str,
        data: np.ndarray,
        alignment: dict,
        params: dict | None = None,
        axis_scales: list[float] | None = None,
        scale_unit: str = "px",
    ):
        self.alignment = alignment
        self.params = params or {}
        self.summary = alignment_summary(alignment)
        finite = data[np.isfinite(data)]
        display_levels = (
            (float(np.nanmin(finite)), float(np.nanmax(finite)))
            if finite.size
            else None
        )
        super().__init__(
            name=name,
            data=data,
            axis_labels=["C", "Z", "Y", "X"],
            view_modes=[
                ViewMode("CZYX", (0, 1, 2, 3)),
                ViewMode("ZCYX", (1, 0, 2, 3)),
            ],
            display_levels=display_levels,
            axis_scales=axis_scales or [1.0, 1.0, 1.0, 1.0],
            scale_unit=scale_unit,
        )


    supported_formats = ("hdf5", "tiff")

    def write_files(self, plan, document) -> None:
        if plan.fmt == "hdf5":
            from imswitch.improcess.model.save_protocol import embed_hdf5

            save_alignment(self.alignment, plan.primary)
            with h5py.File(str(plan.primary), "a") as h5:
                h5.create_dataset("aligned_preview", data=np.asarray(self.data), compression="gzip")
                h5.attrs["summary"] = self.summary
                h5.attrs["axis_labels"] = ",".join(self.axis_labels)
                h5.attrs["scale_unit"] = self.scale_unit
                for key, value in self.params.items():
                    if value is not None:
                        h5.attrs[f"param_{key}"] = value
                embed_hdf5(h5, document)
        elif plan.fmt == "tiff":
            from imswitch.improcess.model.result_io import save_image_result

            save_image_result(
                self, plan.primary, "tiff", extra={"summary": self.summary}, document=document
            )
        else:
            raise ValueError(f"Multicolor registration supports HDF5 or TIFF, got {plan.fmt!r}")
