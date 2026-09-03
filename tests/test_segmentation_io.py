"""segmentation.py 的 NIfTI 读写回归（纯 VTK，不依赖 AI 环境）。

read_nifti 直接引用 reader 的输出（不再 DeepCopy），这里特意在临时文件
删除、reader 已被回收之后再访问体素，锁住"数据生命周期不随 reader/文件
结束"这一前提。
"""
import os
import tempfile
import unittest

import vtk

from ctto3d import segmentation


def _make_mask_image():
    image = vtk.vtkImageData()
    image.SetDimensions(6, 5, 4)              # x, y, z
    image.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
    for z in range(4):
        for y in range(5):
            for x in range(6):
                inside = 1 if (1 <= x <= 2 and 1 <= y <= 2) else 0
                image.SetScalarComponentFromDouble(x, y, z, 0, inside)
    return image


def _count_mask_voxels(image):
    nx, ny, nz = image.GetDimensions()
    return sum(
        1 for z in range(nz) for y in range(ny) for x in range(nx)
        if image.GetScalarComponentAsDouble(x, y, z, 0) > 0)


class ReadNiftiTests(unittest.TestCase):
    def _write_mask(self, directory):
        path = os.path.join(directory, "mask.nii")
        segmentation.write_nifti(_make_mask_image(), path)
        return path

    def test_round_trip_survives_reader_and_file_release(self):
        with tempfile.TemporaryDirectory() as directory:
            image = segmentation.read_nifti(self._write_mask(directory))
        # 此时临时文件已删除、read_nifti 内的 reader 也已回收
        self.assertEqual(image.GetDimensions(), (6, 5, 4))
        self.assertEqual(_count_mask_voxels(image), 2 * 2 * 4)

    def test_reference_geometry_applied_only_on_matching_dimensions(self):
        reference = vtk.vtkImageData()
        reference.SetDimensions(6, 5, 4)
        reference.SetOrigin(1.5, -2.0, 3.0)
        reference.SetSpacing(0.7, 0.7, 2.5)

        mismatched = vtk.vtkImageData()
        mismatched.SetDimensions(7, 5, 4)

        with tempfile.TemporaryDirectory() as directory:
            path = self._write_mask(directory)
            aligned = segmentation.read_nifti(path, reference)
            untouched = segmentation.read_nifti(path, mismatched)

        self.assertEqual(aligned.GetOrigin(), (1.5, -2.0, 3.0))
        self.assertEqual(aligned.GetSpacing(), (0.7, 0.7, 2.5))
        self.assertEqual(untouched.GetOrigin(), (0.0, 0.0, 0.0))
        self.assertEqual(untouched.GetSpacing(), (1.0, 1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
