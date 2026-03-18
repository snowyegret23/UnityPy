import io
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from UnityPy.files.replacers import AppendOnlySpillStore, BytesReplacer, SourceSliceReplacer, TempFileReplacer
from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter


class ReplacerTests(unittest.TestCase):
    def test_source_slice_replacer_streams_from_reader_without_full_copy(self) -> None:
        reader = EndianBinaryReader(b"abcdefghijk")
        replacer = SourceSliceReplacer.from_reader(reader, 2, 5)

        self.assertEqual(b"cdefg", replacer.read_bytes())

        writer = EndianBinaryWriter()
        replacer.write_to(writer, chunk_size=2)
        self.assertEqual(b"cdefg", writer.bytes)

    def test_source_slice_replacer_from_writer_reads_existing_stream(self) -> None:
        writer = EndianBinaryWriter()
        writer.write(b"hello world")
        replacer = SourceSliceReplacer.from_writer(writer, offset=6, size=5)

        out = EndianBinaryWriter()
        replacer.write_to(out, chunk_size=3)
        self.assertEqual(b"world", out.bytes)

    def test_append_only_spill_store_returns_reusable_slice(self) -> None:
        store = AppendOnlySpillStore(prefix="unitypy_test_replacer_")
        self.addCleanup(store.close)

        writer, segment = store.create_writer()
        writer.write(b"segment-one")
        writer.dispose()
        replacer = store.slice(segment.start, segment.length)

        self.assertEqual(b"segment-one", replacer.read_bytes())

        out = EndianBinaryWriter()
        replacer.write_to(out, chunk_size=4)
        self.assertEqual(b"segment-one", out.bytes)

    def test_temp_file_replacer_deletes_owned_file(self) -> None:
        store = AppendOnlySpillStore(prefix="unitypy_test_replacer_tmp_")
        self.addCleanup(store.close)
        temp_replacer = None
        try:
            tmp_path = store.path + ".owned"
            with open(tmp_path, "wb") as handle:
                handle.write(b"temp-data")
            temp_replacer = TempFileReplacer(tmp_path, 9, delete_on_cleanup=True)
            self.assertEqual(b"temp-data", temp_replacer.read_bytes())
        finally:
            if temp_replacer is not None:
                temp_replacer.cleanup()
                self.assertFalse(Path(tmp_path).exists())

    def test_bytes_replacer_iter_chunks(self) -> None:
        replacer = BytesReplacer(b"0123456789")
        self.assertEqual([b"012", b"345", b"678", b"9"], [bytes(c) for c in replacer.iter_chunks(3)])


if __name__ == "__main__":
    unittest.main()
