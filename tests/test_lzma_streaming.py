import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from UnityPy.enums import CompressionFlags
from UnityPy.files.BundleFile import BlockInfo, BundleFile
from UnityPy.helpers import CompressionHelper
from UnityPy.streams import EndianBinaryReader


class LzmaStreamingTests(unittest.TestCase):
    def test_lzma_file_stream_matches_one_shot_without_calling_compression_map(self):
        payload = b"ABCD" * (1024 * 1024 // 4)
        expected_data, expected_block_info = CompressionHelper.chunk_based_compress(
            payload,
            int(CompressionFlags.LZMA),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "compressed.bin")
            one_shot = mock.Mock(side_effect=AssertionError("one-shot LZMA compression was called"))
            chunks = (
                memoryview(payload)[offset : offset + 65536]
                for offset in range(0, len(payload), 65536)
            )

            with mock.patch.dict(
                CompressionHelper.COMPRESSION_MAP,
                {CompressionFlags.LZMA: one_shot},
            ):
                actual_block_info = CompressionHelper.chunk_based_compress_iter_to_file(
                    chunks,
                    int(CompressionFlags.LZMA),
                    output_path,
                )

            one_shot.assert_not_called()
            self.assertEqual(actual_block_info, expected_block_info)
            self.assertEqual(Path(output_path).read_bytes(), expected_data)

    def test_lzma_file_stream_preserves_none_fallback_and_empty_input(self):
        cases = (
            ("fallback", bytes(range(16))),
            ("empty", b""),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            for name, payload in cases:
                with self.subTest(name=name):
                    output_path = os.path.join(temp_dir, f"{name}.bin")
                    expected_data, expected_block_info = CompressionHelper.chunk_based_compress(
                        payload,
                        int(CompressionFlags.LZMA),
                    )

                    actual_block_info = CompressionHelper.chunk_based_compress_iter_to_file(
                        (payload,),
                        int(CompressionFlags.LZMA),
                        output_path,
                    )

                    self.assertEqual(actual_block_info, expected_block_info)
                    self.assertEqual(Path(output_path).read_bytes(), expected_data)

                    if name == "fallback":
                        self.assertEqual(
                            actual_block_info,
                            [(len(payload), len(payload), int(CompressionFlags.NONE))],
                        )
                    else:
                        self.assertEqual(actual_block_info, [])

            self.assertEqual(cases[0][1], Path(os.path.join(temp_dir, "fallback.bin")).read_bytes())

    def test_lzma_file_stream_preserves_logical_block_boundaries_and_upper_flags(self):
        payload = b"A" * 128 + bytes(range(128)) + b"B"
        block_info_flag = 0xC1

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(
            CompressionHelper.COMPRESSION_CHUNK_SIZE_MAP,
            {CompressionFlags.LZMA: 128},
        ):
            expected_data, expected_block_info = CompressionHelper.chunk_based_compress(
                payload,
                block_info_flag,
            )
            output_path = os.path.join(temp_dir, "blocks.bin")
            actual_block_info = CompressionHelper.chunk_based_compress_iter_to_file(
                (memoryview(payload)[offset : offset + 37] for offset in range(0, len(payload), 37)),
                block_info_flag,
                output_path,
            )

            self.assertEqual(actual_block_info, expected_block_info)
            self.assertEqual(Path(output_path).read_bytes(), expected_data)
            self.assertEqual(len(actual_block_info), 3)

    def test_bundle_lzma_block_streams_to_temp_without_one_shot_decompression(self):
        payload = b"UnityPy-LZMA-streaming" * (2 * 1024 * 1024 // 22)
        compressed = CompressionHelper.compress_lzma(payload)
        source = EndianBinaryReader(compressed)
        bundle = object.__new__(BundleFile)
        bundle.decryptor = None
        bundle._blocks_tmp_path = None
        bundle._blocks_tmp_file = None
        bundle._blocks_mmap = None
        bundle._blocks_reader = None
        block_reader = None
        temp_path = None
        one_shot = mock.Mock(side_effect=AssertionError("one-shot LZMA decompression was called"))

        try:
            with mock.patch.object(bundle, "decompress_data", one_shot):
                block_reader = bundle._create_temp_backed_blocks_reader(
                    source,
                    [
                        BlockInfo(
                            uncompressedSize=len(payload),
                            compressedSize=len(compressed),
                            flags=int(CompressionFlags.LZMA),
                        )
                    ],
                    offset=123,
                )

            one_shot.assert_not_called()
            temp_path = bundle._blocks_tmp_path
            self.assertIsNotNone(temp_path)
            self.assertTrue(os.path.isfile(temp_path))
            self.assertEqual(source.Position, len(compressed))
            self.assertEqual(block_reader.BaseOffset, 123)
            self.assertEqual(block_reader.read(), payload)
        finally:
            if block_reader is not None:
                block_reader.dispose()
            bundle._cleanup_temp_blocks_storage()

        self.assertIsNotNone(temp_path)
        self.assertFalse(os.path.exists(temp_path))


if __name__ == "__main__":
    unittest.main()
