import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest import mock

from UnityPy.enums import ArchiveFlagsOld
from UnityPy.files.BundleFile import BundleFile


bundle_file_module = importlib.import_module("UnityPy.files.BundleFile")


class BundleSaveCleanupTests(unittest.TestCase):
    def _make_bundle(self, entry) -> BundleFile:
        bundle = object.__new__(BundleFile)
        bundle.signature = "UnityFS"
        bundle.version = 6
        bundle.version_player = "5.x.x"
        bundle.version_engine = "2019.4.0f1"
        bundle.files = {"entry.assets": entry}
        bundle._directory_info_map = {}
        bundle._original_file_refs = {}
        bundle._blocks_reader = None
        bundle._blocks_tmp_path = None
        bundle._blocks_tmp_file = None
        bundle._blocks_mmap = None
        bundle.dataflags = ArchiveFlagsOld.BlocksAndDirectoryInfoCombined
        bundle._uses_block_alignment = False
        bundle._block_info_flags = 0
        return bundle

    def _tracked_mkstemp(self, temp_dir: str, created_paths: List[Path]):
        real_mkstemp = tempfile.mkstemp

        def tracked_mkstemp(*args, **kwargs):
            kwargs["dir"] = temp_dir
            fd, path = real_mkstemp(*args, **kwargs)
            created_paths.append(Path(path))
            return fd, path

        return tracked_mkstemp

    def test_entry_temp_file_is_removed_when_save_to_fails(self) -> None:
        entry_temp_paths: List[Path] = []

        class FailingEntry:
            flags = 0
            is_changed = True

            def save_to(self, path: str) -> None:
                entry_temp_paths.append(Path(path))
                raise RuntimeError("injected entry save failure")

        bundle = self._make_bundle(FailingEntry())

        with tempfile.TemporaryDirectory(prefix="unitypy_entry_cleanup_test_") as temp_dir:
            created_paths: List[Path] = []
            tracked_mkstemp = self._tracked_mkstemp(temp_dir, created_paths)
            output_path = Path(temp_dir) / "output.bundle"

            with mock.patch.object(bundle_file_module.tempfile, "mkstemp", side_effect=tracked_mkstemp):
                with self.assertRaisesRegex(RuntimeError, "injected entry save failure"):
                    bundle.save_to(output_path)

            self.assertEqual(len(entry_temp_paths), 1)
            self.assertIn(entry_temp_paths[0], created_paths)
            self.assertTrue(entry_temp_paths[0].name.startswith("unitypy_bundle_entry_"))
            self.assertFalse(entry_temp_paths[0].exists())

    def test_compressed_temp_file_is_removed_when_compression_fails(self) -> None:
        entry = SimpleNamespace(flags=0, is_changed=True, save=lambda: b"payload")
        bundle = self._make_bundle(entry)
        compressed_temp_paths: List[Path] = []

        def fail_compression(_chunks, _block_info_flag, out_path: str):
            compressed_temp_paths.append(Path(out_path))
            raise RuntimeError("injected compression failure")

        with tempfile.TemporaryDirectory(prefix="unitypy_compression_cleanup_test_") as temp_dir:
            created_paths: List[Path] = []
            tracked_mkstemp = self._tracked_mkstemp(temp_dir, created_paths)
            output_path = Path(temp_dir) / "output.bundle"

            with mock.patch.object(bundle_file_module.tempfile, "mkstemp", side_effect=tracked_mkstemp):
                with mock.patch.object(
                    bundle_file_module.CompressionHelper,
                    "chunk_based_compress_iter_to_file",
                    side_effect=fail_compression,
                ):
                    with self.assertRaisesRegex(RuntimeError, "injected compression failure"):
                        bundle.save_to(output_path)

            self.assertEqual(len(compressed_temp_paths), 1)
            self.assertIn(compressed_temp_paths[0], created_paths)
            self.assertFalse(compressed_temp_paths[0].exists())


if __name__ == "__main__":
    unittest.main()
