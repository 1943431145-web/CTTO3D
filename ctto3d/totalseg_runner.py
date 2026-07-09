"""Single-process TotalSegmentator runner for Windows.

TotalSegmentator's CLI calls nnU-Net's default batch inference path. On some
Windows/Python/PyTorch combinations that path can fail while sharing tensors
between background processes. This runner keeps TotalSegmentator itself intact
and only swaps nnU-Net inference to its built-in sequential method.
"""

from __future__ import annotations

import argparse
import multiprocessing
from pathlib import Path


def _patch_nnunet_to_sequential():
    from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor

    if getattr(nnUNetPredictor, "_ctto3d_sequential_patch", False):
        return

    def predict_from_files_single_process(
        self,
        list_of_lists_or_source_folder,
        output_folder_or_list_of_truncated_output_files,
        save_probabilities=False,
        overwrite=True,
        num_processes_preprocessing=1,
        num_processes_segmentation_export=1,
        folder_with_segs_from_prev_stage=None,
        num_parts=1,
        part_id=0,
    ):
        if num_parts != 1 or part_id != 0:
            raise ValueError("CTto3D sequential runner only supports one inference part.")
        print("Running nnUNet in sequential single-process mode.")
        return self.predict_from_files_sequential(
            list_of_lists_or_source_folder,
            output_folder_or_list_of_truncated_output_files,
            save_probabilities=save_probabilities,
            overwrite=overwrite,
            folder_with_segs_from_prev_stage=folder_with_segs_from_prev_stage,
        )

    nnUNetPredictor.predict_from_files = predict_from_files_single_process
    nnUNetPredictor._ctto3d_sequential_patch = True


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Run TotalSegmentator through nnU-Net sequential inference."
    )
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--task", default="total")
    parser.add_argument("-nr", "--nr_thr_resamp", type=int, default=1)
    parser.add_argument("-ns", "--nr_thr_saving", type=int, default=1)
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--fastest", action="store_true")
    parser.add_argument("--force_split", action="store_true")
    parser.add_argument("--device", default="gpu")
    parser.add_argument("--roi_subset", nargs="*")
    return parser.parse_args()


def main():
    args = _parse_args()
    from totalsegmentator.config import setup_nnunet

    setup_nnunet()
    _patch_nnunet_to_sequential()

    from totalsegmentator.python_api import totalsegmentator

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    totalsegmentator(
        Path(args.input),
        output,
        nr_thr_resamp=args.nr_thr_resamp,
        nr_thr_saving=args.nr_thr_saving,
        fast=args.fast and not args.fastest,
        fastest=args.fastest,
        task=args.task,
        roi_subset=args.roi_subset or None,
        force_split=args.force_split,
        device=args.device,
        output_type="nifti",
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
