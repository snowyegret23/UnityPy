import os
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from UnityPy.files.BundleFile import BlockInfo, BundleFile
from UnityPy.streams import EndianBinaryReader


class BundleFileMemoryHelperTests(unittest.TestCase):
    def _make_bundle(self):
        bundle = object.__new__(BundleFile)
        bundle._blocks_tmp_path = None
        bundle._blocks_tmp_file = None
        bundle._blocks_mmap = None
        bundle.decompress_data = (
            lambda data, _uncompressed_size, _flags, _index=None: bytes(data)
        )
        return bundle

    def test_create_temp_backed_blocks_reader_uses_temp_storage(self):
        bundle = self._make_bundle()
        reader = EndianBinaryReader(b"abc123", offset=7)
        block_reader = bundle._create_temp_backed_blocks_reader(
            reader,
            [BlockInfo(3, 3, 0), BlockInfo(3, 3, 0)],
            offset=99,
        )

        self.assertTrue(hasattr(block_reader, "view"))
        self.assertEqual(block_reader.BaseOffset, 99)
        self.assertEqual(block_reader.read(6), b"abc123")
        self.assertTrue(bundle._blocks_tmp_path and os.path.isfile(bundle._blocks_tmp_path))

        tmp_path = bundle._blocks_tmp_path
        block_reader.dispose()
        bundle._cleanup_temp_blocks_storage()
        self.assertFalse(os.path.exists(tmp_path))

    def test_create_temp_backed_blocks_reader_handles_empty_block_data(self):
        bundle = self._make_bundle()
        reader = EndianBinaryReader(b"", offset=0)
        block_reader = bundle._create_temp_backed_blocks_reader(
            reader,
            [],
            offset=5,
        )

        self.assertEqual(block_reader.BaseOffset, 5)
        self.assertEqual(block_reader.read(), b"")
        self.assertIsNone(bundle._blocks_tmp_path)


if __name__ == "__main__":
    unittest.main()
