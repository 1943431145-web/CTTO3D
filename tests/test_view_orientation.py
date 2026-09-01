import unittest

import numpy as np

from ctto3d import loader
from ctto3d.viewer import VolumeViewer


class ViewOrientationTests(unittest.TestCase):
    def test_dicom_loader_preserves_patient_orientation_for_view_reset(self):
        class Header:
            Rows = 2
            Columns = 3
            Modality = "CT"
            SeriesInstanceUID = "orientation-test"
            SeriesNumber = 1
            PixelSpacing = (0.8, 0.7)
            SliceThickness = 1.5
            ImageOrientationPatient = (1, 0, 0, 0, -1, 0)
            PatientPosition = "FFS"
            RescaleSlope = 1
            RescaleIntercept = 0

            def __init__(self, z, pixels=None):
                self.ImagePositionPatient = (0, 0, z)
                self.InstanceNumber = z
                self._pixels = pixels
                if pixels is not None:
                    self.PixelData = b"pixels"

            def __contains__(self, name):
                return name == "PixelData" and self._pixels is not None

            @property
            def pixel_array(self):
                return self._pixels

        headers = {
            "slice-0": Header(0),
            "slice-1": Header(-1),
        }
        datasets = {
            "slice-0": Header(0, np.zeros((2, 3), dtype=np.int16)),
            "slice-1": Header(-1, np.ones((2, 3), dtype=np.int16)),
        }

        _image, info = loader._load_dicom_sources(
            headers,
            lambda source, header_only: (
                headers[source] if header_only else datasets[source]),
        )

        self.assertEqual(
            info["image_orientation_patient"],
            (1.0, 0.0, 0.0, 0.0, -1.0, 0.0),
        )
        self.assertEqual(info["patient_position"], "FFS")

    def test_standard_axial_dicom_resets_to_anterior_with_head_up(self):
        position, view_up = VolumeViewer._anatomical_camera_axes({
            "image_orientation_patient": (1, 0, 0, 0, 1, 0),
            "patient_position": "HFS",
        })
        self.assertEqual(position, (0.0, -1.0, 0.0))
        self.assertEqual(view_up, (0.0, 0.0, 1.0))

    def test_reversed_patient_axes_flip_camera_and_keep_head_up(self):
        position, view_up = VolumeViewer._anatomical_camera_axes({
            "image_orientation_patient": (1, 0, 0, 0, -1, 0),
            "patient_position": "FFS",
        })
        self.assertEqual(position, (0.0, 1.0, 0.0))
        self.assertEqual(view_up, (0.0, 0.0, -1.0))

    def test_oblique_orientation_is_converted_to_local_volume_axes(self):
        position, view_up = VolumeViewer._anatomical_camera_axes({
            "image_orientation_patient": (0, 1, 0, -1, 0, 0),
        })
        self.assertEqual(position, (-1.0, 0.0, 0.0))
        self.assertEqual(view_up, (0.0, 0.0, 1.0))

    def test_non_dicom_data_keeps_existing_default_view(self):
        self.assertEqual(
            VolumeViewer._anatomical_camera_axes({}),
            ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
        )


if __name__ == "__main__":
    unittest.main()
