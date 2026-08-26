from __future__ import annotations

from typing import Any

import numpy as np
from qtpy import QtWidgets


class CGHPresenter:
    """Default reusable Qt presentation for main CGH actions."""

    def __init__(self,*,display_name: str="") -> None:
        self.display_name = str(display_name or "SLM")

    @staticmethod
    def _feedback_loss_parts(status: Any):
        parts = []
        if int(getattr(status,"intensity_count",0) or 0) > 0:
            parts.append(
                "%d intensity feedback round(s)"
                % int(status.intensity_count)
            )
        if bool(getattr(status,"position_available",False)):
            parts.append("the position correction")
        if bool(getattr(status,"adaptation_pending",False)):
            parts.append("the pending adapted hologram")
        return parts

    @classmethod
    def _session_loss_detail(
        cls,status: Any,*,include_cgh_result: bool=False,
    ) -> str:
        parts = []
        if include_cgh_result:
            parts.append("the current CGH hologram")
        parts.extend(cls._feedback_loss_parts(status))
        return ", ".join(parts) or "the current feedback state"

    @staticmethod
    def _question(parent,title: str,message: str) -> bool:
        result = QtWidgets.QMessageBox.question(
            parent,
            str(title),
            str(message),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        return result == QtWidgets.QMessageBox.Yes

    def confirm_new_base_cgh(self,parent,status: Any) -> bool:
        detail = self._session_loss_detail(status)
        return self._question(
            parent,
            "Compute new CGH",
            "Computing from the main CGH controls starts a fresh CGH session "
            "and will discard %s if the new hologram computes successfully. "
            "Continue?" % detail,
        )

    def confirm_clear_cgh_session(
        self,parent,status: Any,*,has_cgh_result: bool=False,
    ) -> bool:
        detail = self._session_loss_detail(
            status,include_cgh_result=has_cgh_result,
        )
        return self._question(
            parent,
            "Clear CGH Session",
            "Clearing the CGH session will permanently discard %s. Continue?"
            % detail,
        )

    def plot_target_preview(
        self,section_key: str,target: np.ndarray,
    ) -> None:
        self._plot_image(
            f"CGH Target - {self.display_name}/{section_key}",
            target,
            "Target intensity",
            fourier_coordinates=True,
        )

    def plot_propagation(
        self,section_key: str,intensity: np.ndarray,
    ) -> None:
        self._plot_image(
            f"CGH Propagation - {self.display_name}/{section_key}",
            intensity,
            "Expected sample-plane intensity",
        )

    def plot_metrics(self,section_key: str,metrics) -> bool:
        metrics = tuple(metrics or ())
        if not metrics:
            return False

        import matplotlib.pyplot as plt

        iterations = [item.iteration for item in metrics]
        uniformity = [item.uniformity for item in metrics]
        normalized_std = [item.normalized_std for item in metrics]
        efficiencies = [item.efficiency for item in metrics]

        title = f"CGH Performance - {self.display_name}/{section_key}"
        plt.figure(title,figsize=(8,5))
        plt.clf()
        plt.plot(iterations,uniformity,label="Uniformity")
        plt.plot(iterations,normalized_std,label="Normalized STD")
        if any(value is not None for value in efficiencies):
            plt.plot(
                iterations,
                [np.nan if value is None else value for value in efficiencies],
                label="Efficiency",
            )
        plt.title(title)
        plt.xlabel("Iteration")
        plt.ylabel("Value")
        plt.legend()
        plt.tight_layout()
        plt.show(block=False)
        return True

    @staticmethod
    def _plot_image(
        title: str,array: np.ndarray,label: str,*,fourier_coordinates: bool=False,
    ) -> None:
        array = np.asarray(array)
        if array.ndim != 2:
            raise ValueError(
                f"Plot data must be two-dimensional, got {array.shape}"
            )

        import matplotlib.pyplot as plt

        plt.figure(title,figsize=(8,5))
        plt.clf()
        if fourier_coordinates:
            height,width = array.shape
            rows,cols = np.nonzero(array)
            x = (cols + 0.5) / width - 0.5
            y = (rows + 0.5) / height - 0.5
            values = array[rows,cols]
            plt.scatter(x,y,c=values,s=6,marker="o")
            plt.xlim(-0.5,0.5)
            plt.ylim(-0.5,0.5)
            plt.gca().set_aspect("equal",adjustable="box")
        else:
            plt.imshow(array,cmap="inferno")
        plt.title(label)
        plt.tight_layout()
        plt.show(block=False)
